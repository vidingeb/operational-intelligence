"""
Benchmark installed Ollama models with the same prompt(s) and print a ranked
table (tokens/sec, generate time, load time). Runs entirely on your Mac.

Usage:
  python3 benchmark.py                      # benchmark every installed model
  python3 benchmark.py qwen2.5:7b llama3.2  # only these models
  PROMPT="Explain vMotion" python3 benchmark.py
  RUNS=3 python3 benchmark.py               # average over N timed runs

Each model gets one warm-up run (to load into memory / exclude load time),
then RUNS timed runs that are averaged.
"""

import os
import sys
import time

import httpx

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
PROMPT = os.environ.get(
    "PROMPT",
    "Explain what a hypervisor is and give two examples, in about 120 words.",
)
RUNS = int(os.environ.get("RUNS", "2"))


def installed_models():
    r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=10.0)
    r.raise_for_status()
    return sorted(m["name"] for m in r.json().get("models", []))


def run_once(model):
    """One generation. Returns (tokens_per_sec, gen_s, load_s, out_tokens)."""
    r = httpx.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": model,
            "messages": [{"role": "user", "content": PROMPT}],
            "stream": False,
        },
        timeout=1200.0,
    )
    r.raise_for_status()
    d = r.json()
    eval_count = d.get("eval_count", 0)
    eval_s = (d.get("eval_duration") or 0) / 1e9
    load_s = (d.get("load_duration") or 0) / 1e9
    tps = eval_count / eval_s if eval_s > 0 else 0.0
    return tps, eval_s, load_s, eval_count


def benchmark(model):
    # Warm-up (loads the model; result ignored).
    warm_load = run_once(model)[2]
    tps_list, gen_list, out_list = [], [], []
    for _ in range(RUNS):
        tps, gen_s, _load, out = run_once(model)
        tps_list.append(tps)
        gen_list.append(gen_s)
        out_list.append(out)
    avg = lambda xs: sum(xs) / len(xs)
    return {
        "model": model,
        "tokens_per_sec": avg(tps_list),
        "gen_seconds": avg(gen_list),
        "out_tokens": avg(out_list),
        "load_seconds": warm_load,
    }


def main():
    targets = sys.argv[1:] or installed_models()
    if not targets:
        print("No models installed. Pull one, e.g.: ollama pull llama3.2")
        return

    print(f"Ollama: {OLLAMA_URL}")
    print(f"Prompt: {PROMPT!r}")
    print(f"Runs per model (after warm-up): {RUNS}\n")

    results = []
    for m in targets:
        print(f"  benchmarking {m} …", flush=True)
        try:
            results.append(benchmark(m))
        except Exception as exc:  # noqa: BLE001
            print(f"    skipped ({exc})")

    results.sort(key=lambda r: r["tokens_per_sec"], reverse=True)

    print(f"\n{'MODEL':<24}{'TOK/S':>9}{'GEN s':>9}{'LOAD s':>9}{'OUT tok':>9}")
    print("-" * 60)
    for r in results:
        print(
            f"{r['model']:<24}"
            f"{r['tokens_per_sec']:>9.1f}"
            f"{r['gen_seconds']:>9.1f}"
            f"{r['load_seconds']:>9.1f}"
            f"{r['out_tokens']:>9.0f}"
        )
    print("\nHigher TOK/S = faster generation. LOAD s is a one-time cost per model.")


if __name__ == "__main__":
    main()
