import logging

import torch
from torch._C import Value

from nequip.nn import RescaleOutput, GraphModuleMixin, PerSpeciesScaleShift
from nequip.data import AtomicDataDict, AtomicDataset
from ._grads import ForceOutput


def compute_stats(str_names, dataset, stride):

    # parse the list of string to field, mode
    # and record which quantity correspond to which computed_item
    stat_modes = []
    stat_fields = []
    stat_strs = []
    ids = []
    tuple_ids = []
    mode_count = 0
    tuple_id_map = {"mean": 0, "std": 1, "rms": 0}
    for name in str_names:
        # remove dataset prefix
        if name.startswith("dataset_"):
            name = name[len("dataset_") :]
        # identify per_species and per_atom modes
        prefix = ""
        if name.startswith("per_species_"):
            name = name[len("per_species_") :]
            prefix = "per_species_"
        elif name.startswith("per_atom_"):
            name = name[len("per_atom_") :]
            prefix = "per_atom_"

        stat = name.split("_")[-1]
        field = "_".join(name.split("_")[:-1])
        if stat in ["mean", "std"]:
            stat_mode = prefix + "mean_std"
            stat_str = field + prefix + "mean_std"
        elif stat in ["rms"]:
            stat_mode = prefix + "rms"
            stat_str = field + prefix + "rms"
        else:
            raise ValueError(f"Cannot handle {stat} type quantity")

        if stat_str in stat_strs:
            ids += [stat_strs.index(stat_str)]
        else:
            ids += [len(stat_strs)]
            stat_strs += [stat_str]
            stat_modes += [stat_mode]
            stat_fields += [field]
        tuple_ids += [tuple_id_map[stat]]

    values = dataset.statistics(
        fields=stat_fields,
        modes=stat_modes,
        stride=stride,
    )
    return [values[idx][tuple_ids[i]] for i, idx in enumerate(ids)]


def RescaleEnergyEtc(
    model: GraphModuleMixin,
    config,
    dataset: AtomicDataset,
    initialize: bool,
):
    """Add global rescaling for energy(-based quantities).

    If ``initialize`` is false, doesn't compute statistics.
    """
    global_scale = config.get(
        "global_rescale_scale",
        "dataset_force_rms"
        if AtomicDataDict.FORCE_KEY in model.irreps_out
        else "dataset_energy_std",
    )
    # TODO: change this default?
    global_shift = config.get("global_rescale_shift", "dataset_energy_mean")

    # = Get statistics of training dataset =
    if initialize:
        str_names = []
        for value in [global_scale, global_shift]:
            if isinstance(value, str):
                str_names += [value]
            elif (
                global_scale is None
                or isinstance(value, float)
                or isinstance(value, torch.Tensor)
            ):
                # valid values
                pass
            else:
                raise ValueError(f"Invalid global scale `{global_scale}`")

        # = Compute shifts and scales =
        computed_stats = compute_stats(
            str_names=str_names,
            dataset=dataset,
            stride=config.dataset_statistics_stride,
        )

        if isinstance(global_scale, str):
            global_scale = computed_stats[str_names.index(global_scale)]
        if isinstance(global_shift, str):
            global_shift = computed_stats[str_names.index(global_shift)]

        RESCALE_THRESHOLD = 1e-6
        if isinstance(global_scale, float) and global_scale < RESCALE_THRESHOLD:
            raise ValueError(
                f"Global energy scaling was very low: {global_scale}. If dataset values were used, does the dataset contain insufficient variation? Maybe try disabling global scaling with global_scale=None."
            )

        logging.debug(
            f"Initially outputs are scaled by: {global_scale}, eneriges are shifted by {global_shift}."
        )
    else:
        # Put dummy values
        if global_shift is not None:
            global_shift = 0.0  # it has some kind of value
        if global_scale is not None:
            global_scale = 1.0  # same,

    # == Build the model ==
    return RescaleOutput(
        model=model,
        scale_keys=[
            k
            for k in (
                AtomicDataDict.TOTAL_ENERGY_KEY,
                AtomicDataDict.PER_ATOM_ENERGY_KEY,
                AtomicDataDict.FORCE_KEY,
            )
            if k in model.irreps_out
        ],
        scale_by=global_scale,
        shift_keys=[
            k for k in (AtomicDataDict.TOTAL_ENERGY_KEY,) if k in model.irreps_out
        ],
        shift_by=global_shift,
        trainable_global_rescale_shift=config.get(
            "trainable_global_rescale_shift", False
        ),
        trainable_global_rescale_scale=config.get(
            "trainable_global_rescale_scale", False
        ),
    )


def PerSpecieRescale(
    model: GraphModuleMixin,
    config,
    dataset: AtomicDataset,
    initialize: bool,
):
    """Add global rescaling for energy(-based quantities).

    If ``initialize`` is false, doesn't compute statistics.
    """
    module_prefix = "PerSpeciesScaleShift_"

    force_training = AtomicDataDict.FORCE_KEY in model.irreps_out

    # = Determine energy rescale type =
    global_scale = config.get(
        "global_rescale_scale",
        "dataset_force_rms" if force_training else "dataset_energy_std",
    )
    global_shift = config.get("global_rescale_shift", None)
    scales = config.get(module_prefix + "scales", None)
    shifts = config.get(module_prefix + "shifts", None)

    if global_shift is not None:
        raise ValueError("One can only enable either global shift or per_species shift")

    logging.info(f"Enable per species scale/shift")

    # = Determine what statistics need to be compute =
    if initialize:
        str_names = []
        for value in [scales, shifts, global_scale]:
            if isinstance(value, str):
                str_names += [value]
            elif (
                global_scale is None
                or isinstance(value, float)
                or isinstance(value, list)
                or isinstance(value, torch.Tensor)
            ):
                # valid values
                pass
            else:
                raise ValueError(f"Invalid global scale `{global_scale}`")

        # = Compute shifts and scales =
        computed_stats = compute_stats(
            str_names=str_names,
            dataset=dataset,
            stride=config.dataset_statistics_stride,
        )

        if isinstance(scales, str):
            scales = computed_stats[str_names.index(scales)]
        if isinstance(shifts, str):
            shifts = computed_stats[str_names.index(shifts)]
        if isinstance(global_scale, str):
            global_scale = computed_stats[str_names.index(global_scale)]

        if global_scale is not None:
            scales /= global_scale

    else:
        # Put dummy values
        if scales is not None:
            scales = 1.0  # it has some kind of value
        if shifts is not None:
            shifts = 1.0  # same,

    # first peel off the gradient part
    model_func = model.func if force_training else model

    # insert in per species shift
    model_func.insert_from_parameters(
        after="total_energy_sum",
        shared_params=config,
        name="per_species_scale_shift",
        builder=PerSpeciesScaleShift,
        params=dict(
            field=AtomicDataDict.PER_ATOM_ENERGY_KEY,
            out_field=AtomicDataDict.PER_ATOM_ENERGY_KEY,
        ),
        prepend=True,
    )

    # == Build the model ==
    return model
