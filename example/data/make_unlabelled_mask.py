import zarr
import numpy as np
import sys

f = sys.argv[1]
ds = sys.argv[2]

f = zarr.open(f,"a")

labels = f[ds][:]

unlabelled = (labels > 0).astype(np.uint8)

f["unlabelled"] = unlabelled
f["unlabelled"].attrs["offset"] = f[ds].attrs["offset"]
f["unlabelled"].attrs["resolution"] = f[ds].attrs["resolution"]
