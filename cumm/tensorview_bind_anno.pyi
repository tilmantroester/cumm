from typing import List, Tuple, Union, overload

import numpy as np


class Tensor:
    @overload
    def __init__(self):
        ...

    @overload
    def __init__(self,
                 shape: Union[List[int], Tuple[int]],
                 dtype: int = 0,
                 device: int = -1,
                 pinned: bool = False,
                 managed: bool = False):
        ...

    @property
    def shape(self) -> List[int]:
        ...

    @property
    def stride(self) -> List[int]:
        ...

    @property
    def dtype(self) -> int:
        ...

    @property
    def size(self) -> int:
        ...

    @property
    def itemsize(self) -> int:
        ...

    @property
    def ndim(self) -> int:
        ...

    @property
    def device(self) -> int:
        ...

    def pinned(self) -> bool:
        ...

    def empty(self) -> bool:
        ...

    def dim(self, axis: int) -> int:
        ...

    def slice_first_axis(self, start: int, end: int) -> "Tensor":
        ...

    def view(self, views: List[int]) -> "Tensor":
        ...

    def clone(self,
              pinned: bool = False,
              use_cpu_copy: bool = False) -> "Tensor":
        ...

    def unsqueeze(self, axis: int) -> "Tensor":
        ...

    @overload
    def squeeze(self) -> "Tensor":
        ...

    @overload
    def squeeze(self, axis: int) -> "Tensor":
        ...

    def __getitem__(self, idx: int) -> "Tensor":
        ...

    def numpy(self) -> np.ndarray:
        ...

    def numpy_view(self) -> np.ndarray:
        ...

    @overload
    def cpu(self) -> "Tensor":
        ...

    @overload
    def cpu(self, stream_handle: int) -> "Tensor":
        ...

    @overload
    def copy_(self, other: "Tensor") -> None:
        ...

    @overload
    def copy_(self, other: "Tensor", stream_handle: int) -> None:
        ...

    @overload
    def zero_(self) -> "Tensor":
        ...

    @overload
    def zero_(self, stream_handle: int) -> "Tensor":
        ...

    @overload
    def cuda(self) -> "Tensor":
        ...

    @overload
    def cuda(self, stream_handle: int) -> "Tensor":
        ...

    @overload
    def fill_int_(self, val: Union[int, float]) -> "Tensor":
        ...

    @overload
    def fill_int_(self, val: Union[int, float],
                  stream_handle: int) -> "Tensor":
        ...

    @overload
    def fill_float_(self, val: Union[int, float]) -> "Tensor":
        ...

    @overload
    def fill_float_(self, val: Union[int, float],
                    stream_handle: int) -> "Tensor":
        ...

    def byte_pointer(self) -> int:
        ...


def zeros(shape: List[int],
          dtype: Union[np.dtype, int] = np.float32,
          device: int = -1,
          pinned: bool = False,
          managed: bool = False) -> Tensor:
    ...


def from_blob(ptr: int,
              shape: List[int],
              dtype: Union[np.dtype, int] = np.float32,
              device: int = -1) -> Tensor:
    ...


def from_const_blob(ptr: int,
                    shape: List[int],
                    dtype: Union[np.dtype, int] = np.float32,
                    device: int = -1) -> Tensor:
    ...


def empty(shape: List[int],
          dtype: Union[np.dtype, int] = np.float32,
          device: int = -1,
          pinned: bool = False,
          managed: bool = False) -> Tensor:
    ...


def full(shape: List[int],
         val: Union[int, float],
         dtype: Union[np.dtype, int] = np.float32,
         device: int = -1,
         pinned: bool = False,
         managed: bool = False) -> Tensor:
    ...


def zeros_managed(shape: List[int],
                  dtype: Union[np.dtype, int] = np.float32) -> Tensor:
    ...


def from_numpy(arr: np.ndarray) -> Tensor:
    ...