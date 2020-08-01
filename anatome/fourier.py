from typing import Tuple, Callable, Optional, List

import torch
from torch import nn, Tensor
from torch.nn import functional as F
from tqdm import tqdm

from .utils import _evaluate, _normalize, _denormalize


def fft_shift(input: torch.Tensor,
              dims: Optional[Tuple[int, ...]] = None
              ) -> torch.Tensor:
    """ PyTorch version of np.fftshift

    Args:
        input: rFFTed Tensor of size [Bx]CxHxWx2
        dims:

    Returns: shifted tensor

    """

    if dims is None:
        dims = [i for i in range(1 if input.dim() == 4 else 2, input.dim() - 1)]  # H, W
    shift = [input.size(dim) // 2 for dim in dims]
    return torch.roll(input, shift, dims)


def ifft_shift(input: torch.Tensor,
               dims: Optional[Tuple[int, ...]] = None
               ) -> torch.Tensor:
    """ PyTorch version of np.ifftshift

    Args:
        input: rFFTed Tensor of size [Bx]CxHxWx2
        dims:

    Returns: shifted tensor

    """

    if dims is None:
        dims = [i for i in range(input.dim() - 2, 0 if input.dim() == 4 else 1, -1)]  # H, W
    shift = [-input.size(dim) // 2 for dim in dims]
    return torch.roll(input, shift, dims)


def fftfreq(window_length: int,
            sample_spacing: float,
            *,
            device: Optional[torch.device] = None,
            dtype: Optional[torch.dtype] = None
            ) -> torch.Tensor:
    val = 1 / (window_length * sample_spacing)
    results = torch.empty(window_length, dtype=dtype, device=device)
    n = (window_length - 1) // 2 + 1
    results[:n] = torch.arange(0, n, dtype=dtype, device=device)
    results[n:] = torch.arange(-(window_length // 2), 0, dtype=dtype, device=device)
    return results * val


def add_fourier_noise(idx: Tuple[int, int],
                      images: Tensor,
                      norm: float,
                      size: Optional[Tuple[int, int]] = None,
                      ) -> Tensor:
    """ Add Fourier noise

    Args:
        idx: index to be used
        images: original images
        norm: norm of additive noise
        size:

    Returns: images with Fourier noise

    """

    images = images.clone()

    if size is None:
        _, _, h, w = images.size()
    else:
        h, w = size

    noise = images.new_zeros(1, h, w, 2)
    noise[:, idx[0], idx[1]] = 1
    noise[:, h - 1 - idx[0], w - 1 - idx[1]] = 1
    recon = ifft_shift(noise).irfft(2, normalized=True, onesided=False).unsqueeze(0)
    recon.div_(recon.norm(p=2)).mul_(norm)
    if size is not None:
        recon = F.interpolate(recon, images.shape[2:])
    images.add_(recon).clamp_(0, 1)
    return images


@torch.no_grad()
def fourier_map(model: nn.Module,
                data: Tuple[Tensor, Tensor],
                criterion: Callable[[Tensor, Tensor], Tensor],
                norm: float,
                fourier_map_size: Optional[Tuple[int, int]] = None,
                mean: Optional[List[float] or Tensor] = None,
                std: Optional[List[float] or Tensor] = None
                ) -> Tensor:
    """

    Args:
        model: Trained model
        data: Pairs of [input, target] to compute criterion
        criterion: Criterion of (input, target) -> scalar value
        norm: Intensity of fourier noise
        fourier_map_size: Size of map, (H, W). Note that the computational time is dominated by HW.
        mean: If the range of input is [-1, 1], specify mean and std.
        std: If the range of input is [-1, 1], specify mean and std.

    Returns:

    """
    input, target = data
    if fourier_map_size is None:
        _, _, h, w = input.size()
    else:
        h, w = fourier_map_size
    if mean is not None:
        _mean = torch.as_tensor(mean, device=input.device, dtype=torch.float)
        _std = torch.as_tensor(std, device=input.device, dtype=torch.float)
        input = _denormalize(input, _mean, _std)  # [0, 1]
    map = torch.zeros(h, w)
    for u_i in tqdm(torch.triu_indices(h, w).t(), ncols=80):
        l_i = h - 1 - u_i[0], w - 1 - u_i[1]
        noisy_input = add_fourier_noise(u_i, input, norm, fourier_map_size)
        if mean is not None:
            noisy_input = _normalize(noisy_input, _mean, _std)  # to [-1, 1]
        loss = _evaluate(model, (noisy_input, target), criterion)
        map[u_i[0], u_i[1]] = loss
        map[l_i[0], l_i[1]] = loss
    return map
