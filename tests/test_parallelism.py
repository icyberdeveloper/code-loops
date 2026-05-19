"""Tests for memory-bounded parallel execution helper."""

from __future__ import annotations

import asyncio
import time

import pytest

from code_loops.parallelism import PARALLEL_AGENTS, gather_chunked


def test_default_chunk_size_is_two():
    """If this changes intentionally, update README cost estimates + run a
    `free -h` check to verify new ceiling fits target VM."""
    assert PARALLEL_AGENTS == 2


def test_gather_chunked_returns_in_input_order():
    async def make(i: int) -> int:
        await asyncio.sleep(0.001)
        return i * 10

    results = asyncio.run(gather_chunked([make(i) for i in range(5)], chunk_size=2))
    assert results == [0, 10, 20, 30, 40]


def test_gather_chunked_respects_chunk_size():
    """Track peak concurrent in-flight via a shared counter."""
    in_flight = 0
    peak = 0

    async def slow():
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return None

    asyncio.run(gather_chunked([slow() for _ in range(6)], chunk_size=2))
    assert peak == 2, f"chunk_size=2 should cap concurrency at 2, observed peak={peak}"


def test_gather_chunked_handles_empty_input():
    results = asyncio.run(gather_chunked([], chunk_size=2))
    assert results == []


def test_gather_chunked_single_chunk_smaller_than_size():
    async def f(i: int) -> int:
        return i

    results = asyncio.run(gather_chunked([f(1)], chunk_size=4))
    assert results == [1]


def test_gather_chunked_rejects_zero_chunk_size():
    with pytest.raises(ValueError, match="chunk_size must be >= 1"):
        asyncio.run(gather_chunked([], chunk_size=0))


def test_gather_chunked_propagates_exception():
    async def boom():
        raise RuntimeError("kaboom")

    async def ok():
        return 1

    # chunk=2: first batch = [ok, boom], second batch never starts after raise
    with pytest.raises(RuntimeError, match="kaboom"):
        asyncio.run(gather_chunked([ok(), boom()], chunk_size=2))


def test_gather_chunked_batches_sequentially():
    """Total wall time for 4 tasks at chunk=2, each 50ms, is ~100ms (2 batches),
    not ~50ms (all parallel) and not ~200ms (all sequential)."""

    async def slow():
        await asyncio.sleep(0.05)
        return None

    start = time.monotonic()
    asyncio.run(gather_chunked([slow() for _ in range(4)], chunk_size=2))
    elapsed = time.monotonic() - start
    # 2 batches × 50ms = ~100ms; allow 80-180ms window for scheduler jitter
    assert 0.08 < elapsed < 0.18, f"expected ~100ms, got {elapsed:.3f}s"
