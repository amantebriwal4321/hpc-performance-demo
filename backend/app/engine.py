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
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    wait as futures_wait,
)
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


# A cancel is a zero-arg callable returning True when the job should stop.
CancelFn = Optional[Callable[[], bool]]


def _stop(cancel: CancelFn) -> bool:
    return bool(cancel and cancel())


def _shutdown_pool(pool: ProcessPoolExecutor, stopped: bool) -> None:
    """Tear down the process pool. On a normal finish, wait for the (already
    complete) workers. On a cancel, terminate the worker processes immediately
    so CPU load — and fan noise — drops at once instead of letting in-flight
    chunks run to completion."""
    if stopped:
        for proc in list(getattr(pool, "_processes", {}).values()):
            try:
                proc.terminate()
            except Exception:
                pass
        pool.shutdown(wait=False, cancel_futures=True)
    else:
        pool.shutdown(wait=True)


def run_job(
    workload: str,
    params: Dict[str, Any],
    workers: int,
    progress_cb: ProgressCb = _noop,
    cancel: CancelFn = None,
) -> Dict[str, Any]:
    """Execute one workload across ``workers`` processes. Returns a result dict
    including the measured wall-time. If ``cancel()`` starts returning True the
    run stops scheduling new work and returns what it has, marked ``stopped``."""
    workers = max(1, min(workers, 512))
    if workload == "monte_carlo":
        return _run_monte_carlo(params, workers, progress_cb, cancel)
    if workload == "mandelbrot":
        return _run_mandelbrot(params, workers, progress_cb, cancel)
    raise ValueError(f"unknown workload: {workload}")


def _run_monte_carlo(
    params: Dict[str, Any], workers: int, progress_cb: ProgressCb, cancel: CancelFn = None
) -> Dict[str, Any]:
    total_samples = int(params.get("samples", 50_000_000))
    # More chunks than workers → good load balancing, smoother progress.
    n_chunks = max(workers * 4, workers)
    tasks = workloads.monte_carlo_plan(total_samples, n_chunks)

    results: List[int] = []
    done_chunks = 0
    done_samples = 0
    stopped = False
    t0 = time.perf_counter()

    pool = ProcessPoolExecutor(max_workers=workers)
    try:
        futures = {
            pool.submit(workloads.monte_carlo_chunk, n, seed): n for n, seed in tasks
        }
        pending = set(futures)
        while pending:
            if _stop(cancel):
                stopped = True
                break
            # Poll so we can react to a cancel every ~0.25s even while every
            # worker is mid-chunk on a huge job.
            done, pending = futures_wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            for fut in done:
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
    finally:
        _shutdown_pool(pool, stopped)

    wall = time.perf_counter() - t0
    counted = done_samples if stopped else total_samples
    reduced = workloads.monte_carlo_reduce(results, max(counted, 1))
    return {
        "workload": "monte_carlo",
        "workers": workers,
        "wall_time": wall,
        "throughput": counted / wall if wall > 0 else 0,
        "stopped": stopped,
        **reduced,
    }


def _run_mandelbrot(
    params: Dict[str, Any], workers: int, progress_cb: ProgressCb, cancel: CancelFn = None
) -> Dict[str, Any]:
    width = int(params.get("width", 1000))
    height = int(params.get("height", 800))
    max_iter = int(params.get("max_iter", 400))
    n_chunks = max(workers * 4, workers)
    bands = workloads.mandelbrot_plan(height, n_chunks)

    done_chunks = 0
    done_rows = 0
    stopped = False
    t0 = time.perf_counter()

    pool = ProcessPoolExecutor(max_workers=workers)
    try:
        futures = {}
        for tag, (rs, re) in enumerate(bands):
            fut = pool.submit(
                workloads.mandelbrot_tile, rs, re, width, height, max_iter, tag
            )
            futures[fut] = (rs, re)
        pending = set(futures)
        while pending:
            if _stop(cancel):
                stopped = True
                break
            done, pending = futures_wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            for fut in done:
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
    finally:
        _shutdown_pool(pool, stopped)

    wall = time.perf_counter() - t0
    done_pixels = done_rows * width
    return {
        "workload": "mandelbrot",
        "workers": workers,
        "wall_time": wall,
        "throughput": done_pixels / wall if wall > 0 else 0,
        "pixels": done_pixels if stopped else width * height,
        "width": width,
        "height": height,
        "stopped": stopped,
    }


def _warmup(workload: str) -> None:
    """Run a tiny, untimed instance of the workload to absorb one-time
    process-pool startup and cold-cache costs before the real measurements."""
    try:
        if workload == "mandelbrot":
            run_job("mandelbrot", {"width": 160, "height": 120, "max_iter": 80}, 2, _noop)
        else:
            run_job("monte_carlo", {"samples": 400_000}, 2, _noop)
    except Exception:
        pass  # a warm-up failure must never break the benchmark


def run_scaling_benchmark(
    workload: str,
    params: Dict[str, Any],
    worker_levels: Optional[List[int]] = None,
    progress_cb: ProgressCb = _noop,
    cancel: CancelFn = None,
) -> Dict[str, Any]:
    """Run the same workload at each worker level; measure speedup + efficiency."""
    total = host_cpu_count()
    if not worker_levels:
        worker_levels = default_worker_levels()
    # Clamp every level to the real host and keep the list honest.
    worker_levels = sorted({min(max(1, n), total) for n in worker_levels})

    # The benchmark runs the workload once PER level (including the slow
    # 1-worker level), so a huge Run-sized workload would make it take minutes.
    # It measures the SCALING RATIO, not raw throughput, so cap the per-level
    # size to something that still keeps every core busy but finishes quickly.
    params = dict(params)
    if workload == "monte_carlo":
        params["samples"] = min(int(params.get("samples", 40_000_000)), 40_000_000)
    else:
        params["width"] = min(int(params.get("width", 1200)), 1200)
        params["height"] = min(int(params.get("height", 900)), 900)
        params["max_iter"] = min(int(params.get("max_iter", 500)), 500)

    # Warm-up: the FIRST ProcessPoolExecutor of the run pays a one-time cost
    # (spawning Python interpreters + re-importing modules, especially on
    # Windows), plus cold OS/disk caches. Left unpaid, that cost lands entirely
    # on the 1-worker baseline and makes every later level look superlinear
    # (speedup > N, efficiency > 100%). A tiny throwaway run absorbs it so every
    # timed level starts on equal footing.
    _warmup(workload)

    rows: List[Dict[str, Any]] = []
    baseline: Optional[float] = None

    for level in worker_levels:
        if _stop(cancel):
            break
        progress_cb(
            {
                "type": "benchmark_level_start",
                "workers": level,
                "levels": worker_levels,
            }
        )
        # For the benchmark we only care about wall-time per level, so swallow
        # the fine-grained per-chunk progress from the inner run. We take the
        # BEST (min) of a couple of trials: a transient background spike (a
        # browser, another app) can inflate a single run — worst on the
        # 1-worker baseline, which would then make later levels look superlinear
        # (efficiency > 100%). The minimum is the least-contended, most
        # representative measure of the true cost.
        # The 1-worker baseline is the reference every speedup divides by, and
        # the most contention-sensitive, so give it an extra trial.
        trials = 3 if level <= 1 else 2
        best = None
        for _ in range(trials):
            if _stop(cancel):
                break
            result = run_job(workload, params, level, _noop, cancel)
            if best is None or result["wall_time"] < best["wall_time"]:
                best = result
        if best is None or _stop(cancel):
            break
        result = best
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
