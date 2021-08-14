import math
import os
from enum import Enum

import h5py
import numpy as np
import ptvsd
import torch
from einops import repeat
from typer import Argument, Option, Typer

from fourierflow.datastores.synthetic import GaussianRF, solve_navier_stokes_2d

app = Typer()


class Force(str, Enum):
    li = "li"
    random = "random"


def get_random_force(b, s, device, cycles, scaling):
    ft = torch.linspace(0, 1, s+1).to(device)
    ft = ft[0:-1]
    X, Y = torch.meshgrid(ft, ft)
    X = repeat(X, 'x y -> b x y', b=b)
    Y = repeat(Y, 'x y -> b x y', b=b)

    f = 0
    for p in range(1, cycles + 1):
        k = 2 * math.pi * p
        f += torch.rand(b, 1, 1).to(device) * torch.sin(k * X)
        f += torch.rand(b, 1, 1).to(device) * torch.cos(k * X)

        f += torch.rand(b, 1, 1).to(device) * torch.sin(k * Y)
        f += torch.rand(b, 1, 1).to(device) * torch.cos(k * Y)

        f += torch.rand(b, 1, 1).to(device) * torch.sin(k * (X + Y))
        f += torch.rand(b, 1, 1).to(device) * torch.cos(k * (X + Y))

    f = f * scaling

    return f


@app.command()
def navier_stokes(
    path: str = Argument(..., help='Path to store the generated samples'),
    n: int = Option(1400, help='Number of solutions to generate'),
    s: int = Option(256, help='Width of the solution grid'),
    t: int = Option(20, help='Final time step'),
    steps: int = Option(20, help='Number of snapshots from solution'),
    mu: float = Option(1e-5, help='Viscoity'),
    k_min: int = Option(5, help='Minimium exponent of viscoity'),
    k_max: int = Option(5, help='Maximum exponent of viscoity'),
    seed: int = Option(23893, help='Seed value for reproducibility'),
    delta: float = Option(1e-5, help='Internal time step for sovler'),
    b: int = Option(100, help='Batch size'),
    force: Force = Option(Force.li, help='Type of forcing function'),
    cycles: int = Option(2, help='Number of cycles in forcing function'),
    scaling: float = Option(0.1, help='Scaling of forcing function'),
    debug: bool = Option(False, help='Enable debugging mode with ptvsd'),
):
    # This debug mode is for those who use VS Code's internal debugger.
    if debug:
        ptvsd.enable_attach(address=('0.0.0.0', 5678))
        ptvsd.wait_for_attach()

    device = torch.device('cuda')
    torch.manual_seed(seed)
    np.random.seed(seed + 1234)

    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)

    # Set up 2d GRF with covariance parameters
    GRF = GaussianRF(2, s, alpha=2.5, tau=7, device=device)

    if force == Force.li:
        # Forcing function: 0.1*(sin(2pi(x+y)) + cos(2pi(x+y)))
        ft = torch.linspace(0, 1, s+1, device=device)
        ft = ft[0:-1]
        X, Y = torch.meshgrid(ft, ft)
        f = 0.1*(torch.sin(2 * math.pi * (X + Y)) +
                 torch.cos(2 * math.pi * (X + Y)))

    c = 0
    data_f = h5py.File(path, 'a')
    data_f.create_dataset('a', (n, s, s), np.float32)
    data_f.create_dataset('f', (n, s, s), np.float32)
    data_f.create_dataset('u', (n, s, s, steps), np.float32)
    data_f.create_dataset('mu', (n,), np.float32)

    b = min(n, b)
    with torch.no_grad():
        for j in range(n // b):
            print('batch', j)
            w0 = GRF.sample(b)

            if force == Force.random:
                f = get_random_force(b, s, device, cycles, scaling)

            if k_min != k_max:
                k = np.random.rand(b) * (k_max - k_min) + k_min
                mu = 10.0**-k

            sol, _ = solve_navier_stokes_2d(w0, f, mu, t, delta, steps)
            data_f['a'][c:(c+b), ...] = w0.cpu().numpy()
            data_f['u'][c:(c+b), ...] = sol.cpu().numpy()

            if force == Force.random:
                data_f['f'][c:(c+b), ...] = f.cpu().numpy()

            data_f['mu'][c:(c+b)] = mu

            c += b


if __name__ == "__main__":
    app()
