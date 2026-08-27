#!/usr/bin/env bash
# Run the whole assistant on this machine: local model, local APIs, local UI.
#
# Two modes:
#   ./run-local.sh              replay recorded fixtures, no network needed
#   ./run-local.sh live <host>  point at a real API host, e.g. a customer's
#
# Ollama must already be running locally (`ollama serve`).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"

MODE="${1:-replay}"
MODEL="${OPS_MODEL:-gpt-oss:20b}"
OLLAMA="${OLLAMA_URL:-http://localhost:11434}"

if ! curl -sf -m 5 "$OLLAMA/api/tags" >/dev/null; then
    echo "Ollama is not responding at $OLLAMA - start it with: ollama serve" >&2
    exit 1
fi

if ! curl -s -m 5 "$OLLAMA/api/tags" | grep -q "\"${MODEL%%:*}"; then
    echo "Model $MODEL is not present. Pull it with: ollama pull $MODEL" >&2
    exit 1
fi

pids=()
cleanup() {
    for pid in "${pids[@]:-}"; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM

if [ "$MODE" = "live" ]; then
    MCP="${2:-}"
    [ -n "$MCP" ] || { echo "Usage: $0 live http://api-host" >&2; exit 1; }
    echo "Using live APIs at $MCP"
else
    echo "Replaying fixtures from $HERE/fixtures"
    python3 "$HERE/replay.py" &
    pids+=($!)
    sleep 2
    MCP="http://localhost"
fi

export OLLAMA_URL="$OLLAMA"
export MCP_SERVER="$MCP"
export DEFAULT_MODEL="$MODEL"
export ORCHESTRATOR_URL="http://localhost:8090"

echo "Starting orchestrator (model $MODEL)"
python3 "$REPO/orchestrator/orchestrator.py" &
pids+=($!)

for _ in $(seq 1 20); do
    curl -sf -m 2 http://localhost:8090/config >/dev/null && break
    sleep 1
done

echo "Starting web UI"
python3 "$REPO/orchestrator/web_ui.py" &
pids+=($!)
sleep 2

echo
echo "  UI       http://localhost:8091"
echo "  API      http://localhost:8090"
echo "  Model    $MODEL via $OLLAMA"
echo "  Backend  $MCP"
echo
echo "Ctrl-C to stop everything."
wait
