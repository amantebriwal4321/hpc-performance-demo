"""Live system metrics sampler.

A single background thread polls psutil a few times a second and keeps the most
recent frame in memory. The WebSocket layer reads ``latest()`` and pushes it to
connected browsers. Everything here is measured from the host the server runs
on — on the HPC box that is the real Xeon, in the lab it is whatever machine you
started uvicorn on.
"""

from __future__ import annotations

import platform
import threading
import time
from typing import Any, Dict, Optional

import psutil


_SAMPLE_INTERVAL = 0.25  # seconds


class MetricsSampler:
    def __init__(self, interval: float = _SAMPLE_INTERVAL) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._frame: Dict[str, Any] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Prime cpu_percent so the first real reading isn't 0.0 across the board.
        psutil.cpu_percent(percpu=True)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            per_cpu = psutil.cpu_percent(percpu=True)
            vm = psutil.virtual_memory()
            try:
                load1, load5, load15 = psutil.getloadavg()
            except (AttributeError, OSError):
                # getloadavg is unavailable on some Windows builds.
                load1 = load5 = load15 = None
            frame = {
                "type": "metrics",
                "ts": time.time(),
                "per_cpu": per_cpu,
                "cpu_overall": sum(per_cpu) / len(per_cpu) if per_cpu else 0.0,
                "mem_used_gb": round(vm.used / 1e9, 2),
                "mem_total_gb": round(vm.total / 1e9, 2),
                "mem_percent": vm.percent,
                "load_avg": [load1, load5, load15],
            }
            with self._lock:
                self._frame = frame
            time.sleep(self._interval)

    def latest(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._frame)


def system_info() -> Dict[str, Any]:
    """One-shot machine identity — proves *which* box is doing the work."""
    vm = psutil.virtual_memory()
    logical = psutil.cpu_count(logical=True) or 0
    physical = psutil.cpu_count(logical=False) or logical

    model = platform.processor() or ""
    # platform.processor() is often empty on Linux; fall back to /proc/cpuinfo.
    if not model:
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.lower().startswith("model name"):
                        model = line.split(":", 1)[1].strip()
                        break
        except OSError:
            model = platform.machine()

    freq = psutil.cpu_freq()
    return {
        "cpu_model": model or "Unknown CPU",
        "physical_cores": physical,
        "logical_cores": logical,
        "mem_total_gb": round(vm.total / 1e9, 1),
        "cpu_max_mhz": round(freq.max) if freq and freq.max else None,
        "platform": f"{platform.system()} {platform.release()}",
        "hostname": platform.node(),
    }


# Module-level singleton shared by the app.
sampler = MetricsSampler()
