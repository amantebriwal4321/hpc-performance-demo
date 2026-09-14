# HPC Performance Demonstrator

An interactive web app that **actually runs** a heavy, parallel CPU workload on the
machine hosting it and shows, live, what the hardware is doing. Point it at a laptop
and it uses the laptop's cores; deploy it to the college HPC server and it uses all 72
threads of the dual Xeon Gold 6140 — and the dashboard makes the difference obvious.

The story it tells, top to bottom:

> **this is the workload → this is how much work the HPC is doing → these are the cores
> being used → this is how fast it finishes → this is why an HPC server is useful.**

## What it does

- **Two selectable workloads**, both embarrassingly parallel:
  - **Monte Carlo π** — estimates π by random sampling; the cleanest scaling story.
  - **Mandelbrot render** — a fractal image fills in tile-by-tile, each band **coloured
    by the worker that computed it**, so you literally see the parallel decomposition.
- **Live per-core CPU grid** (green → amber → red), RAM, throughput, progress, elapsed —
  all measured with `psutil` and `time.perf_counter`. **No numbers are faked.**
- **Scaling Benchmark** — runs the *same* workload at 1 → 4 → 8 → … → all cores and charts
  measured wall-time, **speedup** vs. the ideal linear line, and **parallel efficiency**.

## How it works

```
Lab browser ──HTTP──▶ FastAPI (serves the UI + REST)
            ◀─WebSocket─  live per-core CPU %, RAM, progress, throughput
                              │
                              ▼
                    ProcessPoolExecutor(max_workers=N)   ← runs on the HPC host
                              │  real OS processes → one core each at ~100%
                              ▼
                    aggregated result (π / image) + measured timings
```

Real *processes* (not threads) sidestep Python's GIL, so each worker saturates one core —
which is exactly what lights up the CPU grid. The workers are deliberately plain-Python
loops so the CPU load is visible and the speedup curve is textbook.

## Run locally (laptop)

**Docker:**
```bash
docker compose up --build
# open http://localhost:8000
```

**Plain Python (no Docker):**
```bash
# Linux / macOS
bash run.sh

# Windows (PowerShell)
cd backend
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

The worker slider and the benchmark ladder auto-clamp to the host's real core count, so on
a 4-core laptop you'll see `[1, 2, 4]` and on the 72-thread server `[1, 4, 8, 16, 32, 64, 72]`.

## Deploy to the HPC server (over SSH)

**Docker (preferred):**
```bash
ssh user@hpc-server
git clone <your-repo-url> && cd hpc-demonstrator
docker compose up --build -d
# browse http://hpc-server:8000
```

**Plain venv (if Docker/sudo isn't available):**
```bash
ssh user@hpc-server
git clone <your-repo-url> && cd hpc-demonstrator
bash run.sh              # serves on 0.0.0.0:8000
```

### Reaching it from lab computers
- The server binds `0.0.0.0:8000`. If port 8000 is open on the LAN, lab machines just browse
  `http://<server-ip>:8000`.
- If the port is firewalled, use an SSH tunnel from the lab machine:
  ```bash
  ssh -L 8000:localhost:8000 user@hpc-server
  # then browse http://localhost:8000 on the lab machine
  ```

### Notes
- `docker-compose.yml` sets **no CPU/memory limits** on purpose, so the container sees the
  whole machine. Add limits only if you want to deliberately cap the demo.
- Use a **single** uvicorn process (the default). Don't pass `--workers` — the API process
  itself owns the compute pool; forking the API would not fork the pool.
- If it turns out you're on a VPS slice rather than the bare metal, the machine banner and
  the benchmark honestly report only the cores you actually have.
- This is a CPU demo; a GPU on the host is irrelevant to it.

## Demo script (for the lab)
1. Show the **machine banner** — the real Xeon, 72 threads, 252 GB.
2. Run **Monte Carlo π** at 1 worker → note the time and the single busy core.
3. Run it at all cores → watch the whole grid go red and the time collapse.
4. Hit **Scaling Benchmark** → the speedup curve climbs toward the ideal line; read the
   efficiency column. That chart is the "why HPC matters" takeaway.
5. Switch to **Mandelbrot** for the visual: the image fills in bands, each coloured by its
   worker — parallelism you can point at.
6. Cross-check against `htop` in a second SSH window to prove the dashboard is real.
```bash
htop
```
