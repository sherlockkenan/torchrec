#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict


import functools
import timeit
from typing import Any, Callable, Dict, List

import click

import torch
from torchrec.distributed.benchmark.benchmark_utils import benchmark, BenchmarkResult
from torchrec.sparse.jagged_tensor import (
    _regroup_keyed_tensors,
    KeyedJaggedTensor,
    KeyedTensor,
)
from torchrec.sparse.tests.utils import build_groups, build_kts


class DummyModel(torch.nn.Module):
    # pyre-ignore
    def forward(self, *args, **kwargs) -> None:
        pass


def bench(
    name: str,
    labels: torch.Tensor,
    batch_size: int,
    feature_count: int,
    device_type: str,
    run_backward: bool,
    fn: Callable[..., List[torch.Tensor]],
    fn_kwargs: Dict[str, Any],
) -> None:

    # initial call
    fn(**fn_kwargs)

    def wrapped_func(
        model: torch.nn.Module,  # not used
        bench_inputs: List[KeyedJaggedTensor],  # not used
        fn: Callable[..., List[torch.Tensor]],
        fn_kwargs: Dict[str, Any],
        run_backward: bool,
    ) -> None:
        result = fn(**fn_kwargs)
        if run_backward:
            vectors = [tensor.sum(dim=1) for tensor in result]
            pred = vectors[0]
            for vector in vectors[1:]:
                pred.mul(vector)
            loss = torch.nn.functional.l1_loss(pred, labels)
            loss.sum().backward()

    if device_type == "cuda":
        result = benchmark(
            name=name,
            model=DummyModel(),
            warmup_inputs=[],
            bench_inputs=[],
            prof_inputs=[],
            world_size=1,
            output_dir="",
            num_benchmarks=20,
            func_to_benchmark=functools.partial(
                wrapped_func, fn=fn, run_backward=run_backward, fn_kwargs=fn_kwargs
            ),
            benchmark_func_kwargs={},
            rank=0,
            enable_logging=False,
        )

    else:  # cpu
        model = DummyModel()
        times = timeit.repeat(
            lambda: wrapped_func(
                model=model,
                bench_inputs=[],
                fn=fn,
                fn_kwargs=fn_kwargs,
                run_backward=run_backward,
            ),
            number=1,
            repeat=20,
        )
        result = BenchmarkResult(
            short_name=name,
            elapsed_time=torch.tensor(times),
            max_mem_allocated=[0],
        )

    print(
        f"  {name : <{35}} | B: {batch_size : <{8}} | F: {feature_count : <{8}} | device: {device_type : <{8}} | Runtime (P90): {result.runtime_percentile(90):5.1f} ms | Memory (P90): {result.max_mem_percentile(90):5.1f}"
    )


@click.command()
@click.option(
    "--cuda_matrix",
    type=bool,
    default=False,
    help="Run a full GPU matrix, overrides relevant settings",
)
@click.option(
    "--run_backward",
    type=bool,
    default=False,
    help="run backward (forward always runs)",
)
@click.option(
    "--device_type",
    type=str,
    default="cuda",
    help="device type",
)
@click.option(
    "--n_dense",
    type=int,
    default=20,
    help="Total number of dense embeddings.",
)
@click.option(
    "--dim_dense",
    type=int,
    default=64,
    help="Dim dense embedding.",
)
@click.option(
    "--n_sparse",
    default=1000,
    help="Total number of sparse embeddings to be used.",
)
@click.option(
    "--dim_sparse",
    type=int,
    default=128,
    help="Dim dense embedding.",
)
@click.option(
    "--batch_size",
    type=int,
    default=1024,
    help="Batch size.",
)
@click.option(
    "--n_groups",
    type=int,
    default=2,
    help="Total num of regrouping",
)
def main(
    cuda_matrix: bool,
    run_backward: bool,
    device_type: str,
    n_dense: int,
    n_sparse: int,
    dim_dense: int,
    dim_sparse: int,
    batch_size: int,
    n_groups: int,
) -> None:
    if cuda_matrix:
        n_denses = [64, 128, 256, 512, 1024]
        n_sparses = [16, 32, 64, 128, 256]
        batch_sizes = [512, 1024, 2048, 4096]
        device_types = ["cuda"]
    else:
        n_denses = [n_dense]
        n_sparses = [n_sparse]
        batch_sizes = [batch_size]
        device_types = [device_type]

    for device_type in device_types:
        for batch_size in batch_sizes:
            for n_dense, n_sparse in zip(n_denses, n_sparses):

                device = torch.device(device_type)
                kts = build_kts(
                    n_dense,
                    n_sparse,
                    dim_dense,
                    dim_sparse,
                    batch_size,
                    device,
                    run_backward,
                )
                labels = torch.randint(
                    0, 1, (batch_size,), device=torch.device(device_type)
                ).float()
                groups = build_groups(kts, n_groups)
                bench(
                    "[fallback] _regroup_keyed_tenors",
                    labels,
                    batch_size,
                    n_dense + n_sparse,
                    device_type,
                    run_backward,
                    _regroup_keyed_tensors,
                    {"keyed_tensors": kts, "groups": groups},
                )
                bench(
                    "[prod] KeyedTensor.regroup",
                    labels,
                    batch_size,
                    n_dense + n_sparse,
                    device_type,
                    run_backward,
                    KeyedTensor.regroup,
                    {"keyed_tensors": kts, "groups": groups},
                )


if __name__ == "__main__":
    main()
