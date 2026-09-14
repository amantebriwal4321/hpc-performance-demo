"""CPU-bound compute kernels.

Every function here is a *pure*, module-level, picklable callable so it can be
shipped to a ProcessPoolExecutor worker. The work is deliberately written as
plain-Python loops (no NumPy) so each worker pegs one core to 100% — that is
what makes the live CPU grid light up during a demo.

Nothing here is faked: Monte Carlo returns real random-sample hit counts and
Mandelbrot returns real escape-time iteration counts.
"""

from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Tuple


# ---------------------------------------------------------------------------
# Monte Carlo estimation of pi
# ---------------------------------------------------------------------------
def monte_carlo_chunk(n_samples: int, seed: int) -> int:
    """Throw ``n_samples`` random darts at the unit square, return how many
    land inside the quarter circle. Aggregated across all chunks:

        pi ~= 4 * total_hits / total_samples

    ``seed`` keeps each worker's stream independent (and the whole run
    reproducible if you want it to be).
    """
    rng = random.Random(seed)
    hits = 0
    for _ in range(n_samples):
        x = rng.random()
        y = rng.random()
        if x * x + y * y <= 1.0:
            hits += 1
    return hits


def monte_carlo_plan(total_samples: int, n_chunks: int) -> List[Tuple[int, int]]:
    """Split ``total_samples`` into ``n_chunks`` (n_samples, seed) tasks.

    We create more chunks than workers so the pool self-balances: a worker that
    finishes early just grabs the next chunk.
    """
    base = total_samples // n_chunks
    rem = total_samples % n_chunks
    tasks: List[Tuple[int, int]] = []
    for i in range(n_chunks):
        count = base + (1 if i < rem else 0)
        if count > 0:
            tasks.append((count, 1000 + i))
    return tasks


def monte_carlo_reduce(results: List[int], total_samples: int) -> Dict[str, Any]:
    total_hits = sum(results)
    pi = 4.0 * total_hits / total_samples if total_samples else 0.0
    return {
        "pi_estimate": pi,
        "error": abs(pi - math.pi),
        "total_hits": total_hits,
        "total_samples": total_samples,
    }


# ---------------------------------------------------------------------------
# Mandelbrot set render (tiled)
# ---------------------------------------------------------------------------
# A fixed, pretty viewport over the classic Mandelbrot set.
MANDEL_X_MIN, MANDEL_X_MAX = -2.5, 1.0
MANDEL_Y_MIN, MANDEL_Y_MAX = -1.25, 1.25


def mandelbrot_tile(
    row_start: int,
    row_end: int,
    width: int,
    height: int,
    max_iter: int,
    worker_tag: int,
) -> Dict[str, Any]:
    """Compute escape-time iterations for image rows [row_start, row_end).

    Returns the raw iteration counts for those rows plus ``worker_tag`` so the
    UI can tint each tile by which task produced it — making the parallel
    decomposition literally visible as the image fills in.
    """
    rows: List[List[int]] = []
    dx = (MANDEL_X_MAX - MANDEL_X_MIN) / width
    dy = (MANDEL_Y_MAX - MANDEL_Y_MIN) / height
    for py in range(row_start, row_end):
        y0 = MANDEL_Y_MIN + py * dy
        row: List[int] = []
        for px in range(width):
            x0 = MANDEL_X_MIN + px * dx
            x = 0.0
            y = 0.0
            it = 0
            while x * x + y * y <= 4.0 and it < max_iter:
                x, y = x * x - y * y + x0, 2.0 * x * y + y0
                it += 1
            row.append(it)
        rows.append(row)
    return {
        "row_start": row_start,
        "row_end": row_end,
        "rows": rows,
        "worker_tag": worker_tag,
    }


def mandelbrot_plan(height: int, n_chunks: int) -> List[Tuple[int, int]]:
    """Split the image into ``n_chunks`` horizontal bands of rows."""
    band = max(1, math.ceil(height / n_chunks))
    tasks: List[Tuple[int, int]] = []
    start = 0
    tag = 0
    while start < height:
        end = min(start + band, height)
        tasks.append((start, end))
        start = end
        tag += 1
    return tasks
