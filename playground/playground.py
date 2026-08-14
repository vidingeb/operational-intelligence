"""
Local LLM Playground — a zero-dependency-on-your-lab web UI for trying out
different Ollama models on your Mac and comparing their performance.

- Auto-detects whatever models you've pulled (`ollama pull ...`)
- Simple chat interface with a model picker
- Shows performance for every response: total time, tokens/sec, and how long
  the model took to load vs. generate

Run:  python3 playground.py   (then open http://localhost:8095)
"""

import os
import time
import json

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
PORT = int(os.environ.get("PLAYGROUND_PORT", "8095"))

app = FastAPI(title="Local LLM Playground")


@app.get("/api/models")
async def list_models():
    """Return the models currently installed in Ollama."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Can't reach Ollama at {OLLAMA_URL}. Is it running? ({exc})",
        )
    models = [m["name"] for m in resp.json().get("models", [])]
    return {"models": sorted(models)}


@app.post("/api/chat")
async def chat(request: dict, http_request: Request):
    """Send one message to a model and return the reply plus timing metrics.

    Streams from Ollama so that if the browser aborts the request (Stop
    button), the disconnect propagates here, we close the Ollama connection,
    and generation actually halts instead of running to completion.
    """
    model = request.get("model")
    message = request.get("message", "").strip()
    system = request.get("system", "").strip()
    if not model or not message:
        raise HTTPException(status_code=400, detail="model and message are required")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": message})

    started = time.time()
    answer_parts = []
    final = {}
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            async with client.stream(
                "POST",
                f"{OLLAMA_URL}/api/chat",
                json={"model": model, "messages": messages, "stream": True},
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    # If the browser stopped waiting, abort generation.
                    if await http_request.is_disconnected():
                        raise httpx.ReadError("client disconnected")
                    chunk = json.loads(line)
                    answer_parts.append(chunk.get("message", {}).get("content", ""))
                    if chunk.get("done"):
                        final = chunk
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        # A client-initiated stop is expected; surface others as 502.
        if await http_request.is_disconnected():
            raise HTTPException(status_code=499, detail="stopped by client")
        raise HTTPException(status_code=502, detail=f"Ollama error: {exc}")

    wall = time.time() - started

    # Ollama returns durations in nanoseconds.
    def ns_to_s(v):
        return (v or 0) / 1_000_000_000

    eval_count = final.get("eval_count", 0)
    eval_s = ns_to_s(final.get("eval_duration"))
    load_s = ns_to_s(final.get("load_duration"))
    prompt_tokens = final.get("prompt_eval_count", 0)
    tokens_per_sec = round(eval_count / eval_s, 1) if eval_s > 0 else None

    return {
        "model": model,
        "answer": "".join(answer_parts),
        "metrics": {
            "wall_seconds": round(wall, 2),
            "load_seconds": round(load_s, 2),
            "generate_seconds": round(eval_s, 2),
            "prompt_tokens": prompt_tokens,
            "output_tokens": eval_count,
            "tokens_per_sec": tokens_per_sec,
        },
    }


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Local LLM Playground</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #16161e; color: #e6e6ef; height: 100vh; display: flex; flex-direction: column; }
  header { background: #1e1e2a; padding: 0.9rem 1.5rem; border-bottom: 1px solid #2c2c3c;
           display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
  header h1 { font-size: 1.15rem; color: #8ab4f8; }
  header .sub { font-size: 0.8rem; color: #8a8a9a; }
  #chat { flex: 1; overflow-y: auto; padding: 1.5rem; display: flex; flex-direction: column; gap: 0.9rem; }
  .msg { max-width: 80%; padding: 0.8rem 1rem; border-radius: 12px; line-height: 1.5; white-space: pre-wrap; }
  .msg.user { align-self: flex-end; background: #2a3a5f; }
  .msg.assistant { align-self: flex-start; background: #23233230; background: #23233a; border: 1px solid #33334a; }
  .msg.error { align-self: flex-start; background: #3e1a1a; color: #ff8a80; border: 1px solid #5e2a2a; }
  .msg.thinking { align-self: flex-start; color: #8ab4f8; font-style: italic; }
  .msg .tag { display: inline-block; background: #2c2c3c; color: #8ab4f8; padding: 0.1rem 0.45rem;
              border-radius: 5px; font-size: 0.7rem; margin-bottom: 0.45rem; }
  .msg .metrics { margin-top: 0.6rem; font-size: 0.72rem; color: #9a9aae; border-top: 1px solid #33334a;
                  padding-top: 0.45rem; display: flex; gap: 1rem; flex-wrap: wrap; }
  .metrics b { color: #c6c6d6; }
  #controls { padding: 0.8rem 1.5rem; background: #1e1e2a; border-top: 1px solid #2c2c3c;
              display: flex; gap: 0.6rem; align-items: center; flex-wrap: wrap; }
  select, input, textarea { background: #16161e; color: #e6e6ef; border: 1px solid #33334a;
              border-radius: 8px; padding: 0.6rem 0.8rem; font-size: 0.9rem; outline: none; font-family: inherit; }
  select:focus, input:focus, textarea:focus { border-color: #8ab4f8; }
  #model { color: #8ab4f8; cursor: pointer; min-width: 180px; }
  #input { flex: 1; min-width: 220px; }
  button { background: #8ab4f8; color: #16161e; border: none; border-radius: 8px; padding: 0.6rem 1.2rem;
           font-weight: 600; cursor: pointer; font-size: 0.9rem; }
  button:hover { background: #a9c8fb; }
  button:disabled { background: #3a3a4c; color: #777; cursor: not-allowed; }
  #system { width: 100%; resize: vertical; min-height: 0; height: 2.4rem; }
  .row { display: flex; gap: 0.6rem; align-items: center; width: 100%; flex-wrap: wrap; }
</style>
</head>
<body>
  <header>
    <h1>🧪 Local LLM Playground</h1>
    <span class="sub">Ollama on your Mac · pick a model · compare speed</span>
  </header>
  <div id="chat">
    <div class="msg assistant">Pick a model below and ask anything. Every reply shows how fast it ran
(tokens/sec, load time, generate time) so you can compare models.

No models in the dropdown? Pull some in a terminal, e.g.:
  ollama pull llama3.1:8b
  ollama pull qwen2.5:7b
then click ⟳ Refresh.</div>
  </div>
  <div id="controls">
    <div class="row">
      <textarea id="system" placeholder="Optional system prompt (e.g. 'You are a concise assistant.')"></textarea>
    </div>
    <select id="model"><option>loading…</option></select>
    <button id="refresh" onclick="loadModels()" title="Reload installed models">⟳</button>
    <input type="text" id="input" placeholder="Type a message and press Enter…" autofocus>
    <button id="send" onclick="send()">Send</button>
    <button id="stop" onclick="stop()" style="display:none;background:#e06a6a">Stop</button>
  </div>
<script>
  const chat = document.getElementById('chat');
  const input = document.getElementById('input');
  const sysEl = document.getElementById('system');
  const modelEl = document.getElementById('model');
  const sendBtn = document.getElementById('send');
  const stopBtn = document.getElementById('stop');
  let controller = null;

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });

  function stop() {
    if (controller) controller.abort();
  }

  async function loadModels() {
    try {
      const r = await fetch('/api/models');
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'error');
      if (!d.models.length) { modelEl.innerHTML = '<option value="">no models — run: ollama pull …</option>'; return; }
      modelEl.innerHTML = d.models.map(m => `<option value="${m}">${m}</option>`).join('');
    } catch (e) {
      modelEl.innerHTML = `<option value="">${e.message}</option>`;
    }
  }

  function addMsg(text, type, opts = {}) {
    const div = document.createElement('div');
    div.className = 'msg ' + type;
    if (opts.model) {
      const tag = document.createElement('div');
      tag.className = 'tag'; tag.textContent = opts.model; div.appendChild(tag);
    }
    div.appendChild(document.createTextNode(text));
    if (opts.metrics) {
      const m = opts.metrics;
      const el = document.createElement('div');
      el.className = 'metrics';
      const tps = m.tokens_per_sec != null ? m.tokens_per_sec : '—';
      el.innerHTML =
        `<span><b>${tps}</b> tok/s</span>` +
        `<span><b>${m.wall_seconds}s</b> total</span>` +
        `<span>load <b>${m.load_seconds}s</b></span>` +
        `<span>gen <b>${m.generate_seconds}s</b></span>` +
        `<span><b>${m.output_tokens}</b> out / <b>${m.prompt_tokens}</b> in tok</span>`;
      div.appendChild(el);
    }
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
    return div;
  }

  async function send() {
    const message = input.value.trim();
    const model = modelEl.value;
    if (!message) return;
    if (!model) { addMsg('Select a model first.', 'error'); return; }

    addMsg(message, 'user');
    input.value = '';
    sendBtn.disabled = true;
    stopBtn.style.display = '';
    const thinking = addMsg('Thinking with ' + model + '…', 'thinking');
    controller = new AbortController();

    try {
      const r = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model, message, system: sysEl.value }),
        signal: controller.signal,
      });
      const d = await r.json();
      chat.removeChild(thinking);
      if (r.ok) addMsg(d.answer, 'assistant', { model: d.model, metrics: d.metrics });
      else addMsg('Error: ' + (d.detail || 'unknown'), 'error');
    } catch (e) {
      chat.removeChild(thinking);
      if (e.name === 'AbortError') addMsg('⏹ Stopped.', 'error');
      else addMsg('Connection error: ' + e.message, 'error');
    }
    controller = null;
    sendBtn.disabled = false;
    stopBtn.style.display = 'none';
    input.focus();
  }

  loadModels();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT)
