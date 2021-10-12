import enum
from pathlib import Path
from cumm.gemm.algospec.core import ShuffleStrideType
from cumm import tensorview as tv 
from cumm.core_cc.csrc.arrayref import ArrayPtr
from cumm import cudasim
import numpy as np
import pccm
from cumm import dtypes
from cumm.constants import CUTLASS_MODE, CUTLASS_INPUT_ITER, CUTLASS_SMEM_WARP_ITER, CUTLASS_OUTPUT_ITER, CUTLASS_DEBUG

from cumm.gemm import (constants, layout, mask_iters,
                         out_iters, thread_map, volta_iters, volta_out_iters)
from cumm.common import (GemmBasic, GemmBasicKernel, TensorView,
                                TensorViewKernel)
from cumm.gemm import wmma
from cumm.gemm.wmma.simt import WarpMmaSimt
from typing import Dict, List, Union, Optional, Type, Tuple
from cumm.gemm.algospec import GemmAlgo, TensorOpParams, get_algo_spec
from cumm.gemm.core import metaseq, seq, MetaArray, array_type
from cumm.gemm.outputs import Output, OutputSmemStorage
from cumm.gemm.blockmma import Mma, BlockMmaStorage
from cumm.gemm.utils import GemmUtils
from cumm.gemm.algospec import bases

def div_up(a, b):
    return (a + b - 1) // b


class GemmParams(pccm.ParameterizedClass):
    def __init__(self, tile_shape: MetaArray[int], dtype_a: dtypes.DType,
                 dtype_b: dtypes.DType, dtype_c: dtypes.DType,
                 dtype_comp: dtypes.DType, trans_a: bool, trans_b: bool, trans_c: bool,
                 itera_params: mask_iters.MaskTileIteratorParams, 
                 iterb_params: mask_iters.MaskTileIteratorParams, 
                 out_params: out_iters.OutIteratorParams, 
                 have_workspace: bool = False,
                 shuffle_stride: ShuffleStrideType = ShuffleStrideType.NoShuffle):
        super().__init__()
        self.add_dependency(TensorView, GemmBasic)
        self.add_param_class("gemmutils", GemmUtils(tile_shape), "GemmUtils")
        self.itera_params = itera_params
        self.iterb_params = iterb_params
        self.out_params = out_params
        self.shuffle_stride = shuffle_stride 
        self.cutlass_a_type = ("Mma::IteratorA")
        self.cutlass_b_type = ("Mma::IteratorB")
        self.cutlass_a_param_type = self.cutlass_a_type + "::Params"
        self.cutlass_b_param_type = self.cutlass_b_type + "::Params"

        self.add_param_class("itera_p", itera_params, "IterAParams")
        self.add_param_class("iterb_p", iterb_params, "IterBParams")

        self.add_param_class("out_params_ns", out_params, "OutIterParams")

        self.tile_shape = tile_shape
        self.dtype_a = dtype_a
        self.dtype_b = dtype_b
        self.dtype_c = dtype_c
        self.dtype_comp = dtype_comp
        self.trans_a = trans_a
        self.trans_b = trans_b
        self.trans_c = trans_c

        self.add_member("m, n, k, gemm_k_size_per_split", "int")
        self.add_member("ptr_A", f"const {dtype_a}*")
        self.add_member("ptr_B", f"const {dtype_b}*")
        self.add_member("ptr_C", f"{dtype_c}*")
        self.add_member("ptr_D", f"const {dtype_c}*")
        self.add_member("alpha, beta", f"{dtype_comp}")
        self.add_member("grid_dims", f"dim3")
        self.have_workspace = have_workspace
        if have_workspace:
            self.add_member("workspace", "void*")
        if CUTLASS_INPUT_ITER:
            self.add_member("itera_params_", self.cutlass_a_param_type)
            self.add_member("iterb_params_", self.cutlass_b_param_type)
        else:
            self.add_member("itera_params_", f"IterAParams")
            self.add_member("iterb_params_", f"IterBParams")
        if CUTLASS_OUTPUT_ITER:
            self.add_member("params_C", f"Epilogue::OutputTileIterator::Params")
            self.add_member("params_D", f"Epilogue::OutputTileIterator::Params")
        else:
            self.add_member("out_params_", f"OutIterParams")


        # cudasim members
        self.m = 0
        self.n = 0
        self.k = 0
        self.gemm_k_size = 0
        self.ptr_A: Optional[ArrayPtr] = None
        self.ptr_B: Optional[ArrayPtr] = None
        self.ptr_C: Optional[ArrayPtr] = None
        self.ptr_D: Optional[ArrayPtr] = None
        self.alpha = 0
        self.beta = 0
        self.grid_dims = cudasim.Dim3(0, 0, 0)
        self.itera_params_: Optional[mask_iters.MaskTileIteratorParams] = None
        self.iterb_params_: Optional[mask_iters.MaskTileIteratorParams] = None
        self.out_params_: Optional[out_iters.OutIteratorParams] = None

    def python_ctor(self, m: int, n: int, k: int, A: ArrayPtr, B: ArrayPtr,
                    C: ArrayPtr, D: ArrayPtr, alpha: float, beta: float, split_k_slice: int = 1):
        new_obj = GemmParams(self.tile_shape, self.dtype_a, self.dtype_b,
                             self.dtype_c, self.dtype_comp, self.trans_a, self.trans_b, 
                             self.trans_c,
                             self.itera_params, self.iterb_params,
                             self.out_params)
        new_obj.grid_dims.x = mask_iters.div_up(m, new_obj.tile_shape[0])
        new_obj.grid_dims.y = mask_iters.div_up(n, new_obj.tile_shape[1])
        new_obj.grid_dims.z = split_k_slice

        total_gemm_k_iterations = mask_iters.div_up(k, new_obj.tile_shape[2])
        gemm_k_iterations = mask_iters.div_up(total_gemm_k_iterations,
                                              new_obj.grid_dims.z)
        new_obj.gemm_k_size = gemm_k_iterations * new_obj.tile_shape[2]
        print("gemm_k_iterations", k, new_obj.tile_shape[2], new_obj.grid_dims.z, gemm_k_iterations, new_obj.gemm_k_size)

        new_obj.ptr_A = A
        new_obj.ptr_B = B
        new_obj.ptr_C = C
        new_obj.ptr_D = D
        new_obj.m = m
        new_obj.n = n
        new_obj.k = k
        new_obj.alpha = alpha
        new_obj.beta = beta
        a_stride = k 
        if self.trans_a:
            a_stride = m
        b_stride = n 
        if self.trans_b:
            b_stride = k

        new_obj.itera_params_ = self.itera_params.python_ctor(a_stride)
        new_obj.iterb_params_ = self.iterb_params.python_ctor(b_stride)
        new_obj.out_params_ = self.out_params.python_ctor(n)
        return new_obj

    @pccm.cuda.constructor
    def ctor(self):
        code = pccm.FunctionCode()
        if CUTLASS_MODE:
            code.raw(f"""
            CutlassGemm::ThreadblockSwizzle threadblock_swizzle;

            cutlass::gemm::GemmCoord grid_shape = threadblock_swizzle.get_tiled_shape(
                {{m, n, k}}, 
                {{{self.tile_shape[0]}, {self.tile_shape[1]}, {self.tile_shape[2]}}},
                1);
            grid_dims.x = grid_shape.m();
            grid_dims.y = grid_shape.n();
            grid_dims.z = grid_shape.k();
            """)
        else:
            code.raw("grid_dims = get_logical_tile_count(m, n, k, split_k_slice);")
        code.raw(f"""
        // int total_gemm_k_iterations = tv::div_up(k, {self.tile_shape[2]}); // 160, 16 = 10
        // int gemm_k_iterations_per_split =
        //     tv::div_up(total_gemm_k_iterations, int(grid_dims.z)); // 10, 4 = 3
        // gemm_k_size_per_split = gemm_k_iterations_per_split * {self.tile_shape[2]}; // 3 * 16 = 48, 0-48, 48-96, 96-144, 144-192
        gemm_k_size_per_split = GemmUtils::get_gemm_k_size_per_split(k, split_k_slice);
        // tv::ssprint("gemm_k_size_per_split", m, n, k, gemm_k_size_per_split, grid_dims.x, grid_dims.y, grid_dims.z);
        """)
        if CUTLASS_INPUT_ITER:
            code.raw(f"""
            cutlass::layout::RowMajor layoutA({pccm.boolean(self.trans_a)} ? m : k);
            cutlass::layout::RowMajor layoutB({pccm.boolean(self.trans_b)} ? k : n);

            itera_params_ = {self.cutlass_a_param_type}(layoutA);
            iterb_params_ = {self.cutlass_b_param_type}(layoutB);
            """)
        else:
            if self.shuffle_stride == ShuffleStrideType.ShuffleAC:
                code.raw(f"""
                itera_params_ = IterAParams({pccm.boolean(self.trans_a)} ? m : k, IndiceA);
                iterb_params_ = IterBParams({pccm.boolean(self.trans_b)} ? k : n);
                """)
            elif self.shuffle_stride == ShuffleStrideType.ShuffleAB:
                code.raw(f"""
                itera_params_ = IterAParams({pccm.boolean(self.trans_a)} ? m : k, IndiceA);
                iterb_params_ = IterBParams({pccm.boolean(self.trans_b)} ? k : n, IndiceB);
                """)
            else:
                code.raw(f"""
                itera_params_ = IterAParams({pccm.boolean(self.trans_a)} ? m : k);
                iterb_params_ = IterBParams({pccm.boolean(self.trans_b)} ? k : n);
                """)

        if CUTLASS_OUTPUT_ITER:
            code.raw(f"""
            cutlass::layout::RowMajor layoutC(n);
            cutlass::layout::RowMajor layoutD(n);
            params_C = Epilogue::OutputTileIterator::Params(layoutC);
            params_D = Epilogue::OutputTileIterator::Params(layoutD);
            """)
        else:
            if self.shuffle_stride == ShuffleStrideType.ShuffleAC:
                code.raw("out_params_ = OutIterParams(n, IndiceC);")
            else:
                code.raw("out_params_ = OutIterParams(n);")

        code.arg("m, n, k", "int")
        code.arg("A", f" {self.dtype_a}*")
        code.arg("B", f"{self.dtype_b}*")
        code.arg("C", f"{self.dtype_c}*")
        code.arg("D", f"{self.dtype_c}*")
        if self.shuffle_stride == ShuffleStrideType.ShuffleAC:
            code.arg("IndiceA", f"const int*")
            code.arg("IndiceC", f"const int*")
        elif self.shuffle_stride == ShuffleStrideType.ShuffleAB:
            code.arg("IndiceA", f"const int*")
            code.arg("IndiceB", f"const int*")

        code.arg("alpha", f"{self.dtype_comp}", f"{self.dtype_comp}(1)")
        code.arg("beta", f"{self.dtype_comp}", f"{self.dtype_comp}(0)")
        code.arg("split_k_slice", "int", "1")
        if self.have_workspace:
            code.arg("workspace", "void*", "nullptr")
        code.ctor_init("m", "m")
        code.ctor_init("n", "n")
        code.ctor_init("k", "k")
        code.ctor_init("ptr_A", "A")
        code.ctor_init("ptr_B", "B")
        code.ctor_init("ptr_C", "C")
        code.ctor_init("ptr_D", "D")

        code.ctor_init("alpha", "alpha")
        code.ctor_init("beta", "beta")
        if self.have_workspace:
            code.ctor_init("workspace", "workspace")
        return code

    @pccm.cuda.static_function
    def get_logical_tile_count(self):
        code = pccm.FunctionCode()
        code.arg("m,n,k,split_k_slice", "int")
        code.ret("dim3")
        code.raw(f"""
        dim3 grid_dims;
        grid_dims.x = tv::div_up(m, {self.tile_shape[0]});
        grid_dims.y = tv::div_up(n, {self.tile_shape[1]});
        grid_dims.z = split_k_slice;
        return grid_dims;
        """)
        return code 


class GemmKernel(pccm.ParameterizedClass):
    def __init__(self,
                 tile_shape: MetaArray[int],
                 warp_tile_shape: MetaArray[int],
                 num_stage: int,
                 dtype_a: dtypes.DType,
                 dtype_b: dtypes.DType,
                 dtype_c: dtypes.DType,
                 dtype_acc: dtypes.DType,
                 dtype_comp: dtypes.DType,
                 trans_a: bool,
                 trans_b: bool,
                 trans_c: bool,
                 tensorop: Optional[TensorOpParams] = None,
                 algo: GemmAlgo = GemmAlgo.Simt,
                 splitk_serial: bool = False,
                 splitk_parallel: bool = False,
                 need_source: bool = True,
                 shuffle_stride: ShuffleStrideType = ShuffleStrideType.NoShuffle):
        
        """
        splitK and sliceK:
        https://github.com/NVIDIA/cutlass/issues/211#issuecomment-801992218
        split K: multiple block in k axis
        slice K: multiple warp in k axis
        """
        super().__init__()
        self.add_dependency(TensorView, TensorViewKernel, layout.RowMajor,
                            layout.ColumnMajor, GemmBasicKernel)
        self.add_param_class("gemmutils", GemmUtils(tile_shape), "GemmUtils")

        self.tile_shape = tile_shape
        self.shuffle_stride = shuffle_stride
        self.warp_tile_shape = warp_tile_shape
        self.num_stage = num_stage
        self.tensorop = tensorop
        self.splitk_serial = splitk_serial 
        self.splitk_parallel = splitk_parallel
        self.need_source = need_source
        transpose_gemm = trans_c
        if transpose_gemm:
            self.dtype_a = dtype_b
            self.dtype_b = dtype_a
            trans_a = not trans_a
            trans_b = not trans_b
            tmp = trans_a
            trans_a = trans_b
            trans_b = tmp
            trans_c = not trans_c
        else:
            self.dtype_a = dtype_a
            self.dtype_b = dtype_b
        dtype_a = self.dtype_a
        dtype_b = self.dtype_b

        self.dtype_c = dtype_c
        self.dtype_acc = dtype_acc
        self.dtype_comp = dtype_comp
        self.trans_a = trans_a
        self.trans_b = trans_b
        self.trans_c = trans_c
        self.algo = algo
        algo_spec = get_algo_spec(self.algo)(tile_shape, warp_tile_shape,
                                             num_stage, dtype_a, dtype_b,
                                             dtype_c, dtype_acc, dtype_comp,
                                             trans_a, trans_b, trans_c,
                                             tensorop, algo, shuffle_stride)
        self.algo_spec = algo_spec
        self.input_spec = algo_spec.input_spec
        self.mma_spec = algo_spec.mma_spec
        self.output_spec = algo_spec.output_spec
        
        self.warp_count_shape = tile_shape // warp_tile_shape
        self.warp_count = self.warp_count_shape.prod()
        self.num_threads = self.warp_count * constants.WARP_SIZE
        self.partk = self.warp_count_shape[2]
        self.add_param_class("inpitera", self.input_spec.input_iter_a, "InputIteratorA")
        self.add_param_class("inpiterb", self.input_spec.input_iter_b, "InputIteratorB")

        padding_mn = self.mma_spec.padding_mn

        self.acc_frag_iter = self.output_spec.acc_frag_iter
        self.out_warp_tile_iter = self.output_spec.out_warp_tile_iter
        out_smem_padding = self.output_spec.smem_padding
        self.fragment_c_t = array_type(dtype_acc, self.output_spec.get_accumulator_count())
        self.gemm_smem_storage = BlockMmaStorage(tile_shape,
                                                 seq(0, padding_mn[0]),
                                                 seq(0, padding_mn[1]),
                                                 num_stage, dtype_a, dtype_b)
        self.out_smem_storage = OutputSmemStorage(
            seq(tile_shape[0] // self.output_spec.num_out_iters * self.warp_count_shape[2],
                tile_shape[1]), out_smem_padding, dtype_acc, self.output_spec.frag_per_iter)
        # if partk > 1, we need more smem tile to save each k result.
        # self.frag_per_iter = self.output_spec.frag_per_iter
        # self.out_num_tile = self.output_spec.frag_per_iter if self.output_spec.frag_per_iter > 1 else self.partk
        # self.out_tile_size = self.out_smem_storage.smem_size // dtype_acc.itemsize() // self.out_num_tile
        print(self.out_smem_storage.smem_size, self.gemm_smem_storage.smem_size)
        self.smem_size = max(self.out_smem_storage.smem_size,
                             self.gemm_smem_storage.smem_size)
        self.add_param_class("gemm_smem_storage", self.gemm_smem_storage,
                             "BlockMmaStorage")
        self.add_param_class("out_smem_storage", self.out_smem_storage,
                             "OutputSmemStorage")
        inp_iter_a_param = self.input_spec.input_iter_a.get_params()
        inp_iter_b_param = self.input_spec.input_iter_b.get_params()
        have_workspace = splitk_serial or splitk_parallel

        self.gemm_params = GemmParams(tile_shape, dtype_a, dtype_b, dtype_c,
                                      dtype_comp, trans_a, trans_b, trans_c, inp_iter_a_param, 
                                      inp_iter_b_param, self.output_spec.out_iter.get_params(),
                                      have_workspace, shuffle_stride)
        self.add_param_class("gemm_params", self.gemm_params, "GemmParams")

        self.cutlass_smem_a_type = ("Mma::SmemIteratorA")
        self.cutlass_smem_b_type = ("Mma::SmemIteratorB")
        self.cutlass_warp_a_type = "Mma::Operator::IteratorA"
        self.cutlass_warp_b_type = "Mma::Operator::IteratorB"

        self.mma_container = Mma(dtype_acc, self.partk, num_stage, self.mma_spec, self.gemm_smem_storage)
        self.output = Output(dtype_acc, self.warp_count_shape, self.partk, self.output_spec, self.out_smem_storage)
        self.add_param_class("out_iter", self.output_spec.out_iter, "OutIter")
        self.add_param_class("out_iter_const", self.output_spec.const_out_iter, "ConstOutIter")

        self.add_param_class("out_op", self.output_spec.output_op, "OutputOp")

        self.add_param_class("mma", self.mma_container, "Mma")
        self.add_param_class("output", self.output, "Output")

    def get_algo_name(self):
        res = f"{self.algo.value}_{self.dtype_a.shortcut()}{self.dtype_b.shortcut()}{self.dtype_c.shortcut()}"
        res += f"{self.dtype_acc.shortcut()}{self.dtype_comp.shortcut()}"
        las = "n" if self.trans_a else "t"
        lbs = "n" if self.trans_b else "t"
        lcs = "n" if self.trans_c else "t"
        res += f"{las}{lbs}{lcs}"
        res += f"_m{self.tile_shape[0]}n{self.tile_shape[1]}k{self.tile_shape[2]}"
        res += f"m{self.warp_tile_shape[0]}n{self.warp_tile_shape[1]}k{self.warp_tile_shape[2]}"
        if self.tensorop is not None:
            tes = self.tensorop.shape
            res += f"T{tes[0]}{tes[1]}{tes[2]}"
        res += f"_{self.num_stage}"
        if self.shuffle_stride != ShuffleStrideType.NoShuffle:
            res += f"_{self.shuffle_stride.value}"
        if self.splitk_serial:
            res += "1"
        else:
            res += "0"
        if self.splitk_parallel:
            res += "1"
        else:
            res += "0"
        return res
        
    def support_splitk(self):
        return self.splitk_serial or self.splitk_parallel

    @pccm.cuda.cuda_global_function# (inline=True)
    def gemm_kernel(self):
        code = pccm.FunctionCode()
        if CUTLASS_MODE:
            code.arg("params", "CutlassGemm::GemmKernel::Params")
        else:
            code.arg("params", "GemmParams")
        # code.targ("T")
        # code.arg("params", "T")
        if CUTLASS_MODE:
            code.raw(f"""
            CutlassGemm::ThreadblockSwizzle threadblock_swizzle;
            extern __shared__ uint8_t SharedStorage[];
            typename CutlassGemm::GemmKernel::SharedStorage *shared_storage =
                reinterpret_cast<typename CutlassGemm::GemmKernel::SharedStorage *>(SharedStorage);

            constexpr bool kSplitKSerial = {pccm.boolean(self.splitk_serial)};

            cutlass::gemm::GemmCoord threadblock_tile_offset =
                threadblock_swizzle.get_tile_offset(params.grid_tiled_shape);

            // Early exit if CTA is out of range
            if (params.grid_tiled_shape.m() <= threadblock_tile_offset.m() ||
            params.grid_tiled_shape.n() <= threadblock_tile_offset.n()) {{
                return;
            }}
            // Compute initial location in logical coordinates
            cutlass::MatrixCoord tb_offset_A{{
                threadblock_tile_offset.m() * {self.tile_shape[0]},
                threadblock_tile_offset.k() * params.gemm_k_size,
            }};

            cutlass::MatrixCoord tb_offset_B{{
                threadblock_tile_offset.k() * params.gemm_k_size,
                threadblock_tile_offset.n() * {self.tile_shape[1]}
            }};
            int problem_size_k = min(params.problem_size.k(), (threadblock_tile_offset.k() + 1) * params.gemm_k_size);
            int gemm_k_iterations = (problem_size_k - tb_offset_A.column() + {self.tile_shape[2]} - 1) / {self.tile_shape[2]};

            """)
        else:
            code.raw(f"""
            constexpr bool kSplitKSerial = {pccm.boolean(self.splitk_serial)};
            extern __shared__ uint8_t SharedStorage[];
            auto gemm_shared_mem =
                reinterpret_cast<BlockMmaStorage *>(SharedStorage);
            auto out_shared_mem =
                reinterpret_cast<OutputSmemStorage *>(SharedStorage);

            int tile_offset_m = blockIdx.x;
            int tile_offset_n = blockIdx.y;
            int tile_offset_k = blockIdx.z;
            if (tile_offset_m >= params.grid_dims.x ||
                tile_offset_n >= params.grid_dims.y) {{
                return;
            }}
            """)
            code.raw(f"""
            tv::array<int, 2> block_offset_A{{tile_offset_m * {self.tile_shape[0]},
                                              tile_offset_k * params.gemm_k_size_per_split}};
            tv::array<int, 2> block_offset_B{{tile_offset_k * params.gemm_k_size_per_split,
                                              tile_offset_n * {self.tile_shape[1]}}};
            // Gemm::InputIteratorA::Params params_A(params.k);
            // Gemm::InputIteratorB::Params params_B(params.n);
            // refine gemm iteration for split-k
            auto problem_size_k = GemmUtils::get_gemm_k_bound(params.k, params.gemm_k_size_per_split, tile_offset_k);
            auto gemm_k_iterations = GemmUtils::get_gemm_iterations(problem_size_k, params.gemm_k_size_per_split, tile_offset_k);
            // int problem_size_k = min(params.k, (tile_offset_k + 1) * params.gemm_k_size_per_split);
            // int gemm_k_iterations =
            //     tv::div_up(problem_size_k - block_offset_A[1], {self.tile_shape[2]});
            """)
        code.raw(f"""
        int thread_idx = threadIdx.x;
        """)
        if CUTLASS_INPUT_ITER:
            code.raw(f"""
            {self.gemm_params.cutlass_a_type} input_iter_A(
                params.params_A,
                params.ref_A.data(),
                {{params.problem_size.m(), problem_size_k}},
                thread_idx,
                tb_offset_A);
            {self.gemm_params.cutlass_b_type} input_iter_B(
                params.params_B,
                params.ref_B.data(),
                {{problem_size_k, params.problem_size.n()}},
                thread_idx,
                tb_offset_B);
            """)
        else:
            if self.trans_a:
                a_extent = "tv::array<int, 2>{problem_size_k, params.m}"
                a_offset = "tv::array<int, 2>{block_offset_A[1], block_offset_A[0]}"
            else:
                a_extent = "tv::array<int, 2>{params.m, problem_size_k}"
                a_offset = "block_offset_A"

            if not self.trans_b:
                b_extent = "tv::array<int, 2>{problem_size_k, params.n}"
                b_offset = "block_offset_B"
            else:
                b_extent = "tv::array<int, 2>{params.n, problem_size_k}"
                b_offset = "tv::array<int, 2>{block_offset_B[1], block_offset_B[0]}"

            code.raw(f"""
            InputIteratorA input_iter_A(
                params.itera_params_, params.ptr_A,
                {a_extent},
                thread_idx,
                {a_offset});
            InputIteratorB input_iter_B(
                params.iterb_params_, params.ptr_B,
                {b_extent},
                thread_idx,
                {b_offset});
            """)
        code.raw(f"""
        int warp_idx = __shfl_sync(0xffffffff, threadIdx.x / 32, 0);
        int lane_idx = threadIdx.x % 32;
        """)
        if CUTLASS_SMEM_WARP_ITER:
            if not CUTLASS_DEBUG:
                # code.raw(f"""
                # DebugMma mma(shared_storage->main_loop, thread_idx, warp_idx, lane_idx);

                # {self.cutlass_warp_a_type} warp_iter_A(shared_storage->main_loop.operand_A_ref(), lane_idx);
                # {self.cutlass_warp_b_type} warp_iter_B(shared_storage->main_loop.operand_B_ref(), lane_idx);
                # {self.cutlass_smem_a_type} smem_iter_A(shared_storage->main_loop.operand_A_ref(), thread_idx);
                # {self.cutlass_smem_b_type} smem_iter_B(shared_storage->main_loop.operand_B_ref(), thread_idx);
                # int warp_mn =
                #     warp_idx % ({self.warp_count_shape[0]} * {self.warp_count_shape[1]});
                # int warp_idx_k =
                #     warp_idx / ({self.warp_count_shape[0]} * {self.warp_count_shape[1]});
                # int warp_m = warp_mn % {self.warp_count_shape[0]};
                # int warp_n = warp_mn / {self.warp_count_shape[0]};

                # warp_iter_A.add_tile_offset({{warp_m, {self.warp_gemm_iters} * warp_idx_k}});
                # warp_iter_B.add_tile_offset({{{self.warp_gemm_iters} * warp_idx_k, warp_n}});
                # """)
                code.raw(f"""
                DebugMma mma(shared_storage->main_loop, thread_idx, warp_idx, lane_idx);
                auto& smem_iter_A = mma.smem_iter_A;
                auto& smem_iter_B = mma.smem_iter_B;

                auto& warp_iter_A = mma.warp_iter_A;
                auto& warp_iter_B = mma.warp_iter_B;
                Mma mma(shared_storage->main_loop, thread_idx, warp_idx, lane_idx);

                """)

        else:
            code.raw(f"""
            int warp_mn =
                warp_idx % ({self.warp_count_shape[0]} * {self.warp_count_shape[1]});
            int warp_idx_k =
                warp_idx / ({self.warp_count_shape[0]} * {self.warp_count_shape[1]});
            int warp_m = warp_mn % {self.warp_count_shape[0]};
            int warp_n = warp_mn / {self.warp_count_shape[0]};
            Mma mma(gemm_shared_mem, thread_idx, warp_idx_k, warp_m, warp_n, lane_idx);
            """)
        code.raw(f"""
        {self.fragment_c_t} accumulators;
        accumulators.clear();
        """)
        with code.if_("!kSplitKSerial || gemm_k_iterations > 0"):
            if CUTLASS_DEBUG:
                code.raw(f"""
                mma(gemm_k_iterations, accumulators, input_iter_A, input_iter_B, accumulators);
                """)
            else:
                code.raw(f"""
                mma(gemm_k_iterations, accumulators, input_iter_A, input_iter_B, accumulators);
                """)
        if cudasim.enable_debug():
            code.raw(f"""
            tv::print_fragment_meta_once<float, {cudasim.debug_tx()}>(accumulators, "accumulator");
            // tv::print_fragment_once<int, 0, 16, {cudasim.debug_tx()}>(accumulators);

            """)
        if CUTLASS_OUTPUT_ITER:
            code.raw(f"""

            // // C = alpha * A@B + beta * D, D can be C
            Epilogue::OutputOp oop(params.output_op);
            cutlass::MatrixCoord threadblock_offset(
                threadblock_tile_offset.m() * {self.tile_shape[0]},
                threadblock_tile_offset.n() * {self.tile_shape[1]}
            );
            int block_idx = threadblock_tile_offset.m() + threadblock_tile_offset.n() * params.grid_tiled_shape.m();

            // Construct the semaphore.
            cutlass::Semaphore semaphore(params.semaphore + block_idx, thread_idx);

            typename Epilogue::OutputTileIterator iterator_C(
                params.params_C,
                params.ref_C.data(),
                params.problem_size.mn(),
                thread_idx,
                threadblock_offset
            );

            // Tile iterator writing to destination tensor.
            typename Epilogue::OutputTileIterator iterator_D(
                params.params_D,
                params.ref_D.data(),
                params.problem_size.mn(),
                thread_idx,
                threadblock_offset
            );

            Epilogue epilogue(
                shared_storage->epilogue, 
                thread_idx, 
                warp_idx, 
                lane_idx);

            
            epilogue(oop, iterator_D, accumulators, iterator_C); 

            """)

        else:
            code.raw(f"""
            // // C = alpha * A@B + beta * D, D can be C
            OutputOp output_op(params.alpha, params.beta);
            """)
            if self.splitk_serial:
                code.raw(f"""
                int block_idx = tile_offset_m + tile_offset_n * params.grid_dims.x;
                tv::Semaphore semaphore(reinterpret_cast<int*>(params.workspace) + block_idx, thread_idx);
                if (params.grid_dims.z > 1){{
                    semaphore.fetch();
                    output_op.set_k_partition(tile_offset_k, params.grid_dims.z);
                }}
                """)
            code.raw(f"""
            tv::array<int, 2> block_offset_C{{tile_offset_m * {self.tile_shape[0]},
                                            tile_offset_n * {self.tile_shape[1]}}};

            OutIter out_iter_C(params.out_params_, params.ptr_C, {{params.m, params.n}},
                                    {{block_offset_C[0], block_offset_C[1]}},
                                    thread_idx);
            """)
            if self.splitk_serial:
                # we reuse iter_c for splitk_serial to save some time.
                code.raw(f"""
                bool need_self_reduce = false;
                if (params.grid_dims.z > 1){{
                    if (tile_offset_k){{
                        need_self_reduce = true;
                    }}
                    semaphore.wait(tile_offset_k);
                    __threadfence();
                }}
                """)
            if self.need_source:
                code.raw(f"""
                ConstOutIter out_iter_D(params.out_params_, params.ptr_D, {{params.m, params.n}},
                                    {{block_offset_C[0], block_offset_C[1]}},
                                    thread_idx);
                """)
            code.raw(f"Output out(out_shared_mem, thread_idx, warp_idx_k, warp_m, warp_n, lane_idx);")
            if self.splitk_serial:
                with code.if_("need_self_reduce"):
                    code.raw(f"out.run_self_reduce(output_op, accumulators, out_iter_C);")
                with code.else_():
                    if self.need_source:
                        code.raw(f"out.run(output_op, accumulators, out_iter_C, out_iter_D);")
                    else:
                        code.raw(f"out.run(output_op, accumulators, out_iter_C);")
            else:
                if self.need_source:
                    code.raw(f"out.run(output_op, accumulators, out_iter_C, out_iter_D);")
                else:
                    code.raw(f"out.run(output_op, accumulators, out_iter_C);")
            
            if self.splitk_serial:
                code.raw(f"""
                if (params.grid_dims.z > 1){{
                    int lock = 0;
                    if (params.grid_dims.z == tile_offset_k + 1) {{
                        // The final threadblock resets the semaphore for subsequent grids.
                        lock = 0;
                    }}
                    else {{
                        // Otherwise, the semaphore is incremented
                        lock = tile_offset_k + 1;
                    }}
                    
                    semaphore.release(lock);
                }}
                """)
        return code

    # @lineprof.lineprof_wrapper
    async def gemm_kernel_python(self, params: GemmParams):
        smem = cudasim.get_smem()
        gemm_storage = self.gemm_smem_storage
        smem_A = smem[:gemm_storage.smem_size_a].view(
            dtypes.get_npdtype(self.dtype_a))
        assert smem_A.nbytes == gemm_storage.smem_size_a
        smem_B = smem[gemm_storage.smem_size_a:gemm_storage.smem_size].view(
            dtypes.get_npdtype(self.dtype_b))
        assert smem_B.nbytes == gemm_storage.smem_size_b
        out_storage = self.out_smem_storage
        smem_out = smem[:out_storage.smem_size].view(
            dtypes.get_npdtype(self.dtype_acc))
        if cudasim.enable_debug():
            smem_A_ptr = ArrayPtr(self.dtype_a.tv_dtype,
                                smem_A.nbytes // self.dtype_a.itemsize(),
                                external_data=tv.from_numpy(smem_A))
            smem_B_ptr = ArrayPtr(self.dtype_b.tv_dtype,
                                smem_B.nbytes // self.dtype_b.itemsize(),
                                external_data=tv.from_numpy(smem_B))
        else:
            smem_A_ptr = ArrayPtr(self.dtype_a.tv_dtype,
                                smem_A.nbytes // self.dtype_a.itemsize(),
                                external_data=tv.from_numpy(smem_A),
                                meta_data=tv.Tensor())
            smem_B_ptr = ArrayPtr(self.dtype_b.tv_dtype,
                                smem_B.nbytes // self.dtype_b.itemsize(),
                                external_data=tv.from_numpy(smem_B),
                                meta_data=tv.Tensor())

        thread_idx = cudasim.threadIdx().x

        # share smem metadata in block
        smem_A_ptr = await cudasim.block_broadcast(smem_A_ptr)
        smem_B_ptr = await cudasim.block_broadcast(smem_B_ptr)

        smem_out_ptr = ArrayPtr(self.dtype_acc.tv_dtype,
                                smem_out.nbytes // self.dtype_acc.itemsize(),
                                external_data=tv.from_numpy(smem_out))

        tile_offset_m = cudasim.blockIdx().x
        tile_offset_n = cudasim.blockIdx().y
        tile_offset_k = cudasim.blockIdx().z
        if (tile_offset_m >= params.grid_dims.x
                or tile_offset_n >= params.grid_dims.y):
            return

        block_offset_A = seq(tile_offset_m * self.tile_shape[0],
                             tile_offset_k * params.gemm_k_size)
        block_offset_B = seq(tile_offset_k * params.gemm_k_size,
                             tile_offset_n * self.tile_shape[1])
        block_offset_C = seq(tile_offset_m * self.tile_shape[0],
                             tile_offset_n * self.tile_shape[1])
        block_offset_D = seq(tile_offset_m * self.tile_shape[0],
                             tile_offset_n * self.tile_shape[1])
        problem_size_k = min(params.k,
                             (tile_offset_k + 1) * params.gemm_k_size)
        gemm_k_iterations = mask_iters.div_up(
            problem_size_k - block_offset_A[1], self.tile_shape[2])
        if self.trans_a:
            extent_A = seq(problem_size_k, params.m)
            tb_offset_A = seq(block_offset_A[1], block_offset_A[0])
        else:
            extent_A = seq(params.m, problem_size_k)
            tb_offset_A = seq(block_offset_A[0], block_offset_A[1])

        input_iter_A = self.input_spec.input_iter_a.python_ctor(params.itera_params_, params.ptr_A,
                                                   extent_A, thread_idx,
                                                   tb_offset_A, is_left=True)
        if self.trans_b:
            extent_B = seq(params.n, problem_size_k)
            tb_offset_B = seq(block_offset_B[1], block_offset_B[0])
        else:
            extent_B = seq(problem_size_k, params.n)
            tb_offset_B = seq(block_offset_B[0], block_offset_B[1])
        input_iter_B = self.input_spec.input_iter_b.python_ctor(params.iterb_params_, params.ptr_B,
                                                   extent_B, thread_idx,
                                                   tb_offset_B, is_left=False)
        if cudasim.threadIdx().x == 0:
            print(cudasim.blockIdx(), gemm_k_iterations, block_offset_A, block_offset_B)
            print(extent_A, tb_offset_A, extent_B, tb_offset_B)

        warp_idx = cudasim.get_warp_id()
        lane_idx = thread_idx % 32
        await cudasim.syncthreads()
        warp_idx_k = warp_idx // (self.warp_count_shape[0] *
                                  self.warp_count_shape[1])
        warp_mn = warp_idx % (self.warp_count_shape[0] *
                              self.warp_count_shape[1])
        warp_m = warp_mn % self.warp_count_shape[0]
        warp_n = warp_mn // self.warp_count_shape[0]
        mma = await self.mma_container.python_ctor(smem_A_ptr, smem_B_ptr, thread_idx, warp_idx_k, warp_m, warp_n, lane_idx)
        accumulators = ArrayPtr(
            self.dtype_acc.tv_dtype,
            self.mma_spec.accumulator_size)
        accumulators.clear()
        res_inputs = await mma(gemm_k_iterations, accumulators, input_iter_A, input_iter_B, accumulators)

        await cudasim.syncthreads()
                # cudasim.debug_print(acc[:16])


        output_op = self.output_spec.output_op.python_ctor(params.alpha, params.beta)
        if self.splitk_serial:
            if cudasim.gridDim().z > 1:
                output_op.set_k_partition_python(tile_offset_k, cudasim.gridDim().z)
        out_iter_C = self.output_spec.out_iter.python_ctor(params.out_params_, params.ptr_C, 
            seq(params.m, params.n), seq(block_offset_C[0], block_offset_C[1]),
            thread_idx)
        out_iter_D = self.output_spec.out_iter.python_ctor(params.out_params_, params.ptr_D, 
            seq(params.m, params.n), seq(block_offset_C[0], block_offset_C[1]),
            thread_idx)
        if self.splitk_serial and cudasim.gridDim().z > 1:
            if tile_offset_k > 0:
                out_iter_C = out_iter_D
        output = self.output.python_ctor(smem_out_ptr, thread_idx, warp_idx_k, warp_m, warp_n, lane_idx)
        need_self_reduce = False
        if self.splitk_serial:
            if cudasim.gridDim().z > 1:
                if tile_offset_k > 0:
                    need_self_reduce = True 
        if self.splitk_serial:
            if need_self_reduce:
                res_output = await output(output_op, accumulators, out_iter_C, self_reduce=True)
            else:
                if self.need_source:
                    res_output = await output(output_op, accumulators, out_iter_C, out_iter_D)
                else:
                    res_output = await output(output_op, accumulators, out_iter_C)
        else:
            if self.need_source:
                res_output = await output(output_op, accumulators, out_iter_C, out_iter_D)
            else:
                res_output = await output(output_op, accumulators, out_iter_C)
        if not cudasim.enable_debug():
            return
        
        res = {
            **res_output,
            **res_inputs,
        }
        return res 