"""
Simple chat web UI for the On-Prem AI Orchestrator.
Serves a single-page chat interface on port 8091.
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import os
import httpx

app = FastAPI(title="On-Prem AI Chat")

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8090").rstrip("/")

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>On-Prem AI — VMware Assistant</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            height: 100vh;
            display: flex;
            flex-direction: column;
        }
        header {
            background: #16213e;
            padding: 1rem 2rem;
            border-bottom: 1px solid #0f3460;
            display: flex;
            align-items: center;
            gap: 1rem;
        }
        header h1 {
            font-size: 1.3rem;
            color: #4fc3f7;
        }
        header .badge {
            background: #0f3460;
            color: #81d4fa;
            padding: 0.2rem 0.6rem;
            border-radius: 12px;
            font-size: 0.75rem;
        }
        #chat-container {
            flex: 1;
            overflow-y: auto;
            padding: 2rem;
            display: flex;
            flex-direction: column;
            gap: 1rem;
        }
        .message {
            max-width: 80%;
            padding: 1rem 1.2rem;
            border-radius: 12px;
            line-height: 1.5;
            white-space: pre-wrap;
        }
        .message.user {
            align-self: flex-end;
            background: #0f3460;
            color: #e0e0e0;
        }
        .message.assistant {
            align-self: flex-start;
            background: #1e3a5f;
            color: #f0f0f0;
            border: 1px solid #2a4a7f;
        }
        .message .model-tag {
            display: inline-block;
            background: #0f3460;
            color: #81d4fa;
            padding: 0.1rem 0.4rem;
            border-radius: 4px;
            font-size: 0.7rem;
            margin-bottom: 0.5rem;
        }
        .message.error {
            align-self: flex-start;
            background: #3e1a1a;
            color: #ff8a80;
            border: 1px solid #5e2a2a;
        }
        .message.thinking {
            align-self: flex-start;
            background: #1e3a5f;
            color: #81d4fa;
            border: 1px solid #2a4a7f;
            font-style: italic;
        }
        #input-area {
            padding: 1rem 2rem;
            background: #16213e;
            border-top: 1px solid #0f3460;
            display: flex;
            gap: 0.8rem;
            align-items: center;
        }
        #model-select, #scope-select {
            padding: 0.6rem 0.8rem;
            border: 1px solid #0f3460;
            border-radius: 8px;
            background: #1a1a2e;
            color: #4fc3f7;
            font-size: 0.85rem;
            outline: none;
            cursor: pointer;
        }
        #model-select:focus, #scope-select:focus { border-color: #4fc3f7; }
        #scope-select { color: #b0bec5; }
        #user-input {
            flex: 1;
            padding: 0.8rem 1rem;
            border: 1px solid #0f3460;
            border-radius: 8px;
            background: #1a1a2e;
            color: #eee;
            font-size: 1rem;
            outline: none;
        }
        #user-input:focus {
            border-color: #4fc3f7;
        }
        #send-btn {
            padding: 0.8rem 1.5rem;
            background: #4fc3f7;
            color: #1a1a2e;
            border: none;
            border-radius: 8px;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
        }
        #send-btn:hover { background: #81d4fa; }
        #send-btn:disabled {
            background: #2a4a7f;
            color: #666;
            cursor: not-allowed;
        }
        .info-bar {
            padding: 0.5rem 2rem;
            background: #0f3460;
            font-size: 0.8rem;
            color: #81d4fa;
            display: flex;
            gap: 2rem;
        }
        #timer {
            color: #ffcc02;
            font-weight: bold;
        }
        .gpu-strip {
            margin-left: auto;
            display: flex;
            gap: 14px;
            align-items: center;
            font-variant-numeric: tabular-nums;
        }
        .gpu-strip .metric { display: flex; gap: 5px; align-items: baseline; }
        .gpu-strip .metric b { color: #76b900; font-weight: 600; }
        .gpu-strip .label { opacity: 0.6; font-size: 0.85em; }
        .gpu-dot {
            width: 7px; height: 7px; border-radius: 50%;
            background: #76b900; display: inline-block;
        }
        .gpu-dot.busy { animation: pulse 1s ease-in-out infinite; }
        .gpu-dot.stale { background: #888; }
        @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.25; } }
        .usage-bar {
            margin-top: 10px;
            padding-top: 8px;
            border-top: 1px solid rgba(255,255,255,0.12);
            font-size: 0.78em;
            opacity: 0.75;
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            font-variant-numeric: tabular-nums;
        }
        .usage-bar .tools { opacity: 0.8; font-style: italic; }
    </style>
</head>
<body>
    <header>
        <h1>🖥️ On-Prem AI Assistant</h1>
        <span class="badge">vCenter · VCF Ops · Networks</span>
        <span class="badge">100% On-Premises</span>
    </header>
    <div class="info-bar">
        <span>LLM: {{LLM_BACKEND}}</span>
        <span>APIs: {{API_BACKEND}}</span>
        <span id="timer"></span>
        <span id="gpu-strip" class="gpu-strip"></span>
    </div>
    <div id="chat-container">
        <div class="message assistant">Hello! I'm your on-premises VMware infrastructure assistant. I can query vCenter, VCF Operations, and VCF Networks — all running locally with no cloud dependency.

Try asking me:
• "What VMs are running?"
• "Are there any critical alerts?"
• "Show me host resource usage"
• "Which datastores are low on space?"

💡 Tip: Use the model selector below — 8B is fast (~30-60s), 70B is smarter but slower (~3-5min).</div>
    </div>
    <div id="input-area">
        <select id="model-select"><option value="">Loading models...</option></select>
        <select id="scope-select" title="Which systems the assistant may query"></select>
        <input type="text" id="user-input" placeholder="Ask about your VMware infrastructure..." autofocus>
        <button id="send-btn" onclick="sendMessage()">Send</button>
    </div>

    <script>
        const chatContainer = document.getElementById('chat-container');
        const userInput = document.getElementById('user-input');
        const sendBtn = document.getElementById('send-btn');
        const modelSelect = document.getElementById('model-select');
        const scopeSelect = document.getElementById('scope-select');
        const timerEl = document.getElementById('timer');
        let timerInterval = null;

        userInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });

        function startTimer() {
            let seconds = 0;
            timerEl.textContent = '⏱ 0s';
            timerInterval = setInterval(() => {
                seconds++;
                timerEl.textContent = '⏱ ' + seconds + 's';
            }, 1000);
        }

        function stopTimer() {
            if (timerInterval) {
                clearInterval(timerInterval);
                timerInterval = null;
            }
        }

        async function sendMessage() {
            const message = userInput.value.trim();
            if (!message) return;

            const model = modelSelect.value;

            addMessage(message, 'user');
            userInput.value = '';
            sendBtn.disabled = true;

            const modelLabel = modelSelect.options[modelSelect.selectedIndex].text;
            const thinkingEl = addMessage('Thinking with ' + modelLabel + '...', 'thinking');
            startTimer();

            try {
                const response = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message, model, scope: scopeSelect.value || 'all' }),
                });

                stopTimer();
                chatContainer.removeChild(thinkingEl);

                if (response.ok) {
                    const data = await response.json();
                    addMessage(data.answer, 'assistant', data.model, data);
                    refreshTelemetry();
                } else {
                    const err = await response.json();
                    addMessage('Error: ' + (err.detail || 'Unknown error'), 'error');
                }
            } catch (e) {
                stopTimer();
                chatContainer.removeChild(thinkingEl);
                addMessage('Connection error: ' + e.message, 'error');
            }

            sendBtn.disabled = false;
            userInput.focus();
        }

        function addMessage(text, type, model, data) {
            const div = document.createElement('div');
            div.className = 'message ' + type;
            if (model && type === 'assistant') {
                const tag = document.createElement('div');
                tag.className = 'model-tag';
                tag.textContent = model;
                div.appendChild(tag);
                div.appendChild(document.createTextNode('\\n' + text));
                const usage = buildUsageBar(data);
                if (usage) div.appendChild(usage);
            } else {
                div.textContent = text;
            }
            chatContainer.appendChild(div);
            chatContainer.scrollTop = chatContainer.scrollHeight;
            return div;
        }

        // --- token accounting -------------------------------------------------

        function buildUsageBar(data) {
            const u = data && data.usage;
            if (!u || !u.total_tokens) return null;

            const bar = document.createElement('div');
            bar.className = 'usage-bar';

            const parts = [
                u.total_tokens.toLocaleString() + ' tokens',
                u.prompt_tokens.toLocaleString() + ' in / ' +
                    u.completion_tokens.toLocaleString() + ' out',
            ];
            if (u.tokens_per_second) parts.push(u.tokens_per_second + ' tok/s');
            if (u.rounds > 1) parts.push(u.rounds + ' model rounds');
            if (u.total_seconds) parts.push(u.total_seconds + 's');

            parts.forEach(t => {
                const s = document.createElement('span');
                s.textContent = t;
                bar.appendChild(s);
            });

            const calls = (data.tools_called || []);
            if (calls.length) {
                const s = document.createElement('span');
                s.className = 'tools';
                // Same tool can be called more than once across rounds; show counts.
                const counts = {};
                calls.forEach(c => { counts[c] = (counts[c] || 0) + 1; });
                s.textContent = 'tools: ' + Object.entries(counts)
                    .map(([n, c]) => c > 1 ? n + '×' + c : n).join(', ');
                bar.appendChild(s);
            }

            const gpu = data.telemetry && data.telemetry.gpu;
            if (gpu && gpu.power_watts != null) {
                const s = document.createElement('span');
                s.textContent = gpu.power_watts.toFixed(1) + ' W on ' +
                    (gpu.name || 'GPU');
                bar.appendChild(s);
            }
            return bar;
        }

        // --- live inference-host telemetry -----------------------------------

        const gpuStrip = document.getElementById('gpu-strip');

        function metric(label, value, cls) {
            return '<span class="metric ' + (cls || '') + '">' +
                   '<b>' + value + '</b><span class="label">' + label + '</span></span>';
        }

        async function refreshTelemetry() {
            try {
                const r = await fetch('/api/telemetry');
                if (!r.ok) throw new Error('http ' + r.status);
                const t = await r.json();
                if (t.enabled === false || t.error || !t.gpu || t.gpu.error) {
                    gpuStrip.innerHTML = '';
                    return;
                }
                const busy = (t.gpu.utilization_percent || 0) > 20;
                let html = '<span class="gpu-dot' + (busy ? ' busy' : '') + '"></span>';
                html += metric('GPU', Math.round(t.gpu.utilization_percent) + '%');
                html += metric('', t.gpu.power_watts.toFixed(1) + ' W');
                if (t.gpu.temperature_c != null) {
                    html += metric('', Math.round(t.gpu.temperature_c) + '°C');
                }
                if (t.memory && t.memory.total_gb) {
                    html += metric('unified',
                        t.memory.used_gb.toFixed(0) + '/' + t.memory.total_gb.toFixed(0) + ' GB');
                }
                const resident = (t.models_resident || []).filter(m => m.resident_gb);
                if (resident.length) {
                    html += metric('resident', resident[0].resident_gb.toFixed(0) + ' GB');
                }
                gpuStrip.innerHTML = html;
            } catch (e) {
                gpuStrip.innerHTML = '';  // telemetry is optional; fail quietly
            }
        }

        refreshTelemetry();
        // Both dropdowns are filled from the orchestrator rather than hardcoded.
        // The model list used to live in this file, which is why a model
        // installed on the host never showed up here.
        async function loadOptions() {
            try {
                const [models, scopes, config] = await Promise.all([
                    fetch('/api/models').then(r => r.json()),
                    fetch('/api/scopes').then(r => r.json()),
                    fetch('/api/config').then(r => r.json()).catch(() => ({}))
                ]);

                modelSelect.innerHTML = '';
                const entries = Object.entries(models)
                    .filter(([, m]) => m.installed !== false);
                if (!entries.length) {
                    modelSelect.innerHTML = '<option value="">No models installed</option>';
                } else {
                    for (const [id, meta] of entries) {
                        const opt = document.createElement('option');
                        opt.value = id;
                        opt.textContent = meta.name || id;
                        opt.title = meta.description || '';
                        if (id === config.default_model) opt.selected = true;
                        modelSelect.appendChild(opt);
                    }
                }

                scopeSelect.innerHTML = '';
                for (const s of scopes) {
                    const opt = document.createElement('option');
                    opt.value = s.id;
                    opt.textContent = s.label + ' (' + s.tool_count + ')';
                    opt.title = s.summary;
                    scopeSelect.appendChild(opt);
                }
            } catch (e) {
                modelSelect.innerHTML = '<option value="">Could not load models</option>';
                scopeSelect.innerHTML = '<option value="all">All systems</option>';
            }
        }
        loadOptions();

        setInterval(refreshTelemetry, 5000);
    </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    """Render the chat page, labelling the backends actually in use.

    Asks the orchestrator rather than duplicating its config, so a split-site
    deployment shows the real inference host instead of a stale hardcoded IP.
    Uses /config, not /health, so page load never waits on probe timeouts.
    """
    llm, apis = "unknown", "unknown"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            cfg = (await client.get(f"{ORCHESTRATOR_URL}/config")).json()
        llm = cfg.get("ollama_url", "unknown")
        apis = cfg.get("mcp_server", "unknown")
    except Exception:
        pass  # the page is still usable; the banner just says unknown

    return HTML_PAGE.replace("{{LLM_BACKEND}}", llm).replace("{{API_BACKEND}}", apis)


@app.post("/api/chat")
async def chat(request: dict):
    """Proxy to the orchestrator."""
    timeout = 600.0 if "70b" in request.get("model", "") else 300.0
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{ORCHESTRATOR_URL}/chat",
            json={
                "message": request["message"],
                "model": request.get("model"),
                "scope": request.get("scope", "all"),
            },
        )
        response.raise_for_status()
        return response.json()


@app.get("/api/models")
async def models():
    """Models the inference host actually has, for the model dropdown."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{ORCHESTRATOR_URL}/models")
        response.raise_for_status()
        return response.json()


@app.get("/api/scopes")
async def scopes():
    """Tool scopes, for the system dropdown."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{ORCHESTRATOR_URL}/scopes")
        response.raise_for_status()
        return response.json()


@app.get("/api/config")
async def config():
    """Backend configuration, used to preselect the default model."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{ORCHESTRATOR_URL}/config")
            response.raise_for_status()
            return response.json()
    except Exception:
        return {}


@app.get("/api/telemetry")
async def telemetry():
    """Proxy inference-host telemetry for the live GPU strip.

    Short timeout and a soft failure: the strip is decoration, and a slow or
    absent exporter must never stall the page.
    """
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            response = await client.get(f"{ORCHESTRATOR_URL}/telemetry")
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        return {"enabled": False, "error": str(exc)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8091)
