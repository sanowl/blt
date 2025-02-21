# Copyright (c) Meta Platforms, Inc. and affiliates.

import logging
from typing import List, Optional, Tuple, Union

import torch
import torch.nn
import torch.nn as nn
from torch.nn import functional as F
from torch.nn.attention.flex_attention import BlockMask
from xformers.ops import AttentionBias

from bytelatent.base_transformer import (
    InitStdFactor,
    RMSNorm,
    RotaryEmbedding,
    TransformerBlock,
)
from bytelatent.model.transformer import CrossAttention
from bytelatent.model.utils import create_causal_mask, downsample
from bytelatent.tokenizers.blt_tokenizer import BOE_ID

logger = logging.getLogger()


class LocalModelBase(nn.Module):
    def __init__(self, args):
        super().__init__()

        self.dim = args.dim
        self.dropout = args.dropout
        self.vocab_size = args.vocab_size + args.pm_size
        self.patch_size = args.patch_size

        self.efficient_attn = args.efficient_attn
        self.sliding_window = args.sliding_window
        self.use_rope = args.use_rope
        self.init_std_factor = args.init_std_factor
        self.cross_attn_encoder = getattr(args, "cross_attn_encoder", None)
        self.cross_attn_decoder = getattr(args, "cross_attn_decoder", None)
        self.cross_attn_k = getattr(args, "cross_attn_k", None)

        self.boe_id = BOE_ID

        self.norm = RMSNorm(args.dim, eps=args.norm_eps)
        self.layers = nn.ModuleList(
            [TransformerBlock(args) for _ in range(args.n_layers)]
        )

        self.tok_embeddings = nn.Embedding(self.vocab_size, args.dim)
        if not self.use_rope:
            self.pos_embeddings = nn.Embedding(args.max_length, args.dim)
        else:
            self.rope = RotaryEmbedding(
                theta=args.rope_theta,
                head_dim=args.head_dim or args.dim // args.n_heads,
                max_seqlen=getattr(args, "max_encoder_seq_length", args.max_length),
            )
            self.pos_embeddings = None

        self.token_embedding_projection = (
            nn.Linear(args.dim_token_emb, args.dim, bias=False)
            if hasattr(args, "dim_token_emb") and args.dim_token_emb != self.dim
            else None
        )

        self.patch_embedding_projection = self._create_patch_projection(args)

    def _should_create_patch_projection(self, args):
        dimension_mismatch = (
            getattr(args, "dim_patch_emb") and args.dim_patch_emb != self.dim
        )

        # Check cross attention conditions
        cross_attn_conditions = (
            hasattr(args, "cross_attn_encoder")
            and args.cross_attn_encoder
            and getattr(args, "cross_attn_init_by_pooling")
        ) or (
            hasattr(args, "cross_attn_decoder")
            and args.cross_attn_decoder
            and getattr(args, "cross_attn_init_by_pooling")
        )

        return dimension_mismatch or cross_attn_conditions

    def _create_patch_projection(self, args):
        if not self._should_create_patch_projection(args):
            return None

        output_dim = args.dim_token_emb * (self.cross_attn_k or 1)

        return nn.Linear(
            in_features=args.dim_patch_emb,
            out_features=output_dim,
            bias=False,
        )

    def apply_embedding(self, tokens, embeds):
        if embeds is not None:
            return embeds
        else:
            return self.tok_embeddings(tokens)

    def init_weights(self, init_std=None):
        self.rope.reset_parameters()

        init_std = init_std or (self.dim ** (-0.5))
        nn.init.trunc_normal_(
            self.tok_embeddings.weight,
            mean=0.0,
            std=init_std,
            a=-3 * init_std,
            b=3 * init_std,
        )
        if self.pos_embeddings is not None:
            nn.init.trunc_normal_(
                self.pos_embeddings.weight,
                mean=0.0,
                std=init_std,
                a=-3 * init_std,
                b=3 * init_std,
            )

        for depth, layer in enumerate(self.layers):
            factor = {
                InitStdFactor.CURRENT_DEPTH: (2 * (depth + 1)) ** 0.5,
                InitStdFactor.GLOBAL_DEPTH: (2 * (len(self.layers) + 1)) ** 0.5,
                InitStdFactor.DIM_RATIO: self.dim / 4096,
                InitStdFactor.DISABLED: 1.0,
            }[self.init_std_factor]

            layer.init_weights(init_std, factor)

        if self.token_embedding_projection is not None:
            nn.init.trunc_normal_(
                self.token_embedding_projection.weight,
                mean=0.0,
                std=init_std,
                a=-3 * init_std,
                b=3 * init_std,
            )

        if self.patch_embedding_projection is not None:
            nn.init.trunc_normal_(
                self.patch_embedding_projection.weight,
                mean=0.0,
                std=init_std,
                a=-3 * init_std,
                b=3 * init_std,
            )

        if hasattr(self, "output"):
            nn.init.trunc_normal_(
                self.output.weight,
                mean=0.0,
                std=init_std,
                a=-3 * init_std,
                b=3 * init_std,
            )

        if self.cross_attn_layers is not None:
            for depth, layer in enumerate(self.cross_attn_layers):
                factor = {
                    InitStdFactor.CURRENT_DEPTH: (2 * (depth + 1)) ** 0.5,
                    InitStdFactor.GLOBAL_DEPTH: (2 * (len(self.layers) + 1)) ** 0.5,
                    InitStdFactor.DIM_RATIO: self.dim / 4096,
                    InitStdFactor.DISABLED: 1.0,
                }[self.init_std_factor]

                layer.init_weights(init_std, factor)


class LocalEncoder(LocalModelBase):
    def __init__(self, args):
        super().__init__(args)
        self.output_proj = (
            args.patching_mode in ["entropy", "probmax"]
        ) and args.entropy_model_checkpoint_dir is None

        self.apply_transformer = args.use_local_encoder_transformer
        self.downsampling_by_pooling = args.downsampling_by_pooling
        self.patch_only = args.patch_only_encoder
        self.expects_hash_embeddings = args.encoder_hash_byte_group_size is not None
        self.cross_attn_encoder = args.cross_attn_encoder
        self.cross_attn_all_layers_encoder = args.cross_attn_all_layers_encoder
        self.cross_attn_init_by_pooling = args.cross_attn_init_by_pooling
        self.cross_attn_nheads = args.cross_attn_nheads

        if self.cross_attn_encoder:
            self.cross_attn_layers = torch.nn.ModuleList()
            layers_to_add = args.n_layers if self.cross_attn_all_layers_encoder else 1
            for _ in range(layers_to_add):
                self.cross_attn_layers.append(
                    CrossAttention(
                        dim=self.dim,
                        head_dim=self.dim // self.cross_attn_nheads,
                        n_heads=self.cross_attn_nheads,
                        n_kv_heads=self.cross_attn_nheads,
                        norm_eps=args.norm_eps,
                    )
                )

    def apply_embedding(self, tokens, embeds):
        if embeds is not None:
            assert (
                self.expects_hash_embeddings
            ), "Not expecting embeddings to be passed."
            return embeds
        else:
            return self.tok_embeddings(tokens)

    def forward(
        self,
        tokens: torch.Tensor,
        embeds: Optional[torch.Tensor] = None,
        patch_embeds: Optional[torch.Tensor] = None,
        mask: Optional[Union["BlockMask", "AttentionBias", torch.Tensor, str]] = None,
        cross_mask: Optional[torch.Tensor] = None,
        num_patches: Optional[int] = None,
        patch_ids: Optional[torch.Tensor] = None,
        cache: Optional[List[Tuple[torch.Tensor, torch.Tensor, int]]] = None,
    ):
        """ """
        bs, seqlen = tokens.shape
        if mask is None:
            mask = create_causal_mask(seqlen, self.efficient_attn, self.sliding_window)

        h = self.apply_embedding(tokens, embeds)
        freqs_cis = self.rope(seqlen=seqlen) if self.use_rope else None

        h = F.dropout(h, p=self.dropout, training=self.training)

        for i, layer in enumerate(self.layers):
            h = layer(h, mask=mask, freq_cis=freqs_cis, attn_impl=self.efficient_attn)
            # check if cross attention should be applied to either all layer or only the last layer
            if self.cross_attn_encoder and (
                i == len(self.layers) - 1 or self.cross_attn_all_layers_encoder
            ):
                patch_embeds = self.apply_cross_attention(
                    h, patch_embeds, i, bs, num_patches, patch_ids, cross_mask
                )

        h_residual = patch_embeds if self.cross_attn_encoder else None
        return (h, h_residual), cache

    def apply_cross_attention(
        self, h, patch_embeds, layer_idx, bs, num_patches, patch_ids, cross_mask
    ):
        # apply pooling and project
        if self.cross_attn_init_by_pooling and patch_embeds is None:
            patch_embeds = downsample(
                h,
                num_patches,
                patch_ids=patch_ids,
                downsampling_by_pooling=self.downsampling_by_pooling,
                patch_size=self.patch_size,
            )
            if self.patch_embedding_projection is not None:
                patch_embeds = self.patch_embedding_projection(patch_embeds)
                patch_embeds = patch_embeds.reshape(
                    bs, patch_embeds.shape[1] * self.cross_attn_k, self.dim
                )

        layer_idx = layer_idx if self.cross_attn_all_layers_encoder else 0
        patch_embeds_cross = self.cross_attn_layers[layer_idx](
            x=patch_embeds,
            kv=h,
            mask=cross_mask,
        )
        patch_embeds += patch_embeds_cross
        return patch_embeds


class LocalDecoder(LocalModelBase):
    def __init__(self, args):
        super().__init__(args)

        # Model configuration flags
        self.patch_only = args.patch_only_decoder
        self.expects_embeddings = args.share_encoder_decoder_emb
        self.cross_attn_decoder = args.cross_attn_decoder
        self.cross_attn_all_layers_decoder = args.cross_attn_all_layers_decoder
        self.cross_attn_init_by_pooling = args.cross_attn_init_by_pooling
        self.cross_attn_nheads = args.cross_attn_nheads

        if self.cross_attn_decoder:
            self.cross_attn_layers = torch.nn.ModuleList()
            layers_to_add = args.n_layers if self.cross_attn_all_layers_decoder else 1
            for _ in range(layers_to_add):
                self.cross_attn_layers.append(
                    CrossAttention(
                        dim=self.dim,
                        head_dim=self.dim // self.cross_attn_nheads,
                        n_heads=self.cross_attn_nheads,
                        n_kv_heads=self.cross_attn_nheads,
                        norm_eps=args.norm_eps,
                    )
                )

        self.output = nn.Linear(
            self.dim,
            args.vocab_size,
            bias=False,
        )

    def forward(
        self,
        tokens: torch.Tensor,
        embeds: Optional[torch.Tensor],
        patch_embeds: Optional[torch.Tensor] = None,
        mask: Optional[Union["BlockMask", "AttentionBias", torch.Tensor, str]] = None,
        cross_mask: Optional[torch.Tensor] = None,
        cache: Optional[List[Tuple[torch.Tensor, torch.Tensor, int]]] = None,
    ):
        bs, seqlen = tokens.shape
        assert embeds is not None, "Embeddings must be provided"

        if mask is None:
            mask = create_causal_mask(seqlen, self.efficient_attn, self.sliding_window)

        h = embeds

        if self.patch_embedding_projection is not None:
            assert patch_embeds is not None, "Patch embeddings must be passed."
            patch_embeds = self.patch_embedding_projection(patch_embeds)
            if self.cross_attn_k is not None:
                patch_embeds = patch_embeds.reshape(
                    bs, patch_embeds.shape[1] * self.cross_attn_k, self.dim
                )

        if patch_embeds is not None and not self.cross_attn_decoder:
            h = h + patch_embeds

        freqs_cis = self.rope(seqlen=seqlen) if self.use_rope else None

        h = F.dropout(h, p=self.dropout, training=self.training)
        for i, layer in enumerate(self.layers):
            if self.cross_attn_decoder and (
                i == 0 or self.cross_attn_all_layers_decoder
            ):
                # Use cross attention to extract info from patch_embeds into h
                h_cross = self.cross_attn_layers[i](
                    x=h,
                    kv=patch_embeds,
                    mask=cross_mask,
                )
                h = h + h_cross

            h = layer(h, mask=mask, freq_cis=freqs_cis, attn_impl=self.efficient_attn)

        h_preds = self.norm(h)
        h_preds = F.dropout(h_preds, p=self.dropout, training=self.training)
        h_preds = self.output(h_preds)
        h_preds = h_preds.float()
        return h_preds, cache
