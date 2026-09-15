"""FastAPI application: serves the dashboard, exposes the run/benchmark REST
endpoints, and streams live metrics + job progress over a WebSocket.

One job runs at a time (guarded by ``_job_lock``) so the CPU picture on screen
stays interpretable — you always know what is causing the load.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, Dict, List, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import engine
from .metrics import sampler, system_info
from .models import BenchmarkRequest, RunRequest

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="HPC Performance Demonstrator")


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self) -> None:
        self.active: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self.active.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self.active.discard(ws)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self.active)
        dead: List[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self.active.discard(ws)


manager = ConnectionManager()

# Job state --------------------------------------------------------------
_job_lock = threading.Lock()
_job_running = threading.Event()
_cancel_event = threading.Event()  # set by /api/stop to abort the running job
_main_loop: asyncio.AbstractEventLoop | None = None


def _emit(frame: Dict[str, Any]) -> None:
    """Thread-safe bridge: called from the compute worker thread, schedules a
    broadcast on the main event loop."""
    if _main_loop is None:
        return
    asyncio.run_coroutine_threadsafe(manager.broadcast(frame), _main_loop)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def _on_startup() -> None:
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    sampler.start()
    asyncio.create_task(_metrics_pump())


async def _metrics_pump() -> None:
    """Push the latest system-metrics frame to all clients ~4x/second."""
    while True:
        frame = sampler.latest()
        if frame:
            await manager.broadcast(frame)
        await asyncio.sleep(0.25)


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------
@app.get("/api/sysinfo")
async def sysinfo() -> Dict[str, Any]:
    info = system_info()
    info["scaling_ladder"] = engine.default_worker_levels()
    return info


@app.post("/api/run")
async def run(req: RunRequest) -> JSONResponse:
    if _job_running.is_set():
        return JSONResponse(
            {"error": "a job is already running"}, status_code=409
        )
    params = {
        "samples": req.samples,
        "width": req.width,
        "height": req.height,
        "max_iter": req.max_iter,
    }
    _start_thread(_job_run, req.workload, params, req.workers)
    return JSONResponse({"status": "started", "workload": req.workload})


@app.post("/api/benchmark")
async def benchmark(req: BenchmarkRequest) -> JSONResponse:
    if _job_running.is_set():
        return JSONResponse(
            {"error": "a job is already running"}, status_code=409
        )
    params = {
        "samples": req.samples,
        "width": req.width,
        "height": req.height,
        "max_iter": req.max_iter,
    }
    _start_thread(_job_benchmark, req.workload, params, req.worker_levels)
    return JSONResponse({"status": "started", "workload": req.workload})


@app.post("/api/stop")
async def stop() -> JSONResponse:
    """Signal the running job to abort. It stops scheduling new work and unwinds
    within a second or two (in-flight chunks finish first)."""
    if not _job_running.is_set():
        return JSONResponse({"status": "idle"})
    _cancel_event.set()
    return JSONResponse({"status": "stopping"})


def _cancelled() -> bool:
    return _cancel_event.is_set()


def _start_thread(target, *args) -> None:
    _cancel_event.clear()
    _job_running.set()
    t = threading.Thread(target=target, args=args, daemon=True)
    t.start()


def _job_run(workload: str, params: Dict[str, Any], workers: int) -> None:
    with _job_lock:
        try:
            _emit({"type": "job_start", "mode": "run", "workload": workload, "workers": workers})
            result = engine.run_job(workload, params, workers, _emit, _cancelled)
            _emit({"type": "result", **result})
        except Exception as exc:  # keep the server alive; report to UI
            _emit({"type": "error", "message": str(exc)})
        finally:
            _job_running.clear()
            _emit({"type": "job_end", "stopped": _cancel_event.is_set()})


def _job_benchmark(workload: str, params: Dict[str, Any], levels) -> None:
    with _job_lock:
        try:
            _emit({"type": "job_start", "mode": "benchmark", "workload": workload})
            result = engine.run_scaling_benchmark(workload, params, levels, _emit, _cancelled)
            _emit(result)
        except Exception as exc:
            _emit({"type": "error", "message": str(exc)})
        finally:
            _job_running.clear()
            _emit({"type": "job_end", "stopped": _cancel_event.is_set()})


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        while True:
            # We don't expect inbound messages, but keep the socket draining so
            # disconnects are detected promptly.
            await ws.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception:
        await manager.disconnect(ws)


# Static dashboard at "/" (mounted last so it doesn't shadow the API routes).
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
