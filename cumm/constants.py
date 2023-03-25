# Copyright 2021 Yan Yan
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from pathlib import Path
from typing import List

from ccimport import compat

PACKAGE_NAME = "cumm"
PACKAGE_ROOT = Path(__file__).parent.resolve()

_TENSORVIEW_INCLUDE_PATHS: List[Path] = [
    PACKAGE_ROOT.parent / "include",  # pip dev install
    PACKAGE_ROOT / "include",  # pip package
]

TENSORVIEW_INCLUDE_PATH = _TENSORVIEW_INCLUDE_PATHS[0]
if not TENSORVIEW_INCLUDE_PATH.exists():
    for p in _TENSORVIEW_INCLUDE_PATHS[1:]:
        if p.exists():
            TENSORVIEW_INCLUDE_PATH = p

assert TENSORVIEW_INCLUDE_PATH.exists()

TENSORVIEW_INCLUDE_PATH = str(TENSORVIEW_INCLUDE_PATH)
TENSORVIEW_INCLUDE_PATH = os.getenv("CUMM_INCLUDE_PATH", TENSORVIEW_INCLUDE_PATH)

CUTLASS_MODE = False
CUTLASS_INPUT_ITER = CUTLASS_MODE and True
CUTLASS_SMEM_WARP_ITER = CUTLASS_MODE and True
CUTLASS_OUTPUT_ITER = CUTLASS_MODE and True
CUTLASS_DEBUG = False

CUMM_CUDA_VERSION = os.getenv("CUMM_CUDA_VERSION", None)
CUMM_CPU_ONLY_BUILD = False
if CUMM_CUDA_VERSION is not None:
    CUMM_CPU_ONLY_BUILD = CUMM_CUDA_VERSION.strip() == ""
try:
    from .__build_settings__ import __cpu_only__, __disable_jit__
    CUMM_CPU_ONLY_BUILD = __cpu_only__
    CUMM_DISABLE_JIT = __disable_jit__
except ImportError:
    CPU_ONLY = os.getenv("CPU_ONLY", None)
    if CPU_ONLY is not None:
        CUMM_CPU_ONLY_BUILD = CPU_ONLY.strip() != "0"
    CUMM_DISABLE_JIT = os.getenv("CUMM_DISABLE_JIT", "0") == "1"

CUMM_MAXIMUM_NVRTC_CONV_NDIM = 3
