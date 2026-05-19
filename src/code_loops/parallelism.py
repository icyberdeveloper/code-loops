"""Memory-bounded parallel execution helper.

Reason: when stages fan out N parallel `claude --print` subprocesses,
each consumes 200-500MB (Node.js + stream buffer + state). On a small VM
(< 4 GB RAM, no swap), running 4 at once triggers OOM-SIGKILL — the kernel
silently kills one process, which surfaces as `rc=1 + empty stderr` and
takes down the entire stage via `asyncio.gather`.

Mitigation: cap concurrency to PARALLEL_AGENTS via batched gather. Each
batch waits for all members before starting the next. Slower than full
parallel by ~(N/chunk) factor but bounded memory pressure.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

# Hard cap on simultaneous `claude --print` subprocesses across the pipeline.
# Tuned for VMs in the 3-4 GB RAM / 0 swap range. Bump on larger hosts only
# after measuring `free -h` headroom during a real run.
PARALLEL_AGENTS = 2

T = TypeVar("T")


async def gather_chunked(
    awaitables: list[Awaitable[T]], chunk_size: int = PARALLEL_AGENTS
) -> list[T]:
    """Run awaitables with at most `chunk_size` in flight at any time.

    Results returned in input order, same shape as `asyncio.gather(*awaitables)`.
    Each batch's gather still raises on first exception within the batch,
    matching gather's fail-fast semantics.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    results: list[T] = []
    for i in range(0, len(awaitables), chunk_size):
        batch = awaitables[i : i + chunk_size]
        results.extend(await asyncio.gather(*batch))
    return results
