"""Request / response schemas."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


Workload = Literal["monte_carlo", "mandelbrot"]


class RunRequest(BaseModel):
    workload: Workload = "monte_carlo"
    workers: int = Field(default=4, ge=1, le=512)
    # Monte Carlo: total number of random samples.
    samples: int = Field(default=50_000_000, ge=1_000)
    # Mandelbrot: image dimensions + iteration depth.
    width: int = Field(default=1000, ge=64, le=4000)
    height: int = Field(default=800, ge=64, le=4000)
    max_iter: int = Field(default=400, ge=10, le=5000)


class BenchmarkRequest(BaseModel):
    workload: Workload = "monte_carlo"
    samples: int = Field(default=50_000_000, ge=1_000)
    width: int = Field(default=800, ge=64, le=4000)
    height: int = Field(default=600, ge=64, le=4000)
    max_iter: int = Field(default=400, ge=10, le=5000)
    # If omitted, the server builds a sensible ladder clamped to the host.
    worker_levels: Optional[List[int]] = None
