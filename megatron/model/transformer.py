#
# Copyright 2021 Biderman et al. This file is based on code by the authors denoted below and has been modified from its original version.
#
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

"""Transformer."""

import math
import torch
import torch.nn.functional as F
import torch.nn as nn

from .norms import get_norm
from megatron import mpu
from megatron.model.fused_softmax import FusedScaleMaskSoftmax
from megatron.model.activations import get_activation
from megatron.model.utils import exists, get_fusion_type
from megatron.model.positional_embeddings import (
    RotaryEmbedding,
    apply_rotary_pos_emb,
    apply_rotary_pos_emb_torch,
    AliBi,
)
from megatron.model.fused_bias_dropout import (
    get_bias_dropout_add,
    bias_dropout_add_fused_train,
    bias_dropout_add_fused_inference,
)
from megatron.model.utils import configure_sparse_attention

# flags required to enable jit fusion kernels
torch._C._jit_set_profiling_mode(False)
torch._C._jit_set_profiling_executor(False)
torch._C._jit_override_can_fuse_on_cpu(True)
torch._C._jit_override_can_fuse_on_gpu(True)

""" We use the following notation throughout this file:
     h: hidden size
     n: number of attention heads
     p: number of model parallel partitions
     np: n/p
     hp: h/p
     hn: h/n
     b: batch size
     s: sequence length
     l: number of layers
    Transformer takes input of size [s, b, h] and returns a
    tensor of the same size. We use the following arguments:
        hyperparameters: transformer hyperparameters
        attention_mask_func: a function that takes `unmasked-attention-scores`
            with size [b, np, s, s] and an `attention-mask` and will apply
            the masking. The function should return a masked score of the
            same size [b, np, s, s].
               masked-attention-scores = attention_mask_func(
                                     unmasked-attention-scores, attention-mask)
"""


class ParallelMLP(nn.Module):
    """MLP.

    MLP will take the input with h hidden state, project it to 4*h
    hidden dimension, perform nonlinear transformation, and project the
    state back into h hidden dimension. At the end, dropout is also
    applied.
    """

    def __init__(
        self, neox_args, init_method, output_layer_init_method, parallel_output=False
    ):
        super().__init__()

        self.activation_func = get_activation(neox_args)
        self.activation_type = neox_args.activation
        self.bias_gelu_fusion = neox_args.bias_gelu_fusion

        if neox_args.feedforward_size:
            # override feedforward intermediate dimension if provided.
            ff_mult = neox_args.feedforward_size
        else:
            # auto scale so geglu has equal parameters
            ff_mult = 4 * 2 / 3 if self.activation_type == "geglu" else 4
        ff_dim = (
            int(ff_mult * neox_args.hidden_size) * 2 # TODO(Hailey): do we want this to happen even if feedforward_size is overriden? read GEGLU paper for more info
            if self.activation_type == "geglu"
            else ff_mult * neox_args.hidden_size
        )
        self.dense_h_to_4h = mpu.ColumnParallelLinear(
            neox_args=neox_args,
            input_size=neox_args.hidden_size,
            output_size=ff_dim,
            gather_output=False,
            init_method=init_method,
            skip_bias_add=True,
        )
        ff_dim_in = ff_dim // 2 if self.activation_type == "geglu" else ff_dim
        # Project back to h.
        self.dense_4h_to_h = mpu.RowParallelLinear(
            neox_args=neox_args,
            input_size=ff_dim_in,
            output_size=neox_args.hidden_size,
            input_is_parallel=True,
            init_method=output_layer_init_method,
            skip_bias_add=True,
            parallel_output=parallel_output,
        )

    def forward(self, hidden_states):

        # [s, b, 4hp]
        intermediate_parallel, bias_parallel = self.dense_h_to_4h(hidden_states)

        if (
            self.activation_type == "gelu" and self.bias_gelu_fusion
        ) or self.activation_type == "geglu":
            intermediate_parallel = self.activation_func(
                intermediate_parallel, bias_parallel
            )
        else:
            intermediate_parallel = self.activation_func(
                intermediate_parallel + bias_parallel
            )

        # [s, b, h]
        output, output_bias = self.dense_4h_to_h(intermediate_parallel)
        return output, output_bias


class ParallelLinear(nn.Module):
    """
    A Parallel Linear Layer transforming the transformer outputs from hidden_size -> vocab_size
    """

    def __init__(
        self,
        neox_args,
        parallel_output=True,
        init_method=nn.init.xavier_normal_,
    ):
        super().__init__()
        parallelism = neox_args.output_layer_parallelism
        if parallelism == "column":
            self.final_linear = mpu.ColumnParallelLinear(
                neox_args=neox_args,
                input_size=neox_args.hidden_size,
                output_size=neox_args.padded_vocab_size,
                bias=False,
                init_method=init_method,
                gather_output=not parallel_output,
                skip_bias_add=False,
            )
        else:
            self.final_linear = mpu.RowParallelLinear(
                neox_args=neox_args,
                input_size=neox_args.hidden_size,
                output_size=neox_args.padded_vocab_size,
                bias=False,
                input_is_parallel=False,
                init_method=init_method,
                parallel_output=parallel_output,
                skip_bias_add=False,
            )

    def forward(self, hidden_states):
        return self.final_linear(hidden_states)


class ParallelAttention(nn.Module):
    """Parallel attention layer abstract class.

    attention layer takes input with size [b, s, h]
    and returns output of the same size.
    """

    def __init__(
        self,
        neox_args,
        attention_mask_func,
        init_method,
        output_layer_init_method,
        layer_number,
        is_cross_attention=False,
        rpe=None,
        rotary=False,
        use_cache=False,
        parallel_output=False,
    ):
        super().__init__()

        self.fp16 = neox_args.precision == "fp16"
        self.bf16 = neox_args.precision == "bfloat16"
        self.attention_mask_func = attention_mask_func
        self.apply_query_key_layer_scaling = neox_args.apply_query_key_layer_scaling
        self.use_cache = use_cache
        self.attention_softmax_in_fp32 = neox_args.attention_softmax_in_fp32
        if self.apply_query_key_layer_scaling:
            self.attention_softmax_in_fp32 = True
        self.layer_number = layer_number
        # Per attention head and per partition values.
        world_size = mpu.get_model_parallel_world_size()
        self.hidden_size_per_partition = mpu.divide(neox_args.hidden_size, world_size)
        self.hidden_size_per_attention_head = mpu.divide(
            neox_args.hidden_size, neox_args.num_attention_heads
        )
        self.num_attention_heads_per_partition = mpu.divide(
            neox_args.num_attention_heads, world_size
        )
        self.pos_emb = neox_args.pos_emb

        self.is_cross_attention = is_cross_attention

        # Strided linear layer. 
        if is_cross_attention:
            # project encoder key, value states separately
            self.query = mpu.ColumnParallelLinear(
                neox_args=neox_args,
                input_size=neox_args.hidden_size,
                output_size=neox_args.hidden_size,
                gather_output=False,
                init_method=init_method,
            )
            self.key_value = mpu.ColumnParallelLinear(
                neox_args=neox_args,
                input_size=neox_args.hidden_size,
                output_size=2 * neox_args.hidden_size,
                gather_output=False,
                init_method=init_method,
            )
        else:
            self.query_key_value = mpu.ColumnParallelLinear(
                neox_args=neox_args,
                input_size=neox_args.hidden_size,
                output_size=3 * neox_args.hidden_size,
                gather_output=False,
                init_method=init_method,
            )

        coeff = None
        self.norm_factor = math.sqrt(self.hidden_size_per_attention_head)
        if self.apply_query_key_layer_scaling:
            coeff = max(1, self.layer_number)
            self.norm_factor *= coeff

        self.rpe = rpe

        if self.pos_emb == "alibi":
            self.alibi_embed = AliBi(
                neox_args.num_attention_heads,
                neox_args.model_parallel_size,
                mpu.get_model_parallel_rank(),
            )

        # TODO: this arg shouldn't need to be passed in - get from neox_args
        if rotary:
            if neox_args.rotary_pct == 1:
                self.rotary_ndims = None
            else:
                assert neox_args.rotary_pct < 1
                self.rotary_ndims = int(
                    self.hidden_size_per_attention_head * neox_args.rotary_pct
                )
            dim = (
                self.rotary_ndims
                if self.rotary_ndims is not None
                else self.hidden_size_per_attention_head
            )
            self.rotary_emb = RotaryEmbedding(
                dim, base=neox_args.rotary_emb_base, precision=neox_args.params_dtype
            )
        else:
            self.rotary_emb = None

        self.attention_type = neox_args.attention_config[layer_number]
        self.sparse = self.attention_type != "global"
        if self.sparse:
            self.sparse_attn = configure_sparse_attention(
                neox_args,
                self.attention_type,
                self.num_attention_heads_per_partition,
                mpu=mpu,
            )
        else:
            self.scale_mask_softmax = FusedScaleMaskSoftmax(
                input_in_fp16=self.fp16,
                input_in_bf16=self.bf16,
                fusion_type=get_fusion_type(neox_args, cross_attention=self.is_cross_attention),
                mask_func=self.attention_mask_func,
                softmax_in_fp32=self.attention_softmax_in_fp32,
                scale=coeff,
            )

            # Dropout. Note that for a single iteration, this layer will generate
            # different outputs on different number of parallel partitions but
            # on average it should not be partition dependent.
            self.attention_dropout = nn.Dropout(neox_args.attention_dropout)

        # Output.
        self.dense = mpu.RowParallelLinear(
            neox_args=neox_args,
            input_size=neox_args.hidden_size,
            output_size=neox_args.hidden_size,
            input_is_parallel=True,
            init_method=output_layer_init_method,
            skip_bias_add=True,
            parallel_output=parallel_output,
        )

    def attention(
        self, query_layer, key_layer, value_layer, layer_past, attention_mask
    ):
        # ===================================
        # Raw attention scores. [b, np, s, s]
        # ===================================

        # [b, np, sq, sk]
        output_size = (
            query_layer.size(1),
            query_layer.size(2),
            query_layer.size(0),
            key_layer.size(0),
        )

        # [sq, b, np, hn] -> [sq, b * np, hn]
        query_layer = query_layer.view(
            output_size[2], output_size[0] * output_size[1], -1
        )
        key_layer = key_layer.view(output_size[3], output_size[0] * output_size[1], -1)

        # preallocating result tensor: [b * np, sq, sk]
        matmul_result = torch.empty(
            output_size[0] * output_size[1],
            output_size[2],
            output_size[3],
            dtype=query_layer.dtype,
            device=torch.cuda.current_device(),
        )

        # Raw attention scores. [b * np, sq, sk]
        matmul_result = torch.baddbmm(
            matmul_result,
            query_layer.transpose(0, 1),  # [b * np, sq, hn]
            key_layer.transpose(0, 1).transpose(1, 2),  # [b * np, hn, sk]
            beta=0.0,
            alpha=(1.0 / self.norm_factor),
        )

        # change view to [b, np, sq, sk]
        attention_scores = matmul_result.view(*output_size)

        # ==================================================
        # Update attention mask for inference. [b, np, sq, sk]
        # ==================================================

        if self.use_cache:
            with torch.no_grad():
                attention_mask = attention_mask[
                    ..., : attention_scores.size(2), : attention_scores.size(3)
                ]

        # ===========================
        # Attention probs and dropout
        # ===========================

        if exists(self.rpe):
            rpe = self.rpe(query_layer.size(0), key_layer.size(0))
            attention_scores += rpe  # [1, np, sq, sk]

        if self.pos_emb == "alibi":
            attention_scores = self.alibi_embed(attention_scores)
        # if self.is_cross_attention:    
           # assert False, f"{attention_scores.shape}, {attention_mask.shape}"
        # attention scores and attention mask [b, np, sq, sk]
        attention_probs = self.scale_mask_softmax(attention_scores, attention_mask)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        with mpu.get_cuda_rng_tracker().fork():
            attention_probs = self.attention_dropout(attention_probs)

        # =========================
        # Context layer. [sq, b, hp]
        # =========================

        # value_layer -> context layer.
        # [sk, b, np, hn] --> [b, np, sq, hn]

        # context layer shape: [b, np, sq, hn]
        output_size = (
            value_layer.size(1),
            value_layer.size(2),
            query_layer.size(0),
            value_layer.size(3),
        )

        # change view [sk, b * np, hn]
        value_layer = value_layer.view(
            value_layer.size(0), output_size[0] * output_size[1], -1
        )

        # change view [b * np, sq, sk]
        attention_probs = attention_probs.view(
            output_size[0] * output_size[1], output_size[2], -1
        )

        # matmul: [b * np, sq, hn]
        context_layer = torch.bmm(attention_probs, value_layer.transpose(0, 1))

        # change view [b, np, sq, hn]
        context_layer = context_layer.view(*output_size)
        return context_layer

    def sparse_attention(self, query_layer, key_layer, value_layer, attention_mask):
        # TODO: sparse attn dropout?
        # TODO: pad to block size
        # shape of q/k/v is [sq, b, np, hn] and needs to be transposed to [b, np, sq, hn]
        query_layer, key_layer, value_layer = map(
            lambda t: t.permute(1, 2, 0, 3).contiguous(),
            (query_layer, key_layer, value_layer),
        )
        # output shape [b, np(heads), sq, hn]
        attn_mask = attention_mask.to(query_layer.dtype) * -10000
        if exists(self.rpe):
            rpe = self.rpe(query_layer.size(0), key_layer.size(0))
        else:
            rpe = None
        return self.sparse_attn(
            query_layer, key_layer, value_layer, attn_mask=attn_mask, rpe=rpe
        )

    def forward(self, hidden_states, attention_mask, encoder_hidden_states=None, layer_past=None):

        # hidden_states: [sq, b, h]

        # =====================
        # Query, Key, and Value
        # =====================

        # Attention heads [sq, b, h] --> [sq, b, (np * 3 * hn)]
        if not self.is_cross_attention:
            mixed_x_layer, _ = self.query_key_value(hidden_states)

            # [sq, b, (np * 3 * hn)] --> [sq, b, np, 3 * hn]
            new_tensor_shape = mixed_x_layer.size()[:-1] + (
                self.num_attention_heads_per_partition,
                3 * self.hidden_size_per_attention_head,
            )
            mixed_x_layer = mixed_x_layer.view(*new_tensor_shape)

            # [sq, b, np, 3 * hn] --> 3 [sq, b, np, hn]
            (query_layer, key_layer, value_layer) = mpu.split_tensor_along_last_dim(
                mixed_x_layer, 3
            )
        else:
            # TODO(Hailey): does this use more memory somehow? (not cleaned up?)
            mixed_kv_layer, _ = self.key_value(encoder_hidden_states)
            q_layer, _ = self.query(hidden_states)

            # [sk, b, (np * 2 * hn)] --> [sk, b, np, 2 * hn]
            new_tensor_shape = mixed_kv_layer.size()[:-1] + (
                self.num_attention_heads_per_partition,
                2 * self.hidden_size_per_attention_head,
            )
            mixed_kv_layer = mixed_kv_layer.view(*new_tensor_shape)

            # [sk, b, np, 2 * hn] --> 2 [sk, b, np, hn]
            (key_layer, value_layer) = mpu.split_tensor_along_last_dim(
                mixed_kv_layer, 2
            )
            
            # [sq, b, (np * hn)] --> [sq, b, np, hn]
            new_query_shape = (q_layer.size(0),) + new_tensor_shape[1:-1] + (self.hidden_size_per_attention_head,)
            query_layer = q_layer.view(*new_query_shape)

        if exists(self.rotary_emb):
            if exists(self.rotary_ndims):
                # partial rotary
                query_rot, query_pass = (
                    query_layer[..., : self.rotary_ndims],
                    query_layer[..., self.rotary_ndims :],
                )
                key_rot, key_pass = (
                    key_layer[..., : self.rotary_ndims],
                    key_layer[..., self.rotary_ndims :],
                )
            else:
                # full rotary
                query_rot, key_rot = query_layer, key_layer
            apply_rotary_fn = (
                    apply_rotary_pos_emb_torch if self.bf16 or self.is_cross_attention else apply_rotary_pos_emb # jit fails when query and key have different sizes
            )
            seq_len = key_layer.shape[0]
            offset = 0
            if exists(layer_past) and layer_past.numel() > 0:
                offset = layer_past[0].shape[0]
                seq_len += offset
            cos, sin = self.rotary_emb(value_layer, seq_len=seq_len)
            query_layer = apply_rotary_fn(
                query_rot, cos, sin, offset=offset
            )
            key_layer = apply_rotary_fn(
                key_rot, cos, sin, offset=offset
            )

            if exists(self.rotary_ndims):
                query_layer = torch.cat((query_layer, query_pass), dim=-1)
                key_layer = torch.cat((key_layer, key_pass), dim=-1)

        # ==================================
        # Cache key and value for inference
        # ==================================

        if exists(layer_past) and layer_past.numel() > 0:
            past_key, past_value = layer_past
            key_layer = torch.cat((past_key.type_as(key_layer), key_layer), dim=0)
            value_layer = torch.cat(
                (past_value.type_as(value_layer), value_layer), dim=0
            )

        if self.use_cache:
            present = torch.stack((key_layer, value_layer))

        if not self.sparse:
            context_layer = self.attention(
                query_layer, key_layer, value_layer, layer_past, attention_mask
            )
        else:
            context_layer = self.sparse_attention(
                query_layer, key_layer, value_layer, attention_mask
            )

        # [b, np, sq, hn] --> [sq, b, np, hn]
        context_layer = context_layer.permute(2, 0, 1, 3).contiguous()

        # [sq, b, np, hn] --> [sq, b, hp]
        new_context_layer_shape = context_layer.size()[:-2] + (
            self.hidden_size_per_partition,
        )
        context_layer = context_layer.view(*new_context_layer_shape)

        # =================
        # Output. [sq, b, h]
        # =================

        output, bias = self.dense(context_layer)

        if self.use_cache:
            output = [output, present]

        return output, bias


class ParallelTransformerLayer(nn.Module):
    """A single transformer layer.

    Transformer layer takes input with size [b, s, h] and returns an
    output of the same size.
    """

    def __init__(
        self,
        neox_args,
        attention_mask_func,
        init_method,
        output_layer_init_method,
        layer_number,
        layer_type="decoder",
        rpe=None,
        rotary=False,
        use_cache=False,
    ):

        super().__init__()
        self.layer_number = layer_number
        self.layer_type = layer_type
        self.model_arch = neox_args.model_arch

        norm, eps = get_norm(neox_args)

        # Layernorm on the input data.
        self.input_layernorm = norm(neox_args.hidden_size, eps=eps)
        self.use_cache = use_cache

        self.hidden_dropout = neox_args.hidden_dropout
        self.bias_dropout_fusion = neox_args.bias_dropout_fusion
        self.gpt_j_residual = neox_args.gpt_j_residual

        if self.gpt_j_residual:
            self.reduce = mpu.mappings.reduce_from_model_parallel_region

        # TODO(Hailey): where to override attention masking for encoder layers?
        # Self attention.
        self.attention = ParallelAttention(
            neox_args=neox_args,
            attention_mask_func=attention_mask_func,
            init_method=init_method,
            output_layer_init_method=output_layer_init_method,
            layer_number=layer_number,
            rpe=rpe,
            use_cache=self.use_cache,
            rotary=rotary,
            parallel_output=self.gpt_j_residual,
        )

        # Layernorm on the output of the attention layer.
        self.post_attention_layernorm = norm(neox_args.hidden_size, eps=eps)

        # Cross attention, if is a decoder layer in T5 architecture
        self.do_crossattn = (self.layer_type == "decoder" and neox_args.model_arch == "t5")
        if self.do_crossattn:
            self.cross_attention = ParallelAttention(
                neox_args=neox_args,
                attention_mask_func=attention_mask_func,
                init_method=init_method,
                output_layer_init_method=output_layer_init_method,
                layer_number=layer_number,
                is_cross_attention=True,
                rpe=rpe,
                use_cache=self.use_cache,
                rotary=rotary,
                parallel_output=self.gpt_j_residual,
            )

            self.post_cross_attention_layernorm = norm(neox_args.hidden_size, eps=eps)


        # MLP
        self.mlp = ParallelMLP(
            neox_args=neox_args,
            init_method=init_method,
            output_layer_init_method=output_layer_init_method,
            parallel_output=self.gpt_j_residual,
        )

        self.layer_past = None  # used to cache k/v pairs in inference

    def _get_bias_dropout(self):
        if self.bias_dropout_fusion:
            fn = (
                bias_dropout_add_fused_train
                if self.training
                else bias_dropout_add_fused_inference
            )
        else:
            fn = get_bias_dropout_add(self.training)
        return fn

    def forward(
        self, 
        x,
        attention_mask,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        layer_past=None,
    ):
        layer_past = layer_past if layer_past is not None else self.layer_past
        bias_dropout_fn = self._get_bias_dropout()
        # x: [b, s, h]
        if self.gpt_j_residual:
            # pseudocode:
            # x = x + attn(ln1(x)) + mlp(ln2(x))
            # this means we can avoid doing the allreduce in the attn / mlp outputs
            # to save communication time (we can do a single allreduce after we add mlp / attn outputs).

            # pseudocode (with cross attention):
            # x = x + attn(ln1(x)) + crossattn(ln1.5(x)) + mlp(ln2(x))

            # attention_output = attn(ln1(x))
            residual = x
            attention_output, attention_bias = self.attention(
                self.input_layernorm(x), attention_mask, layer_past=layer_past
            )
            if self.use_cache:
                attention_output, presents = attention_output
                self.layer_past = presents

            with torch.enable_grad():
                attention_output = bias_dropout_fn(
                    attention_output,
                    bias=attention_bias.expand_as(attention_output),
                    residual=None,
                    prob=self.hidden_dropout,
                )

            if self.do_crossattn:
                # cross_attention_output = crossattn(ln1.5(x)) + attention_output
                cross_attention_output, cross_attention_bias = self.cross_attention(
                    self.post_attention_layernorm(x), 
                    encoder_attention_mask, 
                    encoder_hidden_states=encoder_hidden_states,
                    layer_past=layer_past,
                )
                if self.use_cache:
                    cross_attention_output, presents = cross_attention_output
                    self.layer_past = presents

                with torch.enable_grad():
                    cross_attention_output = bias_dropout_fn(
                        cross_attention_output,
                        bias=cross_attention_bias.expand_as(cross_attention_output),
                        residual=attention_output,
                        prob=self.hidden_dropout,
                    )

            # assign these to cross attn terms if using it, else use self attn
            ln2 = self.post_attention_layernorm if not self.do_crossattn else self.post_cross_attention_layernorm
            attn_output = attention_output if not self.do_crossattn else cross_attention_output

            # output = mlp(ln2(x)) + (cross) attention_output
            mlp_output, mlp_bias = self.mlp(ln2(x))
            with torch.enable_grad():
                output = bias_dropout_fn(
                    mlp_output,
                    bias=mlp_bias.expand_as(mlp_output),
                    residual=attn_output,
                    prob=self.hidden_dropout,
                )

            # output = output + residual
            output = residual + self.reduce(output)
        else:
            # pseudocode:
            # x = x + attn(ln1(x))
            # only do if using cross attention:  # x = x + crossattn(ln1.5(x))
            # x = x + mlp(ln2(x))

            residual = x

            # x = x + attn(ln1(x))
            attention_output, attention_bias = self.attention(
                self.input_layernorm(x), attention_mask, layer_past=layer_past
            )
            if self.use_cache:
                attention_output, presents = attention_output
                self.layer_past = presents
            with torch.enable_grad():
                attention_output = bias_dropout_fn(
                    attention_output,
                    bias=attention_bias.expand_as(residual),
                    residual=residual,
                    prob=self.hidden_dropout,
                )

            # if cross attention in layer: x = x + crossattn(ln1.5(x))
            if self.do_crossattn:
                # TODO(Hailey): check how to handle attention masks here + in encoder layers
                cross_attention_output, cross_attention_bias = self.cross_attention(
                    self.post_attention_layernorm(attention_output), 
                    encoder_attention_mask, 
                    encoder_hidden_states=encoder_hidden_states,
                    layer_past=layer_past,
                )
                if self.use_cache:
                    cross_attention_output, presents = cross_attention_output
                    self.layer_past = presents
                with torch.enable_grad():
                    cross_attention_output = bias_dropout_fn(
                        cross_attention_output,
                        bias=cross_attention_bias.expand_as(attention_output),
                        residual=attention_output,
                        prob=self.hidden_dropout,
                    )

            residual = attention_output if not self.do_crossattn else cross_attention_output
            ln2 = self.post_attention_layernorm if not self.do_crossattn else self.post_cross_attention_layernorm

            # output = x + mlp(ln2(x))
            mlp_output, mlp_bias = self.mlp(
                ln2(residual)
            )
            with torch.enable_grad():
                output = bias_dropout_fn(
                    mlp_output,
                    bias=mlp_bias.expand_as(residual),
                    residual=residual,
                    prob=self.hidden_dropout,
                )

        return output


class ParallelTransformerLayerPipe(ParallelTransformerLayer):
    """Extends ParallelTransformerLayer to forward attention_mask through the pipeline."""

    def forward(self, args):
        if self.model_arch == "t5":
            if self.layer_type == "encoder":
                assert (
                    len(args) == 5
                ), f"Encoder layer expects 5 arguments - \
                    hidden_states, decoder_input_ids, decoder_position_ids, encoder_attention_mask, attention_mask,\
                    got {len(args)}"

                hidden_states, decoder_input_ids, decoder_position_ids, encoder_attention_mask, attention_mask = \
                    args
                
                # TODO(Hailey): Between the first and second decoder layer, shape of decoder input ids and pos. ids goes from [b, s] to [bs]. Why???
                # so we fix it manually
                if decoder_input_ids.size()[0] != hidden_states.size()[1]:
                    # then DS did the weird reshape so we fix it
                    decoder_input_ids = decoder_input_ids.view(hidden_states.size()[1], -1)
                    decoder_position_ids = decoder_position_ids.view(hidden_states.size()[1], -1) 

                return super().forward(hidden_states, encoder_attention_mask), \
                    decoder_input_ids, decoder_position_ids, encoder_attention_mask, attention_mask
            elif self.layer_type == "decoder":
                assert (
                    len(args) == 4
                ), f"T5 Decoder layer expects 4 arguments - \
                    decoder_hidden_states, encoder_hidden_states, encoder_attention_mask, attention_mask,\
                    got {len(args)}"
                hidden_states, encoder_hidden_states, encoder_attention_mask, decoder_attention_mask = args
                
                return super().forward(
                    hidden_states, 
                    decoder_attention_mask, 
                    encoder_hidden_states=encoder_hidden_states,
                    encoder_attention_mask=encoder_attention_mask,
                ), encoder_hidden_states, encoder_attention_mask, decoder_attention_mask

        else:
            assert (
                len(args) == 2
            ), "ParallelTransformerLayerPipe expects 2 arguments - hidden_states and attention_mask"
            hidden_states, attention_mask = args
            # we are returning just [hidden_states, mask]
            return super().forward(hidden_states, attention_mask), attention_mask


class ParallelLinearPipe(ParallelLinear):
    """Another helper class to pass presents through to the output when doing inference with a Pipe Parallel model"""

    def forward(self, args):
        assert isinstance(
            args, torch.Tensor
        ), "ParallelLinearPipe expects a single argument - hidden_states"
        hidden_state = args
        logits, bias = super().forward(hidden_state)
        return logits


class NormPipe(nn.Module):
    """Just a helper class to pass presents through to the output when doing inference with a Pipe Parallel model"""

    def __init__(self, norm_class, hidden_size, eps):
        super().__init__()
        self.norm = norm_class(hidden_size, eps=eps)

    def forward(self, args):
        assert not isinstance(
            args, tuple
        ), "NormPipe should only receive a single tensor as input"
        return self.norm(args)


def parallel_lm_logits(input_, word_embeddings_weight, parallel_output, bias=None):
    """LM logits using word embedding weights."""
    # Parallel logits.
    input_parallel = mpu.copy_to_model_parallel_region(input_)

    # Matrix multiply.
    if bias is None:
        logits_parallel = F.linear(input_parallel, word_embeddings_weight)
    else:
        logits_parallel = F.linear(input_parallel, word_embeddings_weight, bias)

    # Gather if needed.
    if parallel_output:
        return logits_parallel

    return mpu.gather_from_model_parallel_region(logits_parallel)
