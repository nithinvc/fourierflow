import logging
import time
from functools import partial
from typing import Callable, Dict, List, Optional

import jax
import jax.numpy as jnp
import numpy as np
import xarray as xr
from hydra.utils import instantiate
from jax_cfd.base.boundaries import periodic_boundary_conditions
from jax_cfd.base.finite_differences import curl_2d
from jax_cfd.base.funcutils import repeated, trajectory
from jax_cfd.base.grids import Grid
from jax_cfd.base.initial_conditions import (filtered_velocity_field)
                                            #  wrap_velocities)
from jax_cfd.base.resize import downsample_staggered_velocity
from jax_cfd.spectral.utils import vorticity_to_velocity
from torch.utils.data import DataLoader, Dataset

from fourierflow.utils import downsample_vorticity_hat, import_string

from .base import Builder

# NOTE: Currently, wrap_velocities from jax cfd throws an error. We don't use it in benchmarking so set it to None
wrap_velocities = None

logger = logging.getLogger(__name__)

KEYS = ['vx', 'vy', 'vz']


class KolmogorovBuilder(Builder):
    name = 'kolmogorov'

    def __init__(self, train_dataset, valid_dataset, test_dataset,
                 loader_target: str = 'torch.utils.data.DataLoader', **kwargs):
        super().__init__()
        self.kwargs = kwargs
        self.train_dataset = train_dataset
        self.valid_dataset = valid_dataset
        self.test_dataset = test_dataset
        self.DataLoader = import_string(loader_target)

    def train_dataloader(self) -> DataLoader:
        loader = self.DataLoader(self.train_dataset,
                                 shuffle=True,
                                 **self.kwargs)
        return loader

    def val_dataloader(self) -> DataLoader:
        loader = self.DataLoader(self.valid_dataset,
                                 shuffle=False,
                                 **self.kwargs)
        return loader

    def test_dataloader(self) -> DataLoader:
        loader = self.DataLoader(self.test_dataset,
                                 shuffle=False,
                                 **self.kwargs)
        return loader

    def inference_data(self):
        k = self.test_dataset.k
        ds = self.test_dataset.ds.isel(time=slice(None, None, k))
        data = {
            'data': ds.vorticity.data,
            'vx': ds.vx.data,
            'vy': ds.vy.data,
        }
        return data


class KolmogorovJAXDataset(Dataset):
    def __init__(self, path, k, unroll_length, in_memory=False):
        self.ds = xr.open_dataset(path, engine='h5netcdf')
        self.k = k
        self.B = len(self.ds.sample)
        self.L = unroll_length
        self.T = len(self.ds.time) - self.k * self.L

        if in_memory:
            logger.info('Loading dataset into memory...')
            self.ds.load()

    def __len__(self):
        return self.B * self.T

    def __getitem__(self, idx):
        b = idx // self.T
        t = idx % self.T
        k = self.k
        L = self.L

        ds = self.ds.isel(sample=b, time=slice(t, t+L*k+1, k))
        in_ds = ds.isel(time=0)
        out_ds = ds.isel(time=slice(1, None, None)).transpose('x', 'y', 'time')

        inputs = {
            'vx': in_ds.vx.data,
            'vy': in_ds.vy.data,
            # 'vorticity': in_ds.vorticity,
        }

        outputs = {
            'vx': out_ds.vx.data,
            'vy': out_ds.vy.data,
            # 'vorticity': out_ds.vorticity,
        }

        return inputs, outputs


class KolmogorovTorchDataset(Dataset):
    def __init__(self, path, k, in_memory=False):
        self.ds = xr.open_dataset(path, engine='h5netcdf')
        self.k = k
        self.B = len(self.ds.sample)
        self.T = len(self.ds.time) - self.k

        if in_memory:
            logger.info('Loading dataset into memory...')
            self.ds.load()

    def __len__(self):
        return self.B * self.T

    def __getitem__(self, idx):
        b = idx // self.T
        t = idx % self.T
        k = self.k

        ds = self.ds.isel(sample=b, time=slice(t, t+k+1, k))
        in_ds = ds.isel(time=slice(0, 1)).transpose('x', 'y', 'time')
        out_ds = ds.isel(time=slice(1, 2)).transpose('x', 'y', 'time')

        return {
            'x': in_ds.vorticity.data,
            'vx': in_ds.vx.data,
            'vy': in_ds.vy.data,
            'y': out_ds.vorticity.data,
        }


class KolmogorovMultiTorchDataset(Dataset):
    def __init__(self, paths, k, batch_size):
        self.dss = [xr.open_dataset(path, engine='h5netcdf') for path in paths]
        self.k = k
        self.B = len(self.dss[0].sample)
        self.T = len(self.dss[0].time) - self.k
        self.counter = 0
        self.batch_size = batch_size
        self.ds_index = 0

    def __len__(self):
        return self.B * self.T

    def __getitem__(self, idx):
        b = idx // self.T
        t = idx % self.T
        k = self.k

        ds = self.dss[self.ds_index]
        ds = ds.isel(sample=b, time=slice(t, t+k+1, k))
        in_ds = ds.isel(time=slice(0, 1)).transpose('x', 'y', 'time')
        out_ds = ds.isel(time=slice(1, 2)).transpose('x', 'y', 'time')
        self.update_counter()

        return {
            'x': in_ds.vorticity.data,
            'y': out_ds.vorticity.data,
        }

    def update_counter(self):
        self.counter += 1
        if self.counter % self.batch_size == 0:
            self.ds_index = (self.ds_index + 1) % len(self.dss)


class KolmogorovTrajectoryDataset(Dataset):
    def __init__(self, init_path, path, corr_path, k, end=None, in_memory=False):
        ds = xr.open_dataset(path, engine='h5netcdf')
        init_ds = xr.open_dataset(init_path, engine='h5netcdf')
        init_ds = init_ds.expand_dims(dim={'time': [0.0]})
        ds = xr.concat([init_ds, ds], dim='time')
        self.ds = ds.transpose('sample', 'x', 'y', 'time')

        corr_ds = xr.open_dataset(corr_path, engine='h5netcdf')
        self.corr_ds = corr_ds.transpose('sample', 'x', 'y', 'time')

        self.k = k
        self.B = len(self.ds.sample)
        self.end = end

        if in_memory:
            logger.info('Loading datasets into memory...')
            self.ds.load()
            self.corr_ds.load()

    def __len__(self):
        return self.B

    def __getitem__(self, b):
        time_slice = slice(None, self.end, self.k)
        ds = self.ds.isel(sample=b, time=time_slice)
        corr_ds = self.corr_ds.isel(sample=b, time=time_slice)

        out = {
            'times': ds.time.data,
            'data': ds.vorticity.data,
            'vx': ds.vx.data,
            'vy': ds.vy.data,
            'corr_data': corr_ds.vorticity.data,
        }
        return out


class KolmogorovJAXTrajectoryDataset(Dataset):
    def __init__(self, init_path, path, corr_path, k, end=None,
                 inner_steps=1, outer_steps=100, in_memory=False):
        ds = xr.open_dataset(path, engine='h5netcdf')
        init_ds = xr.open_dataset(init_path, engine='h5netcdf')
        init_ds = init_ds.expand_dims(dim={'time': [0.0]})
        ds = xr.concat([init_ds, ds], dim='time')
        self.ds = ds.transpose('sample', 'x', 'y', 'time')

        corr_ds = xr.open_dataset(corr_path, engine='h5netcdf')
        self.corr_ds = corr_ds.transpose('sample', 'x', 'y', 'time')

        self.k = k
        self.B = len(self.ds.sample)
        self.end = end
        self.inner_steps = inner_steps
        self.outer_steps = outer_steps

        if in_memory:
            logger.info('Loading datasets into memory...')
            self.ds.load()
            self.corr_ds.load()

    def __len__(self):
        return self.B

    def __getitem__(self, b):
        time_slice = slice(None, self.end, self.k)
        ds = self.ds.isel(sample=b, time=time_slice)
        corr_ds = self.corr_ds.isel(sample=b, time=time_slice)

        s = self.inner_steps
        e = s + self.outer_steps * s

        out = {
            'times': corr_ds.time.data[..., s:e:s],
            'vx': ds.vx.data[..., 0],
            'vy': ds.vy.data[..., 0],
            'targets': corr_ds.vorticity.data[..., s:e:s],
        }
        return out


def get_learned_interpolation_step_fn(grid):

    from functools import partial

    import elegy
    import haiku as hk
    from jax_cfd.base.funcutils import init_context
    from jax_cfd.base.grids import GridArray, GridVariable
    from jax_cfd.ml.advections import modular_self_advection, self_advection
    from jax_cfd.ml.equations import modular_navier_stokes_model
    from jax_cfd.ml.forcings import kolmogorov_forcing
    from jax_cfd.ml.interpolations import FusedLearnedInterpolation
    from jax_cfd.ml.physics_specifications import NavierStokesPhysicsSpecs

    dt = 0.007012483601762931
    forcing_module = partial(kolmogorov_forcing,
                             scale=1.0,
                             wavenumber=4,
                             linear_coefficient=-0.1,
                             )
    interpolation_module = partial(FusedLearnedInterpolation,
                                   tags=['u', 'c']
                                   )
    advection_module = partial(modular_self_advection,
                               interpolation_module=interpolation_module,
                               )
    convection_module = partial(self_advection,
                                advection_module=advection_module,
                                )
    physics_specs = NavierStokesPhysicsSpecs(
        density=1.0,
        viscosity=1e-3,
        forcing_module=forcing_module,
    )

    def step_fwd(x):
        model = modular_navier_stokes_model(
            grid=grid,
            dt=dt,
            physics_specs=physics_specs,
            convection_module=convection_module,
        )
        return model(x)

    step_model = hk.without_apply_rng(hk.transform(step_fwd))

    model = elegy.load(
        "./experiments/kolmogorov/re_1000/learned_interpolation/x128/checkpoints/weights.06-0.99")
    params = hk.data_structures.to_immutable_dict(model.module.params_)

    # inputs = []
    # for seed, offset in enumerate(grid.cell_faces):
    #     rng_key = jax.random.PRNGKey(seed)
    #     data = jax.random.uniform(rng_key, grid.shape, jnp.float32)
    #     variable = GridVariable(
    #         array=GridArray(data, offset, grid),
    #         bc=periodic_boundary_conditions(grid.ndim))
    #     inputs.append(variable)
    # inputs = tuple(inputs)
    # rng = jax.random.PRNGKey(42)

    # with init_context():
    #     params = step_model.init(rng, inputs)

    def step_fn(inputs):
        return step_model.apply(params, inputs)

    return step_fn


def generate_kolmogorov(sim_grid: Grid,
                        out_sizes: List[Dict[str, int]],
                        method: str,
                        step_fn: Callable,
                        downsample_fn: Callable,
                        seed: jax.random.KeyArray,
                        initial_field: Optional[xr.Dataset] = None,
                        peak_wavenumber: float = 4.0,
                        max_velocity: float = 7.0,
                        inner_steps: int = 25,
                        outer_steps: int = 200,
                        warmup_steps: int = 40,
                        out_vorticity: bool = True):
    """Generate 2D Kolmogorov flows, similar to Kochkov et al (2021).

    Adapted from https://github.com/google/jax-cfd/blob/main/notebooks/demo.ipynb
    """
    # Seems that there is some memory leak, especially when generating
    # re_1000/short_trajectories
    jax.lib.xla_bridge.get_backend.cache_clear()
    # Define the physical dimensions of the simulation.
    velocity_solve = vorticity_to_velocity(
        sim_grid) if sim_grid.ndim == 2 else None

    out_grids = {}
    for o in out_sizes:
        grid = Grid(shape=[o['size']] * sim_grid.ndim, domain=sim_grid.domain)
        out_grids[(o['size'], o['k'])] = grid

    downsample = partial(downsample_fn, sim_grid, out_grids,
                         velocity_solve, out_vorticity)

    if initial_field is None:
        # Construct a random initial velocity. The `filtered_velocity_field`
        # function ensures that the initial velocity is divergence free and it
        # filters out high frequency fluctuations.
        v0 = filtered_velocity_field(
            seed, sim_grid, max_velocity, peak_wavenumber)
        if method == 'pseudo_spectral':
            # Compute the fft of the vorticity. The spectral code assumes an fft'd
            # vorticity for an initial state.
            vorticity0 = curl_2d(v0).data
    else:
        u, bcs = [], []
        for i in range(sim_grid.ndim):
            u.append(initial_field[KEYS[i]].data)
            bcs.append(periodic_boundary_conditions(sim_grid.ndim))
        v0 = wrap_velocities(u, sim_grid, bcs)
        if method == 'pseudo_spectral':
            vorticity0 = initial_field.vorticity.values

    if method == 'pseudo_spectral':
        state = jnp.fft.rfftn(vorticity0, axes=(0, 1))
    else:
        state = v0

    step_fn = instantiate(step_fn)
    # step_fn = get_learned_interpolation_step_fn(sim_grid)
    outer_step_fn = repeated(step_fn, inner_steps)

    # During warming up, we ignore intermediate results and just return
    # the final field
    if warmup_steps > 0:
        def ignore(_):
            return None
        trajectory_fn = trajectory(outer_step_fn, warmup_steps, ignore)
        start = time.time()
        state, _ = trajectory_fn(state)
        elapsed = np.float32(time.time() - start)
        outs = downsample(state)
        return outs, elapsed

    if outer_steps > 0:
        start = time.time()
        trajectory_fn = trajectory(outer_step_fn, outer_steps, downsample)
        _, trajs = trajectory_fn(state)
        elapsed = np.float32(time.time() - start)
        return trajs, elapsed


def downsample_vorticity(sim_grid, out_grids, velocity_solve, out_vorticity, vorticity_hat):
    outs = {}
    for key, out_grid in out_grids.items():
        size = key[0]
        if size == sim_grid.shape[0]:
            vxhat, vyhat = velocity_solve(vorticity_hat)
            out = {
                'vx': jnp.fft.irfftn(vxhat, axes=(0, 1)),
                'vy': jnp.fft.irfftn(vyhat, axes=(0, 1)),
                'vorticity': jnp.fft.irfftn(vorticity_hat, axes=(0, 1)),
            }
        else:
            out = downsample_vorticity_hat(
                vorticity_hat, velocity_solve, sim_grid, out_grid)
        if not out_vorticity:
            del out['vorticity']
        outs[key] = out

    # cpu = jax.devices('cpu')[0]
    # outs = {k: jax.device_put(v, cpu) for k, v in outs.items()}
    return outs


def downsample_velocity(sim_grid, out_grids, velocity_solve, out_vorticity, u):
    outs = {}
    for key, out_grid in out_grids.items():
        size = key[0]
        out = {}
        if size == sim_grid.shape[0]:
            for i in range(sim_grid.ndim):
                out[KEYS[i]] = u[i].data
            if sim_grid.ndim == 2 and out_vorticity:
                out['vorticity'] = curl_2d(u).data
        else:
            u_new = downsample_staggered_velocity(
                sim_grid, out_grid, u)
            for i in range(sim_grid.ndim):
                out[KEYS[i]] = u_new[i].data
            if sim_grid.ndim == 2 and out_vorticity:
                out['vorticity'] = curl_2d(u_new).data
        outs[key] = out

    # cpu = jax.devices('cpu')[0]
    # outs = {k: jax.device_put(v, cpu) for k, v in outs.items()}
    return outs
