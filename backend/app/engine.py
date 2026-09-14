"""Parallel execution engine.

Runs the workloads across a ProcessPoolExecutor and reports progress through a
callback. Two entry points:

* ``run_job``            — run one workload at a chosen worker count.
* ``run_scaling_benchmark`` — run the SAME total workload at a ladder of worker
  counts and measure how wall-time falls (the headline "why HPC matters" chart).

Both are synchronous/blocking and meant to be called from a background thread;
progress is surfaced via ``progress_cb`` so the WebSocket layer can stream it.
All timings use ``time.perf_counter`` — nothing is estimated.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from . import workloads

ProgressCb = Callable[[Dict[str, Any]], None]


def _noop(_: Dict[str, Any]) -> None:
    pass


def host_cpu_count() -> int:
    return os.cpu_count() or 1


def default_worker_levels() -> List[int]:
    """A scaling ladder clamped to the real host: 1, 4, 8, 16, ... , all cores.

    On a 72-thread Xeon this yields [1, 4, 8, 16, 32, 64, 72]; on a 4-core
    laptop it collapses to [1, 2, 4] — honest on whatever hardware runs it.
    """
    total = host_cpu_count()
    ladder = [1, 2, 4, 8, 16, 32, 64, 128, 256]
    levels = [n for n in ladder if n < total]
    levels.append(total)
    # De-dup while preserving order.
    seen = set()
    out: List[int] = []
    for n in levels:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def run_job(
    workload: str,
    params: Dict[str, Any],
    workers: int,
    progress_cb: ProgressCb = _noop,
) -> Dict[str, Any]:
    """Execute one workload across ``workers`` processes. Returns a result dict
    including the measured wall-time."""
    workers = max(1, min(workers, 512))
    if workload == "monte_carlo":
        return _run_monte_carlo(params, workers, progress_cb)
    if workload == "mandelbrot":
        return _run_mandelbrot(params, workers, progress_cb)
    raise ValueError(f"unknown workload: {workload}")


def _run_monte_carlo(
    params: Dict[str, Any], workers: int, progress_cb: ProgressCb
) -> Dict[str, Any]:
    total_samples = int(params.get("samples", 50_000_000))
    # More chunks than workers → good load balancing, smoother progress.
    n_chunks = max(workers * 4, workers)
    tasks = workloads.monte_carlo_plan(total_samples, n_chunks)

    results: List[int] = []
    done_chunks = 0
    done_samples = 0
    t0 = time.perf_counter()

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(workloads.monte_carlo_chunk, n, seed): n for n, seed in tasks
        }
        for fut in as_completed(futures):
            hits = fut.result()
            n = futures[fut]
            results.append(hits)
            done_chunks += 1
            done_samples += n
            elapsed = time.perf_counter() - t0
            progress_cb(
                {
                    "type": "progress",
                    "workload": "monte_carlo",
                    "chunks_done": done_chunks,
                    "chunks_total": len(tasks),
                    "tasks_done": done_samples,
                    "tasks_total": total_samples,
                    "elapsed": elapsed,
                    "throughput": done_samples / elapsed if elapsed > 0 else 0,
                    "pi_running": 4.0 * sum(results) / done_samples
                    if done_samples
                    else 0.0,
                }
            )

    wall = time.perf_counter() - t0
    reduced = workloads.monte_carlo_reduce(results, total_samples)
    return {
        "workload": "monte_carlo",
        "workers": workers,
        "wall_time": wall,
        "throughput": total_samples / wall if wall > 0 else 0,
        **reduced,
    }


def _run_mandelbrot(
    params: Dict[str, Any], workers: int, progress_cb: ProgressCb
) -> Dict[str, Any]:
    width = int(params.get("width", 1000))
    height = int(params.get("height", 800))
    max_iter = int(params.get("max_iter", 400))
    n_chunks = max(workers * 4, workers)
    bands = workloads.mandelbrot_plan(height, n_chunks)

    done_chunks = 0
    done_rows = 0
    t0 = time.perf_counter()

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for tag, (rs, re) in enumerate(bands):
            fut = pool.submit(
                workloads.mandelbrot_tile, rs, re, width, height, max_iter, tag
            )
            futures[fut] = (rs, re)
        for fut in as_completed(futures):
            tile = fut.result()
            rs, re = futures[fut]
            done_chunks += 1
            done_rows += re - rs
            elapsed = time.perf_counter() - t0
            # Stream the finished band so the browser can paint it live.
            progress_cb(
                {
                    "type": "progress",
                    "workload": "mandelbrot",
                    "chunks_done": done_chunks,
                    "chunks_total": len(bands),
                    "tasks_done": done_rows,
                    "tasks_total": height,
                    "elapsed": elapsed,
                    "throughput": (done_rows * width) / elapsed if elapsed > 0 else 0,
                    "tile": {
                        "row_start": tile["row_start"],
                        "row_end": tile["row_end"],
                        "worker_tag": tile["worker_tag"],
                        "rows": tile["rows"],
                    },
                    "width": width,
                    "height": height,
                    "max_iter": max_iter,
                }
            )

    wall = time.perf_counter() - t0
    return {
        "workload": "mandelbrot",
        "workers": workers,
        "wall_time": wall,
        "throughput": (width * height) / wall if wall > 0 else 0,
        "pixels": width * height,
        "width": width,
        "height": height,
    }


def run_scaling_benchmark(
    workload: str,
    params: Dict[str, Any],
    worker_levels: Optional[List[int]] = None,
    progress_cb: ProgressCb = _noop,
) -> Dict[str, Any]:
    """Run the same workload at each worker level; measure speedup + efficiency."""
    total = host_cpu_count()
    if not worker_levels:
        worker_levels = default_worker_levels()
    # Clamp every level to the real host and keep the list honest.
    worker_levels = sorted({min(max(1, n), total) for n in worker_levels})

    rows: List[Dict[str, Any]] = []
    baseline: Optional[float] = None

    for level in worker_levels:
        progress_cb(
            {
                "type": "benchmark_level_start",
                "workers": level,
                "levels": worker_levels,
            }
        )
        # For the benchmark we only care about wall-time per level, so swallow
        # the fine-grained per-chunk progress from the inner run.
        result = run_job(workload, params, level, _noop)
        wall = result["wall_time"]
        if baseline is None:
            baseline = wall
        speedup = baseline / wall if wall > 0 else 0.0
        efficiency = speedup / level if level > 0 else 0.0
        row = {
            "workers": level,
            "wall_time": round(wall, 4),
            "speedup": round(speedup, 3),
            "efficiency": round(efficiency, 3),
            "throughput": round(result.get("throughput", 0), 1),
        }
        rows.append(row)
        progress_cb({"type": "benchmark_level_done", "row": row, "levels": worker_levels})

    return {
        "type": "benchmark_complete",
        "workload": workload,
        "host_cores": total,
        "rows": rows,
    }
