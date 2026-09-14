/* HPC Performance Demonstrator — client.
   Opens a WebSocket, routes frames by `type`, drives the CPU grid, counters,
   workload output and the scaling charts. No framework, no build step. */

const $ = (id) => document.getElementById(id);
const fmt = (n) => Number(n).toLocaleString("en-US");
const fmtInt = (n) => Number(n).toLocaleString("en-US", { maximumFractionDigits: 0 });

let SYS = { logical_cores: 8 };
let coreCells = [];
let benchRunning = false;

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
async function boot() {
  try {
    const res = await fetch("/api/sysinfo");
    SYS = await res.json();
  } catch (e) {
    SYS = { cpu_model: "Unknown", logical_cores: 8, physical_cores: 4, mem_total_gb: 0 };
  }
  renderBanner();
  buildCpuGrid(SYS.logical_cores || 8);
  configureControls();
  connectWs();
  initCharts();
}

function renderBanner() {
  const phys = SYS.physical_cores || "?";
  const log = SYS.logical_cores || "?";
  $("machine-banner").innerHTML =
    `<strong>${SYS.cpu_model || "CPU"}</strong><br>` +
    `${phys} cores / ${log} threads · ${SYS.mem_total_gb || "?"} GB RAM · ${SYS.hostname || ""}`;
  $("core-sub").textContent = `${log} logical cores`;
}

function configureControls() {
  const slider = $("workers");
  slider.max = SYS.logical_cores || 8;
  slider.value = Math.min(4, SYS.logical_cores || 4);
  $("worker-label").textContent = slider.value;
  slider.addEventListener("input", () => ($("worker-label").textContent = slider.value));

  $("workload").addEventListener("change", onWorkloadChange);
  $("btn-run").addEventListener("click", startRun);
  $("btn-bench").addEventListener("click", startBenchmark);
  onWorkloadChange(); // apply initial show/hide for the selected workload
}

function onWorkloadChange() {
  const w = $("workload").value;
  document.querySelectorAll(".mc-only").forEach((el) => (el.hidden = w !== "monte_carlo"));
  document.querySelectorAll(".mandel-only").forEach((el) => (el.hidden = w !== "mandelbrot"));
  $("pi-output").hidden = w !== "monte_carlo";
  $("mandel-canvas").hidden = w !== "mandelbrot";
  $("output-title").textContent = w === "monte_carlo" ? "π estimate" : "Mandelbrot render";
}

// ---------------------------------------------------------------------------
// CPU grid
// ---------------------------------------------------------------------------
function buildCpuGrid(n) {
  const grid = $("cpu-grid");
  grid.innerHTML = "";
  coreCells = [];
  const cols = n > 36 ? 12 : n > 16 ? 8 : Math.min(n, 8);
  grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
  for (let i = 0; i < n; i++) {
    const cell = document.createElement("div");
    cell.className = "core";
    const bar = document.createElement("div");
    bar.className = "bar";
    const lbl = document.createElement("div");
    lbl.className = "lbl";
    lbl.textContent = i;
    cell.appendChild(bar);
    cell.appendChild(lbl);
    grid.appendChild(cell);
    coreCells.push(bar);
  }
}

function heat(pct) {
  // green → amber → red as utilisation climbs.
  if (pct < 50) return "#22c55e";
  if (pct < 85) return "#f59e0b";
  return "#ef4444";
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------
function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => $("conn-dot").classList.add("on");
  ws.onclose = () => {
    $("conn-dot").classList.remove("on");
    setTimeout(connectWs, 1500); // auto-reconnect
  };
  ws.onmessage = (ev) => handleFrame(JSON.parse(ev.data));
}

function handleFrame(f) {
  switch (f.type) {
    case "metrics": return onMetrics(f);
    case "progress": return onProgress(f);
    case "result": return onResult(f);
    case "job_start": return onJobStart(f);
    case "job_end": return onJobEnd(f);
    case "benchmark_level_start": return onBenchLevelStart(f);
    case "benchmark_level_done": return onBenchLevelDone(f);
    case "benchmark_complete": return onBenchComplete(f);
    case "error": return alert("Error: " + f.message);
  }
}

// ---------------------------------------------------------------------------
// Frame handlers
// ---------------------------------------------------------------------------
function onMetrics(f) {
  const per = f.per_cpu || [];
  for (let i = 0; i < coreCells.length && i < per.length; i++) {
    const pct = per[i];
    coreCells[i].style.height = pct + "%";
    coreCells[i].style.background = heat(pct);
  }
  $("s-cpu").textContent = (f.cpu_overall || 0).toFixed(0) + "%";
  $("s-ram").textContent = `${f.mem_used_gb} / ${f.mem_total_gb} GB`;
}

function onJobStart(f) {
  setButtons(true);
  if (f.mode === "run") {
    $("s-workers").textContent = f.workers;
    if (f.workload === "monte_carlo") {
      $("pi-value").textContent = "π = …";
      $("pi-error").textContent = "";
      $("pi-bar").style.width = "0%";
    } else {
      prepMandel();
    }
  }
}

function onJobEnd() {
  setButtons(false);
  benchRunning = false;
}

function onProgress(f) {
  const pct = f.tasks_total ? (100 * f.tasks_done) / f.tasks_total : 0;
  $("s-progress").textContent = pct.toFixed(0) + "%";
  $("s-tput").textContent = fmtInt(f.throughput) + (f.workload === "monte_carlo" ? " s/s" : " px/s");
  $("s-elapsed").textContent = f.elapsed.toFixed(2) + "s";

  if (f.workload === "monte_carlo") {
    $("pi-value").textContent = "π = " + f.pi_running.toFixed(6);
    $("pi-bar").style.width = pct + "%";
  } else if (f.tile) {
    paintTile(f.tile, f.width, f.height, f.max_iter);
  }
}

function onResult(f) {
  if (f.workload === "monte_carlo") {
    $("pi-value").textContent = "π = " + f.pi_estimate.toFixed(6);
    $("pi-error").textContent =
      `error ${f.error.toExponential(2)} · ${fmt(f.total_samples)} samples · ` +
      `${f.wall_time.toFixed(2)}s · ${fmtInt(f.throughput)} samples/s`;
    $("pi-bar").style.width = "100%";
  } else {
    $("pi-error").textContent = `${fmt(f.pixels)} px · ${f.wall_time.toFixed(2)}s`;
  }
  $("s-progress").textContent = "100%";
}

// ---------------------------------------------------------------------------
// Mandelbrot canvas
// ---------------------------------------------------------------------------
let mctx = null, mImg = null, mW = 0, mH = 0;
function prepMandel() {
  const sel = $("mandel-size").value.split("x");
  mW = parseInt(sel[0]); mH = parseInt(sel[1]);
  const cv = $("mandel-canvas");
  cv.width = mW; cv.height = mH;
  mctx = cv.getContext("2d");
  mctx.fillStyle = "#000";
  mctx.fillRect(0, 0, mW, mH);
}

function paintTile(tile, width, height, maxIter) {
  if (!mctx) return;
  const bandH = tile.row_end - tile.row_start;
  const img = mctx.createImageData(width, bandH);
  const hue = (tile.worker_tag * 47) % 360; // colour by worker → parallelism visible
  for (let r = 0; r < bandH; r++) {
    const row = tile.rows[r];
    for (let c = 0; c < width; c++) {
      const it = row[c];
      const idx = (r * width + c) * 4;
      let rr, gg, bb;
      if (it >= maxIter) {
        rr = gg = bb = 0; // inside the set
      } else {
        const light = 25 + 55 * (it / maxIter); // brighter = escaped faster/slower
        [rr, gg, bb] = hslToRgb(hue, 65, light);
      }
      img.data[idx] = rr; img.data[idx + 1] = gg; img.data[idx + 2] = bb; img.data[idx + 3] = 255;
    }
  }
  mctx.putImageData(img, 0, tile.row_start);
}

function hslToRgb(h, s, l) {
  s /= 100; l /= 100;
  const k = (n) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return [Math.round(255 * f(0)), Math.round(255 * f(8)), Math.round(255 * f(4))];
}

// ---------------------------------------------------------------------------
// Run / benchmark triggers
// ---------------------------------------------------------------------------
function paramsFromControls() {
  const [w, h, it] = ($("mandel-size").value || "1200x900x600").split("x").map(Number);
  return {
    workload: $("workload").value,
    workers: parseInt($("workers").value),
    samples: parseInt($("samples").value),
    width: w, height: h, max_iter: it,
  };
}

async function startRun() {
  const p = paramsFromControls();
  await post("/api/run", p);
}

async function startBenchmark() {
  benchRunning = true;
  resetCharts();
  const p = paramsFromControls();
  await post("/api/benchmark", {
    workload: p.workload, samples: p.samples,
    width: p.width, height: p.height, max_iter: p.max_iter,
  });
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 409) alert("A job is already running — wait for it to finish.");
}

function setButtons(running) {
  $("btn-run").disabled = running;
  $("btn-bench").disabled = running;
}

// ---------------------------------------------------------------------------
// Benchmark handlers + charts
// ---------------------------------------------------------------------------
let timeChart, speedupChart;
function initCharts() {
  const gridColor = "#223047", tick = "#90a1b8";
  const base = {
    responsive: true,
    plugins: { legend: { labels: { color: "#e6edf6" } } },
    scales: {
      x: { grid: { color: gridColor }, ticks: { color: tick }, title: { display: true, text: "workers", color: tick } },
      y: { grid: { color: gridColor }, ticks: { color: tick }, beginAtZero: true },
    },
  };
  timeChart = new Chart($("time-chart"), {
    type: "bar",
    data: { labels: [], datasets: [{ label: "wall time (s)", data: [], backgroundColor: "#3ba0ff" }] },
    options: JSON.parse(JSON.stringify(base)),
  });
  speedupChart = new Chart($("speedup-chart"), {
    type: "line",
    data: { labels: [], datasets: [
      { label: "measured speedup", data: [], borderColor: "#22c55e", backgroundColor: "#22c55e", tension: .2 },
      { label: "ideal (linear)", data: [], borderColor: "#ffb020", borderDash: [6, 5], pointRadius: 0 },
    ] },
    options: JSON.parse(JSON.stringify(base)),
  });
}

function resetCharts() {
  for (const ch of [timeChart, speedupChart]) {
    ch.data.labels = [];
    ch.data.datasets.forEach((d) => (d.data = []));
    ch.update();
  }
  $("bench-table").hidden = true;
  $("bench-table").querySelector("tbody").innerHTML = "";
}

function onBenchLevelStart(f) {
  setButtons(true);
  $("s-workers").textContent = f.workers;
  $("s-progress").textContent = `benchmarking ${f.workers}…`;
}

function onBenchLevelDone(f) {
  const r = f.row;
  timeChart.data.labels.push(r.workers);
  timeChart.data.datasets[0].data.push(r.wall_time);
  timeChart.update();

  speedupChart.data.labels.push(r.workers);
  speedupChart.data.datasets[0].data.push(r.speedup);
  speedupChart.data.datasets[1].data.push(r.workers); // ideal = N
  speedupChart.update();

  const tb = $("bench-table").querySelector("tbody");
  const tr = document.createElement("tr");
  tr.innerHTML = `<td>${r.workers}</td><td>${r.wall_time.toFixed(3)}</td>` +
    `<td>${r.speedup.toFixed(2)}×</td><td>${(r.efficiency * 100).toFixed(0)}%</td>`;
  tb.appendChild(tr);
  $("bench-table").hidden = false;
}

function onBenchComplete(f) {
  $("s-progress").textContent = "benchmark done";
}

boot();
