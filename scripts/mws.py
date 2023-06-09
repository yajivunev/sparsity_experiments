import numpy as np

from funlib.persistence import open_ds
from funlib.geometry import Coordinate,Roi
from funlib.segment.arrays import replace_values

from skimage.filters import threshold_otsu

import mahotas
from affogato.segmentation import compute_mws_segmentation
from affogato.segmentation import MWSGridGraph, compute_mws_clustering
from typing import Optional

from skimage.morphology import remove_small_objects
from scipy.ndimage import distance_transform_edt, maximum_filter, measurements, gaussian_filter
from skimage.measure import label


def watershed_from_boundary_distance(
    boundary_distances, return_seeds=False, id_offset=0, min_seed_distance=10
):

    max_filtered = maximum_filter(boundary_distances, min_seed_distance)
    maxima = max_filtered == boundary_distances
    seeds, n = mahotas.label(maxima)

    if n == 0:
        return np.zeros(boundary_distances.shape, dtype=np.uint64), id_offset

    seeds[seeds != 0] += id_offset

    fragments = mahotas.cwatershed(boundary_distances.max() - boundary_distances, seeds)

    ret = (fragments.astype(np.uint64), n + id_offset)
    if return_seeds:
        ret = ret + (seeds.astype(np.uint64),)

    return ret


def mutex_watershed(
        affs,
        offsets,
        stride,
        algorithm="kruskal",
        mask=None,
        randomize_strides=True,
        sep=3) -> np.ndarray:

    affs = 1 - affs

    affs[:sep] = affs[:sep] * -1
    affs[:sep] = affs[:sep] + 1

    segmentation = compute_mws_segmentation(
        affs,
        offsets,
        sep,
        strides=stride,
        randomize_strides=randomize_strides,
        algorithm=algorithm,
        mask=mask,
    )

    return segmentation


def seeded_mutex_watershed(
        seeds,
        affs,
        offsets,
        mask,
        stride,
        randomize_strides=True) -> np.ndarray:
    
    shape = affs.shape[1:]
    if seeds is not None:
        assert (len(seeds.shape) == len(shape)
        ), f"Got shape {seeds.data.shape} for mask but expected {shape}"
    if mask is not None:
        assert (len(mask.shape) == len(shape)
        ), f"Got shape {mask.data.shape} for mask but expected {shape}"

    grid_graph = MWSGridGraph(shape)
    if seeds is not None:
        grid_graph.update_seeds(seeds.data)

    ndim = len(offsets[0])

    grid_graph.add_attractive_seed_edges = True
    neighbor_affs, lr_affs = (
        np.require(affs[:ndim], requirements="C"),
        np.require(affs[ndim:], requirements="C"),
    )
    
    # assuming affinities are 1 between voxels that belong together and
    # 0 if they are not part of the same object. Invert if the other way
    # around.
    # neighbors_affs should be high for objects that belong together
    # lr_affs is the oposite
    lr_affs = 1 - lr_affs

    uvs, weights = grid_graph.compute_nh_and_weights(
        neighbor_affs, offsets[:ndim]
    )

    if stride is None:
        stride = [1] * ndim
        
    grid_graph.add_attractive_seed_edges = False
    mutex_uvs, mutex_weights = grid_graph.compute_nh_and_weights(
        lr_affs,
        offsets[ndim:],
        stride,
        randomize_strides=randomize_strides,
    )

    # compute the segmentation
    n_nodes = grid_graph.n_nodes
    segmentation = compute_mws_clustering(
        n_nodes, uvs, mutex_uvs, weights, mutex_weights
    )
    grid_graph.relabel_to_seeds(segmentation)
    segmentation = segmentation.reshape(shape)
    if mask is not None:
        segmentation[np.logical_not(mask)] = 0

    return segmentation


def post(
        pred_file,
        pred_dataset,
        roi,
        neighborhood,
        stride,
        randomize_strides,
        algorithm,
        mask_thresh,
        filter_fragments,
        **kwargs):

    # load
    pred = open_ds(pred_file,pred_dataset)

    if roi is not None:
        roi = Roi(pred.roi.offset+Coordinate(roi[0]),roi[1])
    else:
        roi = pred.roi


    voxel_size = pred.voxel_size
    context = Coordinate([4,16,16]) #* voxel_size

    pred = pred.to_ndarray(roi)
   
    if pred.dtype == np.uint8:
        max_affinity_value = 255.0
        pred = pred.astype(np.float32)
    else:
        max_affinity_value = 1.0

    if pred.max() < 1e-3:
        return

    pred /= max_affinity_value
    
    # prepare
    offsets = [tuple(x) for x in neighborhood]
 
    # extract fragments
    adjacent_edge_bias = -0.4  # bias towards merging
    lr_edge_bias = -0.7  # bias heavily towards splitting

    # add some random noise to affs (this is particularly necessary if your affs are
    #  stored as uint8 or similar)
    # If you have many affinities of the exact same value the order they are processed
    # in may be fifo, so you can get annoying streaks.
    random_noise = np.random.randn(*pred.shape) * 0.001
    # add smoothed affs, to solve a similar issue to the random noise. We want to bias
    # towards processing the central regions of objects first.
    
#    smoothed_pred = (
#        gaussian_filter(pred, sigma=(0, *(Coordinate(context) / 3))) - 0.5
#    ) * 0.01
#    shift = np.array(
#        [adjacent_edge_bias if max(offset) <= 1 else lr_edge_bias for offset in offsets]
#    ).reshape((-1, *((1,) * (len(pred.shape) - 1))))
    
    if mask_thresh > 0.0 or algorithm == "seeded":    
        
        mean_pred = 0.5 * (pred[1] + pred[2])
        depth = mean_pred.shape[0]

        if mask_thresh > 0.0:
            mask = np.zeros(mean_pred.shape, dtype=bool)
        
        if algorithm == "seeded":
            seeds = np.zeros(mean_pred.shape, dtype=np.uint64)

        for z in range(depth):

            boundary_mask = mean_pred[z] > mask_thresh * np.max(pred)
            boundary_distances = distance_transform_edt(boundary_mask)
            if mask_thresh > 0.0:
                mask[z] = boundary_mask

            if algorithm == "seeded":
                _,_,seeds[z] = watershed_from_boundary_distance(
                    boundary_distances,
                    return_seeds=True,
                )
    
    if mask_thresh == 0.0:
        mask = None

    if "seeded" in algorithm:
        seeds = seeds if "wo" not in algorithm else None   
        
        seg = seeded_mutex_watershed(
            seeds=seeds,
            affs=pred,
            offsets=offsets,
            mask=mask,
            stride=stride,
            randomize_strides=randomize_strides)
    
    else:
        seg = mutex_watershed(
            pred + random_noise,#+ shift + random_noise + smoothed_pred,
            offsets=offsets,
            stride=stride,
            algorithm=algorithm,
            mask=mask,
            randomize_strides=randomize_strides)

    if filter_fragments > 0:
        average_pred = np.mean(pred.data, axis=0)

        filtered_fragments = []

        fragment_ids = np.unique(seg)

        for fragment, mean in zip(
            fragment_ids, measurements.mean(average_pred, seg, fragment_ids)
        ):
            if mean < filter_fragments:
                filtered_fragments.append(fragment)

        filtered_fragments = np.array(filtered_fragments, dtype=seg.dtype)
        replace = np.zeros_like(filtered_fragments)
        replace_values(seg, filtered_fragments, replace, inplace=True)
    
    return seg
