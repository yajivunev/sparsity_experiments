import gunpowder as gp
import os
import json
import logging
import math
import numpy as np
import random
import torch
import zarr
from lsd.train.gp import AddLocalShapeDescriptor
from lsd.train import LsdExtractor
from scipy.ndimage import (
    binary_dilation,
    distance_transform_edt,
    gaussian_filter,
    maximum_filter,
    generate_binary_structure,
    label
)
from skimage.morphology import disk
from skimage.measure import label
from skimage.segmentation import watershed, expand_labels
from model import AffsUNet, WeightedMSELoss

setup_dir = os.path.abspath(os.path.dirname(os.path.realpath(__file__)))

logging.basicConfig(level=logging.INFO)

torch.backends.cudnn.benchmark = True

def init_weights(m):
    if isinstance(m, (torch.nn.Conv3d,torch.nn.ConvTranspose3d)):
        torch.nn.init.kaiming_uniform_(m.weight,nonlinearity='relu')

class ZerosSource(gp.BatchProvider):
    def __init__(self, datasets, shape=None, dtype=np.uint64, array_specs=None):
        self.datasets = datasets

        if array_specs is None:
            self.array_specs = {}
        else:
            self.array_specs = array_specs

        self.shape = shape if shape is not None else gp.Coordinate((200, 200, 200))
        self.dtype = dtype

        # number of spatial dimensions
        self.ndims = None

    def setup(self):
        for array_key, ds_name in self.datasets.items():
            if array_key in self.array_specs:
                spec = self.array_specs[array_key].copy()
            else:
                spec = gp.ArraySpec()

            if spec.voxel_size is None:
                voxel_size = gp.Coordinate((1,) * len(self.shape))
                spec.voxel_size = voxel_size

            self.ndims = len(spec.voxel_size)

            if spec.roi is None:
                offset = gp.Coordinate((0,) * self.ndims)
                spec.roi = gp.Roi(offset, self.shape * spec.voxel_size)

            if spec.dtype is not None:
                assert spec.dtype == self.dtype
            else:
                spec.dtype = self.dtype

            if spec.interpolatable is None:
                spec.interpolatable = spec.dtype in [
                    np.float,
                    np.float32,
                    np.float64,
                    np.float10,
                    np.uint8,  # assuming this is not used for labels
                ]

            self.provides(array_key, spec)

    def provide(self, request):
        batch = gp.Batch()

        for array_key, request_spec in request.array_specs.items():
            voxel_size = self.spec[array_key].voxel_size

            # scale request roi to voxel units
            dataset_roi = request_spec.roi / voxel_size

            # shift request roi into dataset
            dataset_roi = (
                dataset_roi - self.spec[array_key].roi.get_offset() / voxel_size
            )


            # create array spec
            array_spec = self.spec[array_key].copy()
            array_spec.roi = request_spec.roi

            # add array to batch
            batch.arrays[array_key] = gp.Array(
                np.zeros(self.shape, self.dtype)[dataset_roi.to_slices()], array_spec
            )

        return batch


class CreateLabels(gp.BatchFilter):
    def __init__(
        self,
        labels,
        anisotropy
    ):

        self.labels = labels
        self.anisotropy = anisotropy + 1

    def process(self, batch, request):

        labels = batch[self.labels].data
        anisotropy = random.choice(range(self.anisotropy)) + 1
        labels = np.concatenate([labels,]*anisotropy)
        shape = labels.shape

        # random 3d labels generation method
        choice = random.choice(["a","b","c"])

        if choice == "a":
            # different numbers simulate more or less objects
            num_points = random.randint(25,50*anisotropy)

            for n in range(num_points):
                z = random.randint(1, labels.shape[0] - 1)
                y = random.randint(1, labels.shape[1] - 1)
                x = random.randint(1, labels.shape[2] - 1)

                labels[z, y, x] = 1
            
            structs = [generate_binary_structure(2, 2), disk(random.randint(1,5))]

            # different numbers will simulate larger or smaller objects
            for z in range(labels.shape[0]):
                
                dilations = random.randint(1, 10)
                struct = random.choice(structs)

                dilated = binary_dilation(
                    labels[z], structure=struct, iterations=dilations
                )

                labels[z] = dilated.astype(labels.dtype)

            # relabel
            labels = label(labels)

            # expand labels
            distance = labels.shape[0]

            distances, indices = distance_transform_edt(
                labels == 0, return_indices=True
            )

            expanded_labels = np.zeros_like(labels)

            dilate_mask = distances <= distance

            masked_indices = [
                dimension_indices[dilate_mask] for dimension_indices in indices
            ]

            nearest_labels = labels[tuple(masked_indices)]

            expanded_labels[dilate_mask] = nearest_labels

            labels = expanded_labels

            # change background
            labels[labels == 0] = np.max(labels) + 1

            # relabel
            labels = label(labels)[::anisotropy].astype(np.uint64)

        elif choice == "b":
            np.random.seed()
            peaks = np.random.random(shape).astype(np.float32)
            peaks = gaussian_filter(peaks, sigma=5.0)
            max_filtered = maximum_filter(peaks, 10)
            maxima = max_filtered == peaks
            seeds = label(maxima,connectivity=1)
            
            labels = watershed(1.0 - peaks, seeds)[::anisotropy].astype(np.uint64)

        elif choice == "c":
            # sample random points
            num_points = random.randint(25,50*anisotropy)

            for n in range(num_points):
                z = random.randint(1, labels.shape[0] - 1)
                y = random.randint(1, labels.shape[1] - 1)
                x = random.randint(1, labels.shape[2] - 1)

                labels[z, y, x] = 1

            # relabel
            labels = label(labels)

            # dilate
            dilations = random.randint(25,40)
            labels = expand_labels(labels,dilations)

            # change bg id
            labels[labels == 0] = np.max(labels) + 1

            # relabel
            labels = label(labels)[::anisotropy].astype(np.uint64)

        else:
            raise AssertionError("invalid choice")

        batch[self.labels].data = labels


class SmoothLSDs(gp.BatchFilter):
    def __init__(self, lsds):
        self.lsds = lsds

    def process(self, batch, request):

        lsds = batch[self.lsds].data

        # different numbers will simulate noisier or cleaner lsds
        sigma = random.uniform(0.5, 1.5)

        for z in range(lsds.shape[1]):
            lsds_sec = lsds[:, z]

            lsds[:, z] = np.array(
                [
                    gaussian_filter(lsds_sec[i], sigma=sigma)
                    for i in range(lsds_sec.shape[0])
                ]
            ).astype(lsds_sec.dtype)

        batch[self.lsds].data = lsds


def train(
        iterations,
        in_channels,
        num_fmaps,
        fmap_inc_factor,
        downsample_factors,
        kernel_size_down,
        kernel_size_up,
        input_shape,
        voxel_size,
        sigma,
        batch_size,
        neighborhood,
        **kwargs):

    model = AffsUNet(
            in_channels,
            num_fmaps,
            fmap_inc_factor,
            downsample_factors,
            kernel_size_down,
            kernel_size_up)

    #model.apply(init_weights)

    loss = WeightedMSELoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=0.5e-4)

    zeros = gp.ArrayKey("ZEROS")
    gt_lsds = gp.ArrayKey("GT_LSDS")
    gt_affs = gp.ArrayKey("GT_AFFS")
    pred_affs = gp.ArrayKey("PRED_AFFS")
    affs_weights = gp.ArrayKey("AFFS_WEIGHTS")

    if 'output_shape' not in kwargs:
        output_shape = model.forward(torch.empty(size=[batch_size,in_channels]+input_shape))[0].shape[1:]
        with open(os.path.join(setup_dir,"config.json"),"r") as f:
            config = json.load(f)

        config['output_shape'] = list(output_shape)

        with open(os.path.join(setup_dir,"config.json"),"w") as f:
            json.dump(config,f)

    else: output_shape = kwargs.get("output_shape")

    output_shape = gp.Coordinate(tuple(output_shape))
    input_shape = gp.Coordinate(tuple(input_shape))

    voxel_size = gp.Coordinate(voxel_size)
    
    anisotropy = int((voxel_size[0] / voxel_size[1]) - 1) # 0 is isotropic

    output_size = output_shape * voxel_size
    input_size = input_shape * voxel_size

    request = gp.BatchRequest()

    request.add(zeros, input_size)
    request.add(gt_lsds, input_size)
    request.add(gt_affs, output_size)
    request.add(pred_affs, output_size)
    request.add(affs_weights, output_size)

    source = ZerosSource(
        {
            zeros: "zeros",  # just a zeros dataset, since we need a source
        },
        shape=input_shape,
        array_specs={
            zeros: gp.ArraySpec(interpolatable=False, voxel_size=voxel_size),
        },
    )

    source += gp.Pad(zeros, None)

    pipeline = source

    pipeline += CreateLabels(zeros,anisotropy)

    if input_shape[0] != input_shape[1]:
        pipeline += gp.SimpleAugment(transpose_only=[1, 2])
    else:
        pipeline += gp.SimpleAugment()

    pipeline += gp.ElasticAugment(
        control_point_spacing=[voxel_size[1], voxel_size[0], voxel_size[0]],
        jitter_sigma=[2*int(not bool(anisotropy)), 2, 2],
        scale_interval=(0.75,1.25),
        rotation_interval=[0,math.pi/2.0],
        subsample=4,
    )

    # do this on non eroded labels - that is what predicted lsds will look like
    pipeline += AddLocalShapeDescriptor(
        zeros, gt_lsds, sigma=sigma, downsample=2
    )

    # add random noise
    pipeline += gp.NoiseAugment(gt_lsds)
    
    pipeline += gp.IntensityAugment(gt_lsds, 0.9, 1.1, -0.1, 0.1)

    # smooth the batch by different sigmas to simulate noisy predictions
    pipeline += SmoothLSDs(gt_lsds)

    # now we erode - we want the gt affs to have a pixel boundary
    pipeline += gp.GrowBoundary(zeros, steps=1, only_xy=bool(anisotropy))

    pipeline += gp.AddAffinities(
        affinity_neighborhood=neighborhood,
        labels=zeros,
        affinities=gt_affs,
        dtype=np.float32,
    )

    pipeline += gp.BalanceLabels(gt_affs, affs_weights)

    pipeline += gp.Stack(batch_size)

    pipeline += gp.PreCache(cache_size=50, num_workers=20)

    pipeline += gp.torch.Train(
        model,
        loss,
        optimizer,
        inputs={"input": gt_lsds},
        loss_inputs={0: pred_affs, 1: gt_affs, 2: affs_weights},
        outputs={0: pred_affs},
        save_every=1000,
        log_dir=os.path.join(setup_dir,'log'),
        checkpoint_basename=os.path.join(setup_dir,'model')
    )

    pipeline += gp.Squeeze([gt_lsds,gt_affs,pred_affs])
    
    pipeline += gp.Snapshot(
        dataset_names={
            zeros: "labels",
            gt_lsds: "gt_lsds",
            gt_affs: "gt_affs",
            pred_affs: "pred_affs",
        },
        output_filename="batch_{iteration}.zarr",
        output_dir=os.path.join(setup_dir,'snapshots'),
        every=1000,
    )

    with gp.build(pipeline):
        for i in range(iterations):
            pipeline.request_batch(request)


if __name__ == "__main__":

    config_path = os.path.join(setup_dir,"config.json")

    with open(config_path,"r") as f:
        config = json.load(f)

    train(**config)
