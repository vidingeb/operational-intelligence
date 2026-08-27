#!/usr/bin/env python3
"""Minimal telemetry exporter for the DGX Spark GB10.

Serves GPU utilisation, power draw and unified-memory usage as JSON so the
orchestrator can display where inference is actually running.

Deliberately stdlib-only: this runs on the inference host, and that host should
not accumulate a dependency tree just to report a wattage.

Two GB10-specific details drive the implementation:

  * ``nvidia-smi --query-gpu=memory.used`` returns [N/A]. GB10 is a unified
    memory part, so there is no discrete VRAM pool to report. Memory has to
    come from /proc/meminfo instead.
  * Ollama's size_vram is the honest figure for "how much of that unified
    memory the model is holding", so it is reported alongside.

Bind to the tailnet address only. There is no authentication here.

    python3 telemetry.py --host 100.85.206.58 --port 9101
"""

import argparse
import json
import shutil
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OLLAMA = "http://127.0.0.1:11434"
CACHE_TTL = 2.0  # seconds; nvidia-smi costs ~40ms, don't run it per request

_cache = {"at": 0.0, "data": None}

GPU_QUERY = "name,utilization.gpu,power.draw,temperature.gpu,clocks.sm"


def _num(value):
    """nvidia-smi reports unsupported fields as [N/A] — treat those as unknown."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_gpu():
    if not shutil.which("nvidia-smi"):
        return {"error": "nvidia-smi not found"}
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip().splitlines()[0]
    except Exception as exc:
        return {"error": str(exc)}

    parts = [p.strip() for p in out.split(",")]
    if len(parts) < 5:
        return {"error": f"unexpected nvidia-smi output: {out!r}"}
    return {
        "name": parts[0],
        "utilization_percent": _num(parts[1]),
        "power_watts": _num(parts[2]),
        "temperature_c": _num(parts[3]),
        "sm_clock_mhz": _num(parts[4]),
    }


def read_memory():
    """Unified memory from /proc/meminfo, in GB.

    GB10 shares one pool between CPU and GPU, so this is also the ceiling on
    model size — which is the number worth showing.
    """
    info = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0])  # kB
    except OSError as exc:
        return {"error": str(exc)}

    total = info.get("MemTotal", 0) / 1048576
    available = info.get("MemAvailable", 0) / 1048576
    if not total:
        return {"error": "could not read MemTotal"}
    return {
        "total_gb": round(total, 1),
        "used_gb": round(total - available, 1),
        "available_gb": round(available, 1),
        "used_percent": round((total - available) / total * 100, 1),
        "note": "unified CPU/GPU memory",
    }


def read_models():
    """Which models are resident, and how much memory they hold."""
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=3) as resp:
            models = json.load(resp).get("models", [])
    except Exception as exc:
        return {"error": str(exc)}
    return [
        {
            "name": m.get("name"),
            "parameters": m.get("details", {}).get("parameter_size"),
            "quantization": m.get("details", {}).get("quantization_level"),
            "resident_gb": round(m.get("size_vram", 0) / 1073741824, 1),
            "context_length": m.get("context_length"),
        }
        for m in models
    ]


def snapshot():
    now = time.time()
    if _cache["data"] and now - _cache["at"] < CACHE_TTL:
        return _cache["data"]
    data = {
        "host": "gb10",
        "timestamp": now,
        "gpu": read_gpu(),
        "memory": read_memory(),
        "models_resident": read_models(),
    }
    _cache.update(at=now, data=data)
    return data


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") not in ("", "/telemetry"):
            self.send_error(404)
            return
        body = json.dumps(snapshot()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # don't spam the journal with one line per poll


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9101)
    args = ap.parse_args()
    print(f"gb10 telemetry on {args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
