import math
from typing import Any, Dict

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
import wandb
from allennlp.common import Lazy
from allennlp.training.optimizers import Optimizer
from einops import rearrange, repeat
from mpl_toolkits.axes_grid1 import make_axes_locatable

from fourierflow.common import Experiment, Module, Scheduler
from fourierflow.modules import fourier_encode
from fourierflow.modules.loss import LpLoss


@Experiment.register('fourier_2d_single')
class Fourier2DSingleExperiment(Experiment):
    def __init__(self,
                 conv: Module,
                 optimizer: Lazy[Optimizer],
                 n_steps: int,
                 max_freq: int = 32,
                 num_freq_bands: int = 8,
                 freq_base: int = 2,
                 scheduler: Lazy[Scheduler] = None,
                 scheduler_config: Dict[str, Any] = None,
                 low: float = 0,
                 high: float = 1,
                 use_position: bool = True,
                 use_fourier_position: bool = False):
        super().__init__()
        self.conv = conv
        self.n_steps = n_steps
        self.l2_loss = LpLoss(size_average=True)
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.scheduler_config = scheduler_config
        self.use_fourier_position = use_fourier_position
        self.use_position = use_position
        self.max_freq = max_freq
        self.num_freq_bands = num_freq_bands
        self.freq_base = freq_base
        self.low = low
        self.high = high
        self.register_buffer('_float', torch.FloatTensor([0.1]))

    def forward(self, x):
        x = self.conv(x)
        return x.squeeze()

    def encode_positions(self, dim_sizes, low=-1, high=1, fourier=True):
        # dim_sizes is a list of dimensions in all positional/time dimensions
        # e.g. for a 64 x 64 image over 20 steps, dim_sizes = [64, 64, 20]

        # A way to interpret `pos` is that we could append `pos` directly
        # to the raw inputs to attach the positional info to the raw features.
        def generate_grid(size):
            return torch.linspace(low, high, steps=size,
                                  device=self._float.device)
        grid_list = list(map(generate_grid, dim_sizes))
        pos = torch.stack(torch.meshgrid(*grid_list), dim=-1)
        # pos.shape == [*dim_sizes, n_dims]

        if not fourier:
            return pos

        # To get the fourier encodings, we will go one step further
        fourier_feats = fourier_encode(
            pos, self.max_freq, self.num_freq_bands, base=self.freq_base)
        # fourier_feats.shape == [*dim_sizes, n_dims, n_bands * 2 + 1]

        fourier_feats = rearrange(fourier_feats, '... n d -> ... (n d)')
        # fourier_feats.shape == [*dim_sizes, pos_size]

        return fourier_feats

    def _training_step(self, batch):
        x, y = batch
        B, *dim_sizes, _ = x.shape
        # data.shape == [batch_size, *dim_sizes]

        if self.use_position:
            pos_feats = self.encode_positions(
                dim_sizes, self.low, self.high, self.use_fourier_position)
            # pos_feats.shape == [*dim_sizes, pos_size]

            pos_feats = repeat(pos_feats, '... -> b ...', b=B)
            # pos_feats.shape == [batch_size, *dim_sizes, n_dims]

            x = torch.cat([x, pos_feats], dim=-1)
            # xx.shape == [batch_size, *dim_sizes, 3]

        im, _, _ = self.conv(x)
        # im.shape == [batch_size * time, *dim_sizes, 1]

        BN = im.shape[0]
        loss = self.l2_loss(im.reshape(BN, -1), y.reshape(BN, -1))

        return loss

    def _valid_step(self, data, split):
        B, *dim_sizes, T = data.shape
        X, Y = dim_sizes
        # data.shape == [batch_size, *dim_sizes, total_steps]

        xx = repeat(data, '... -> ... 1')
        # xx.shape == [batch_size, *dim_sizes, total_steps, 1]

        if self.use_position:
            pos_feats = self.encode_positions(
                dim_sizes, self.low, self.high, self.use_fourier_position)
            # pos_feats.shape == [*dim_sizes, pos_size]

            pos_feats = repeat(pos_feats, '... -> b ...', b=B)
            # pos_feats.shape == [batch_size, *dim_sizes, n_dims]

            all_pos_feats = repeat(pos_feats, '... e -> ... t e', t=T)

            xx = torch.cat([xx, all_pos_feats], dim=-1)
            # xx.shape == [batch_size, *dim_sizes, total_steps, 3]

        xx = xx[..., -self.n_steps-1:-1, :]
        # xx.shape == [batch_size, *dim_sizes, n_steps, 3]

        yy = data[:, ..., -self.n_steps:]
        # yy.shape == [batch_size, *dim_sizes, n_steps]

        loss = 0
        # We predict one future one step at a time
        pred_layer_list = []
        out_fts_list = []
        for t in range(self.n_steps):
            if t == 0:
                x = xx[..., t, :]
            elif self.use_position:
                x = torch.cat([im, pos_feats], dim=-1)
            else:
                x = im
            # x.shape == [batch_size, *dim_sizes, 3]

            im, im_list, out_fts = self.conv(x)
            # im.shape == [batch_size, *dim_sizes, 1]

            out_fts_list.append(out_fts)

            y = yy[..., t]
            loss += self.l2_loss(im.reshape(B, -1), y.reshape(B, -1))
            pred = im if t == 0 else torch.cat((pred, im), dim=-1)
            pred_layer_list.append(im_list)

        loss /= self.n_steps
        loss_full = self.l2_loss(pred.reshape(B, -1), yy.reshape(B, -1))

        return loss, loss_full, pred, pred_layer_list, out_fts_list

    def _test_step(self, data, split):
        B, *dim_sizes, T = data.shape
        X, Y = dim_sizes
        # data.shape == [batch_size, *dim_sizes, total_steps]

        xx = repeat(data, '... -> ... 1')
        # xx.shape == [batch_size, *dim_sizes, total_steps, 1]

        if self.use_position:
            pos_feats = self.encode_positions(
                dim_sizes, self.low, self.high, self.use_fourier_position)
            # pos_feats.shape == [*dim_sizes, pos_size]

            pos_feats = repeat(pos_feats, '... -> b ...', b=B)
            # pos_feats.shape == [batch_size, *dim_sizes, n_dims]

            all_pos_feats = repeat(pos_feats, '... e -> ... t e', t=T)

            xx = torch.cat([xx, all_pos_feats], dim=-1)
            # xx.shape == [batch_size, *dim_sizes, total_steps, 3]

        xx = xx[..., :1, :]
        # xx.shape == [batch_size, *dim_sizes, n_steps, 3]

        yy = data[:, ..., 1:]
        # yy.shape == [batch_size, *dim_sizes, n_steps]

        loss = 0
        # We predict one future one step at a time
        pred_layer_list = []
        out_fts_list = []
        for t in range(19):
            if t == 0:
                x = xx[..., t, :]
            elif self.use_position:
                x = torch.cat([im, pos_feats], dim=-1)
            else:
                x = im
            # x.shape == [batch_size, *dim_sizes, 3]

            im, im_list, out_fts = self.conv(x)
            # im.shape == [batch_size, *dim_sizes, 1]

            out_fts_list.append(out_fts)
            y = yy[..., t]
            l = self.l2_loss(im.reshape(B, -1), y.reshape(B, -1))
            loss += l
            if t in [0, 1, 3, 7, 14, 18]:
                self.log(f'test_loss_{t + 1}', l)
            pred = im if t == 0 else torch.cat((pred, im), dim=-1)
            pred_layer_list.append(im_list)

        loss /= self.n_steps
        loss_full = self.l2_loss(pred.reshape(B, -1), yy.reshape(B, -1))

        return loss, loss_full, pred, pred_layer_list, out_fts_list

    def training_step(self, batch, batch_idx):
        loss = self._training_step(batch)
        self.log('train_loss', loss)
        return loss

    def validation_step(self, batch, batch_idx):
        loss, loss_full, preds, pred_list, _ = self._valid_step(batch, 'valid')
        self.log('valid_loss', loss)
        self.log('valid_loss_full', loss_full)

        if batch_idx == 0:
            data = batch
            expt = self.logger.experiment
            log_imshow(expt, data[0, :, :, 9], 'gt t=9')
            log_imshow(expt, data[0, :, :, 19], 'gt t=19')
            log_imshow(expt, preds[0, :, :, -1], 'pred t=19')

            layers = pred_list[-1]
            if layers:
                for i, layer in enumerate(layers):
                    log_imshow(expt, layer[0], f'layer {i} t=19')

    def test_step(self, batch, batch_idx):
        loss, loss_full, preds, pred_list, out_fts_list = self._valid_step(
            batch, 'test')
        # loss, loss_full, _, _ = self._test_step(batch, 'test')
        self.log('test_loss', loss)
        self.log('test_loss_full', loss_full)

        # import pickle
        # with open('R.pkl', 'wb') as f:
        #     pickle.dump(out_fts_list, f)

        # if batch_idx == 0:
        #     expt = self.logger.experiment
        #     log_imshow(expt, torch.sqrt(
        #         out_fts_list[0][0][0].real**2 + out_fts_list[0][0][0].imag**2), 'layer 0')
        #     log_imshow(expt, torch.sqrt(
        #         out_fts_list[0][1][0].real**2 + out_fts_list[0][1][0].imag**2), 'layer 1')


class MidpointNormalize(mpl.colors.Normalize):
    """See https://stackoverflow.com/a/50003503/3790116."""

    def __init__(self, vmin, vmax, midpoint=0, clip=False):
        self.midpoint = midpoint
        mpl.colors.Normalize.__init__(self, vmin, vmax, clip)

    def __call__(self, value, clip=None):
        normalized_min = max(
            0, 1 / 2 * (1 - abs((self.midpoint - self.vmin) / (self.midpoint - self.vmax))))
        normalized_max = min(
            1, 1 / 2 * (1 + abs((self.vmax - self.midpoint) / (self.midpoint - self.vmin))))
        normalized_mid = 0.5
        x, y = [self.vmin, self.midpoint, self.vmax], [
            normalized_min, normalized_mid, normalized_max]
        return np.ma.masked_array(np.interp(value, x, y))


def log_imshow(expt, tensor, name):
    fig = plt.figure(figsize=(6, 6))
    ax = fig.add_subplot(1, 1, 1)
    vals = tensor.cpu().numpy()
    vmax = vals.max()
    # vmin = 0  # -1 if 'layer' in name else -3
    # vmax = 10  # 1 if 'layer' in name else 3
    # norm = MidpointNormalize(vmin=0, vmax=10, midpoint=0)
    im = ax.imshow(vals, interpolation='bilinear',
                   cmap=plt.get_cmap('Reds'))
    divider = make_axes_locatable(ax)
    cax = divider.append_axes('right', size='5%', pad=0.05)
    fig.colorbar(im, cax=cax, orientation='vertical')
    fig.tight_layout()
    expt.log({f'{name}': wandb.Image(fig)})
    plt.close('all')
