from __future__ import annotations

from functools import partial
from typing import Callable, List, Optional, Tuple

import torch
from torch import Tensor, nn
from torch.nn import functional as F

from .utils import _irfft, _rfft, _svd, fftfreq

def _check_shape_equal(x: Tensor,
                       y: Tensor,
                       dim: int
                       ):
    if x.size(dim) != y.size(dim):
        raise ValueError(f'x.size({dim}) == y.size({dim}) is expected, but got {x.size(dim)=}, {y.size(dim)=} instead.')

def _zero_mean(input: Tensor,
               dim: int
               ) -> Tensor:
    return input - input.mean(dim=dim, keepdim=True)

def _matrix_normalize(input: Tensor,
                      dim: int
                      ) -> Tensor:
    """
    Center and normalize according to the forbenius norm of the centered data.

    Note:
        - this does not create standardized random variables in a random vectors.
    ref:
        - https://stats.stackexchange.com/questions/544812/how-should-one-normalize-activations-of-batches-before-passing-them-through-a-si
    :param input:
    :param dim:
    :return:
    """
    from torch.linalg import norm
    X_centered: Tensor = _zero_mean(input, dim=dim)
    X_star: Tensor = X_centered / norm(X_centered, "fro")
    return X_star

def cca_by_svd(x: Tensor,
               y: Tensor
               ) -> Tuple[Tensor, Tensor, Tensor]:
    """ CCA using only SVD.
    For more details, check Press 2011 "Canonical Correlation Clarified by Singular Value Decomposition"

    Args:
        x: input tensor of Shape NxD1
        y: input tensor of shape NxD2

    Returns: x-side coefficients, y-side coefficients, diagonal

    """

    # torch.svd(x)[1] is vector
    u_1, s_1, v_1 = _svd(x)
    u_2, s_2, v_2 = _svd(y)
    uu = u_1.t() @ u_2
    u, diag, v = _svd(uu)
    # v @ s.diag() = v * s.view(-1, 1), but much faster
    a = (v_1 * s_1.reciprocal_().unsqueeze_(0)) @ u
    b = (v_2 * s_2.reciprocal_().unsqueeze_(0)) @ v
    return a, b, diag


def cca_by_qr(x: Tensor,
              y: Tensor
              ) -> Tuple[Tensor, Tensor, Tensor]:
    """ CCA using QR and SVD.
    For more details, check Press 2011 "Canonical Correlation Clarified by Singular Value Decomposition"

    Args:
        x: input tensor of Shape NxD1
        y: input tensor of shape NxD2

    Returns: x-side coefficients, y-side coefficients, diagonal

    """
    q_1, r_1 = torch.linalg.qr(x)
    q_2, r_2 = torch.linalg.qr(y)
    qq = q_1.t() @ q_2
    u, diag, v = _svd(qq)
    a = r_1.inverse() @ u
    b = r_2.inverse() @ v
    return a, b, diag


def cca(x: Tensor,
        y: Tensor,
        backend: str
        ) -> Tuple[Tensor, Tensor, Tensor]:
    """ Compute CCA, Canonical Correlation Analysis

    Args:
        x: input tensor of Shape NxD1
        y: input tensor of Shape NxD2
        backend: svd or qr

    Returns: x-side coefficients, y-side coefficients, diagonal

    """
    assert(x.size(0) == y.size(0)), f'Traditional CCA needs same number of data points for both data matrices' \
                                    f'for it to work but got {x.size(0)=} and {y.size(0)=}'
    if x.size(0) < x.size(1) or y.size(0) < y.size(1):
        import logging
        logging.warning(f'Warning, you have less data points than features in one of your data matrices: '
                        f'{x.size()}, {y.size()}')
    # _check_shape_equal(x, y, dim=0)  # check first dim are equal

    # if x.size(0) != y.size(0):
    #     raise ValueError(f'x.size(0) == y.size(0) is expected, but got {x.size(0)=}, {y.size(0)=} instead.')
    #
    # if x.size(0) < x.size(1):
    #     raise ValueError(f'x.size(0) >= x.size(1) is expected, but got {x.size()=}.')
    #
    # if y.size(0) < y.size(1):
    #     raise ValueError(f'y.size(0) >= y.size(1) is expected, but got {y.size()=}.')

    if backend not in ('svd', 'qr'):
        raise ValueError(f'backend is svd or qr, but got {backend}')

    # x = _matrix_normalize(x, dim=0)
    # y = _matrix_normalize(y, dim=0)
    x = _zero_mean(x, dim=0)
    y = _zero_mean(y, dim=0)
    return cca_by_svd(x, y) if backend == 'svd' else cca_by_qr(x, y)


def _svd_reduction(input: Tensor,
                   accept_rate: float
                   ) -> Tensor:
    """
    Outputs the SV part of SVCCA, i.e. it does the dimensionality reduction of SV by removing neurons such that
    accept_rate (e.g. 0.99) of variance is kept.

    Note:
        - this might make your sanity check for SVCCA never have N < D since it might reduce the
        dimensions automatically such that there is less dimensions/neurons in [N, D'].

    :param input:
    :param accept_rate:
    :return:
    """
    left, diag, right = _svd(input)
    full = diag.abs().sum()
    ratio = diag.abs().cumsum(dim=0) / full
    num = torch.where(ratio < accept_rate,
                      input.new_ones(1, dtype=torch.long),
                      input.new_zeros(1, dtype=torch.long)
                      ).sum()
    return input @ right[:, : num]


def svcca_distance(x: Tensor,
                   y: Tensor,
                   accept_rate: float,
                   backend: str
                   ) -> Tensor:
    """ Singular Vector CCA proposed in Raghu et al. 2017.

    Args:
        x: input tensor of Shape NxD1, where it's recommended that N>Di
        y: input tensor of Shape NxD2, where it's recommended that N>Di
        accept_rate: 0.99
        backend: svd or qr

    Returns:

    """

    x = _svd_reduction(x, accept_rate)
    y = _svd_reduction(y, accept_rate)
    div = min(x.size(1), y.size(1))
    a, b, diag = cca(x, y, backend)
    return 1 - diag.sum() / div


def pwcca_distance(x: Tensor,
                   y: Tensor,
                   backend: str
                   ) -> Tensor:
    """ Projection Weighted CCA proposed in Marcos et al. 2018.

    Args:
        x: input tensor of Shape NxD1, where it's recommended that N>Di
        y: input tensor of Shape NxD2, where it's recommended that N>Di
        backend: svd or qr

    Returns:

    """

    a, b, diag = cca(x, y, backend)
    alpha = (x @ a).abs_().sum(dim=0)
    alpha /= alpha.sum()
    return 1 - alpha @ diag


def _debiased_dot_product_similarity(z: Tensor,
                                     sum_row_x: Tensor,
                                     sum_row_y: Tensor,
                                     sq_norm_x: Tensor,
                                     sq_norm_y: Tensor,
                                     size: int
                                     ) -> Tensor:
    return (z
            - size / (size - 2) * (sum_row_x @ sum_row_y)
            + sq_norm_x * sq_norm_y / ((size - 1) * (size - 2)))


def linear_cka_distance(x: Tensor,
                        y: Tensor,
                        reduce_bias: bool
                        ) -> Tensor:
    """ Linear CKA used in Kornblith et al. 19
    
    Args:
        x: input tensor of Shape NxD1
        y: input tensor of Shape NxD2
        reduce_bias: debias CKA estimator, which might be helpful when D is limited

    Returns:

    """
    # _check_shape_equal(x, y, 0)
    x = _zero_mean(x, dim=0)
    y = _zero_mean(y, dim=0)
    # x = _matrix_normalize(x, dim=0)
    # y = _matrix_normalize(y, dim=0)

    if x.size(0) != y.size(0):
        raise ValueError(f'x.size(0) == y.size(0) is expected, but got {x.size(0)=}, {y.size(0)=} instead.')

    dot_prod = (y.t() @ x).norm('fro').pow(2)
    norm_x = (x.t() @ x).norm('fro')
    norm_y = (y.t() @ y).norm('fro')

    if reduce_bias:
        size = x.size(0)
        # (x @ x.t()).diag()
        sum_row_x = torch.einsum('ij,ij->i', x, x)
        sum_row_y = torch.einsum('ij,ij->i', y, y)
        sq_norm_x = sum_row_x.sum()
        sq_norm_y = sum_row_y.sum()
        dot_prod = _debiased_dot_product_similarity(dot_prod, sum_row_x, sum_row_y, sq_norm_x, sq_norm_y, size)
        norm_x = _debiased_dot_product_similarity(norm_x.pow_(2), sum_row_x, sum_row_y, sq_norm_x, sq_norm_y, size)
        norm_y = _debiased_dot_product_similarity(norm_y.pow_(2), sum_row_x, sum_row_y, sq_norm_x, sq_norm_y, size)
    return 1 - dot_prod / (norm_x * norm_y)

def orthogonal_procrustes_distance(x: Tensor,
                                   y: Tensor,
                                   ) -> Tensor:
    """ Orthogonal Procrustes distance used in Ding+21.
    Returns in dist interval [0, 1].

    Note:
        -  for a raw representation A we first subtract the mean value from each column, then divide
    by the Frobenius norm, to produce the normalized representation A* , used in all our dissimilarity computation.
        - see uutils.torch_uu.orthogonal_procrustes_distance to see my implementation
    Args:
        x: input tensor of Shape NxD1
        y: input tensor of Shape NxD2
    Returns:
    """
    # _check_shape_equal(x, y, 0)
    nuclear_norm = partial(torch.linalg.norm, ord="nuc")

    x = _matrix_normalize(x, dim=0)
    y = _matrix_normalize(y, dim=0)
    # note: ||x||_F = 1, ||y||_F = 1
    # - note this already outputs it between [0, 1] e.g. it's not 2 - 2 nuclear_norm(<x1, x2>) due to 0.5*d_proc(x, y)
    return 1 - nuclear_norm(x.t() @ y)

class SimilarityHook(object):
    """ Hook to compute CCAs and CKA distance between modules in given models ::

        model = resnet18()
        hook1 = SimilarityHook(model, "layer3.0.conv1")
        hook2 = SimilarityHook(model, "layer3.0.conv2")
        model.eval()
        with torch.no_grad():
            for _ in range(10):
                model(torch.randn(120, 3, 224, 224))
        hook1.distance(hook2, size=8)

    Args:
        model: Model
        name: Name of module appeared in `model.named_modules()`
        cca_distance: the method to compute CCA and CKA distance. By default, PWCCA is used.
        'pwcca', 'svcca', 'lincka' or partial functions such as `partial(pwcca_distance, backend='qr')` are expected.
        force_cpu: Force computation on CPUs. In some cases, CCA computation is faster on CPUs than on GPUs.
    """

    _supported_dim = (2, 4)
    _default_backends = {'pwcca': partial(pwcca_distance, backend='svd'),
                         'svcca': partial(svcca_distance, accept_rate=0.99, backend='svd'),
                         'lincka': partial(linear_cka_distance, reduce_bias=False),
                         "opd": orthogonal_procrustes_distance}

    def __init__(self,
                 model: nn.Module,
                 name: str,
                 cca_distance: Optional[str or Callable] = None,
                 force_cpu: bool = False,
                 ) -> None:

        if isinstance(model, (nn.DataParallel, nn.parallel.DistributedDataParallel)):
            raise RuntimeWarning('model is nn.DataParallel or nn.DistributedDataParallel. '
                                 'SimilarityHook may causes unexpected behavior.')

        self.model = model
        self.module = {k: v for k, v in self.model.named_modules()}[name]
        if cca_distance is None or isinstance(cca_distance, str):
            cca_distance = self._default_backends[cca_distance or 'pwcca']
        self.cca_function = cca_distance
        self.name = name
        self.force_cpu = force_cpu
        if self.force_cpu:
            # fully utilize CPUs available
            from multiprocessing import cpu_count
            torch.set_num_threads(cpu_count())

        self.device = None
        self._hooked_tensors = None
        self._register_hook()

    def _register_hook(self
                       ) -> None:
        def hook(*args):
            output = args[2]
            if output.dim() not in self._supported_dim:
                raise RuntimeError(f'CCAHook currently supports tensors of dimension {self._supported_dim}, '
                                   f'but got {output.dim()} instead.')

            self.device = output.device
            # store intermediate tensors on CPU to avoid possible CUDA OOM
            output = output.cpu()

            if self._hooked_tensors is None:
                self._hooked_tensors = output
            else:
                self._hooked_tensors = torch.cat([self._hooked_tensors, output], dim=0).contiguous()

        self.module.register_forward_hook(hook)

    def clear(self
              ) -> None:
        """ Clear stored tensors
        """

        self._hooked_tensors = None

    @property
    def hooked_tensors(self
                       ) -> Tensor:
        if self._hooked_tensors is None:
            raise RuntimeError('Run model in advance')
        return self._hooked_tensors

    @staticmethod
    def create_hooks(model: nn.Module,
                     names: List[str],
                     cca_distance: Optional[str or Callable] = None,
                     force_cpu: bool = False,
                     ) -> List[SimilarityHook]:
        """ Create list of hooks from names. ::

        >>> hooks1 = SimilarityHook.create_hooks(model1, ['name1', ...])
        >>> hooks2 = SimilarityHook.create_hooks(model2, ['name1', ...])
        >>> with torch.no_grad():
        >>>    model1(input)
        >>>    model2(input)
        >>> [[hook1.distance(hook2) for hook2 in hooks2] for hook1 in hooks1]

        Args:
            model: Model
            names: List of names of module appeared in `model.named_modules()`
            cca_distance: the method to compute CCA and CKA distance. By default, PWCCA is used.
            'pwcca', 'svcca', 'lincka' or partial functions such as `partial(pwcca_distance, backend='qr')` are expected.
            force_cpu: Force computation on CPUs. In some cases, CCA computation is faster on CPUs than on GPUs.

        Returns: List of hooks

        """

        return [SimilarityHook(model, name, cca_distance, force_cpu) for name in names]

    def distance(self,
                 other: SimilarityHook,
                 *,
                 downsample_method: Optional[str] = 'avg_pool',
                 size: Optional[int] = None,
                 effecive_neuron_type: str = 'filter'
                 ) -> float:
        """ Compute CCA distance between self and other.

        Args:
            other: Another hook
            downsample_method: method for downsampling. avg_pool or dft.
            size: size of the feature map after downsampling
            effecive_neuron_type: how to define a nueron. Default filter means each filter is a neuron so each patch of
                data is a data point. Yields [M, C, H, W] -> [MHW -> C].
                Each neuron vector will have size [MHW, 1] for each filter/neuron.
                Other option activation means a neuron is one of the CHW activations. Yields [M, C, H, W] -> [M, CHW]
                Each neuron vector will have size [M, 1] for each activation/neuron.

        Returns: returns distance

        """
        self_tensor = self.hooked_tensors
        other_tensor = other.hooked_tensors
        if not self.force_cpu:
            # hooked_tensors is CPU tensor, so need to device
            self_tensor = self_tensor.to(self.device)
            other_tensor = other_tensor.to(self.device)
        if self_tensor.size(0) != other_tensor.size(0):
            raise RuntimeError('0th dimension of hooked tensors are different')
        if self_tensor.dim() != other_tensor.dim():
            raise RuntimeError('Dimensions of hooked tensors need to be same, '
                               f'but got {self_tensor.dim()=} and {other_tensor.dim()=}')

        if self_tensor.dim() == 2:   # - output of FCNN so it's a matrix e.g. [N, D]
            return self.cca_function(self_tensor, other_tensor).item()
        else:
            if effecive_neuron_type == 'filter':
                # - [M, C, H, W] -> [M*H*W, C] = [N, D]
                pass
            elif effecive_neuron_type == 'activation':
                # - [M, C, H, W] -> [M, C*H*W] = [N, D]
                assert False, 'Not tested'
                # improvement: [M, C, H, W] -> [M, C*H*W]
                self_tensor = self_tensor.flatten(start_dim=3, end_dim=-1).contiguous()
                other_tensor = other_tensor.flatten(start_dim=3, end_dim=-1).contiguous()
                return self.cca_function(self_tensor, other_tensor).item()
            elif effecive_neuron_type == 'original_anatome':
                dist: float = distance_cnn_original_anatome(self, size, downsample_method, self_tensor, other_tensor)
                return dist
            else:
                raise ValueError(f'Invalid effecive_neuron_type: {effecive_neuron_type=}')

    @staticmethod
    def _downsample_4d(input: Tensor,
                       size: int,
                       backend: str
                       ) -> Tensor:
        """
        Downsample 4D tensor of BxCxHxD [B, C, H, D] to 3D tensor of {size^2}xBxC [size^2, B, C]
        """

        if input.dim() != 4:
            raise RuntimeError(f'input is expected to be 4D tensor, but got {input.dim()=}.')

        # todo: what if channel-last?
        b, c, h, w = input.size()

        if (size, size) == (h, w):
            return input.flatten(2).permute(2, 0, 1)

        if (size, size) > (h, w):
            raise RuntimeError(f'size is expected to be smaller than h or w, but got {h=}, {w=}.')

        if backend not in ('avg_pool', 'dft'):
            raise RuntimeError(f'backend is expected to be avg_pool or dft, but got {backend=}.')

        if backend == 'avg_pool':
            input = F.adaptive_avg_pool2d(input, (size, size))

        else:
            # almost PyTorch implant of
            # https://github.com/google/svcca/blob/master/dft_ccas.py
            if input.size(2) != input.size(3):
                raise RuntimeError('width and height of input needs to be equal')
            h = input.size(2)
            input_fft = _rfft(input, 2, normalized=True, onesided=False)
            freqs = fftfreq(h, 1 / h, device=input.device)
            idx = (freqs >= -size / 2) & (freqs < size / 2)
            # BxCxHxWx2 -> BxCxhxwx2
            input_fft = input_fft[..., idx, :][..., idx, :, :]
            input = _irfft(input_fft, 2, normalized=True, onesided=False)

        # BxCxHxW -> (HW)xBxC
        input = input.flatten(2).permute(2, 0, 1)
        return input


# - for backward compatibility
# SimilarityHook = DistanceHook
# - comment this out once the forward merge has been done
DistanceHook = SimilarityHook

def original_computation_of_distance_from_Ryuichiro_Hataya(self: DistanceHook,
                                                           self_tensor: Tensor,
                                                           other_tensor: Tensor) -> float:
    return torch.stack([self.cca_function(s, o)
                        for s, o in zip(self_tensor.unbind(), other_tensor.unbind())
                        ]
                       ).mean().item()

def original_computation_of_distance_from_Ryuichiro_Hataya_as_loop(self: DistanceHook,
                                                                   self_tensor: Tensor,
                                                                   other_tensor: Tensor) -> float:
    """
    Get the distance between two layer matrices by considering each individual data point on it's own.
    i.e. we consider [M, F, HW] that we have M data points and the matrix of size [F, HW] for each of them.


    Note:
        - complexity is O(M) * O(gpu([F, HW])
    ref:
        - unbind: https://pytorch.org/docs/stable/generated/torch.unbind.html
        - see original implementation: original_computation_of_distance_from_Ryuichiro_Hataya
        - original computedation for (sv/pwd)cca: https://github.com/brando90/svcca/blob/master/tutorials/002_CCA_for_Convolutional_Layers.ipynb

    :param self:
    :param self_tensor:
    :param other_tensor:
    :return:
    """
    assert (self_tensor.dim() == 3), f'Expects a flattened conv layer tensor so a tensor of 3 dims but got: {self_tensor.size()}'
    M, F, HW = self_tensor.size()
    # - remove the first dimension to get a list of all the tensors [M, F, HW] -> list([F, HW]) of M elements
    self_tensor: tuple[Tensor] = self_tensor.unbind()
    other_tensor: tuple[Tensor] = other_tensor.unbind()
    assert (len(self_tensor) == M and len(other_tensor) == M)
    # - for each of the M data points, compute the distance/similarity,
    dists_for_wrt_entire_data_points: list[float] = []
    for m in range(M):
        s, o = self_tensor[m], other_tensor[m]
        dist: float = self.cca_function(s, o)
        dists_for_wrt_entire_data_points.append(dist)
    # - compute dist
    # return it to [M, F, HW]
    dists_for_wrt_entire_data_points: Tensor = torch.stack(dists_for_wrt_entire_data_points)
    dist: float = dists_for_wrt_entire_data_points.mean().item()
    return dist

def downsampling_original_anatome(self: DistanceHook, self_tensor: Tensor, other_tensor: Tensor,
                                  downsample_method: Optional[str], size: Optional[int]):
    # - convolution layer [M, C, H, W]
    if size is None:
        # no downsampling: [M, C, H, W] -> [M, C, H*W]
        # flatten(2) -> flatten from 2 to -1 (end)
        self_tensor = self_tensor.flatten(2).contiguous()
        other_tensor = other_tensor.flatten(2).contiguous()
    else:
        # do downsampling: [M, C, H, W] -> [size^2, B, C]
        downsample_method = downsample_method or 'avg_pool'
        self_tensor = self._downsample_4d(self_tensor, size, downsample_method)
        other_tensor = self._downsample_4d(other_tensor, size, downsample_method)
    # - compute distance = 1.0 - sim
    return torch.stack([self.cca_function(s, o)
                        for s, o in zip(self_tensor.unbind(), other_tensor.unbind())
                        ]
                       ).mean().item()

def distance_cnn_original_anatome(self: DistanceHook, size: Optional[int],
             downsample_method: Optional[str],
             self_tensor: Tensor,
             other_tensor: Tensor
             ) -> float:
    """
    Original anatome.
    """
    # - convolution layer [M, C, H, W]
    if size is None:
        # - approximates each activation is a neuron (idk why C as size of data ever makes sense)
        # neuron vec of size [C, 1]
        # M layer matrices of dim [C, H*W] = [N, D]
        # - each spatial dimension is a neuron and each filter is a data point.
        # should be [M, C*H*W] imho.
        # - no downsampling: [M, C, H, W] -> [M, C, H*W]
        # flatten(2) -> flatten from 2 to -1 (end)
        self_tensor = self_tensor.flatten(start_dim=2, end_dim=-1).contiguous()
        other_tensor = other_tensor.flatten(start_dim=2, end_dim=-1).contiguous()
    else:
        # - each filter is a neuron of size B. (this one is sort of consistent with number of true data being the size of a neuron)
        # neuron vec of size [B, 1]
        # size^2 layer matrices of dim [B, C] = [N, D]
        # do downsampling: [M, C, H, W] -> [size^2, B, C]
        downsample_method = downsample_method or 'avg_pool'
        self_tensor = self._downsample_4d(self_tensor, size, downsample_method)
        other_tensor = self._downsample_4d(other_tensor, size, downsample_method)
    # - compute distance = 1.0 - sim
    # loop through each of the M or size^2 data matrices of size [N_eff, D_eff]
    return torch.stack([self.cca_function(s, o)
                        for s, o in zip(self_tensor.unbind(), other_tensor.unbind())
                        ]
                       ).mean().item()