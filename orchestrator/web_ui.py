"""
Simple chat web UI for the On-Prem AI Orchestrator.
Serves a single-page chat interface on port 8091.
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
import os
import httpx

app = FastAPI(title="On-Prem AI Chat")

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8090").rstrip("/")

# --- Authentication -----------------------------------------------------------
#
# The tailnet is a network control, not an identity one: it says which machines
# can open a socket, not who is driving. This adds the identity half using the
# headers `tailscale serve` injects on every proxied request.
#
# Two facts make that safe, both verified rather than assumed:
#   1. Tailscale *overwrites* these headers. A client that sends its own
#      Tailscale-User-Login through the proxy gets the real one instead.
#   2. The proxy connects over loopback, so a request arriving from anywhere
#      else did not pass through it and its headers are attacker-controlled.
#
# Hence the two rules below. The loopback check is what stops the whole scheme
# from being bypassed by binding the app to 0.0.0.0 and setting the header by
# hand -- which is exactly how header-based auth is usually broken.
UI_BIND = os.getenv("UI_BIND", "127.0.0.1")
UI_AUTH = os.getenv("UI_AUTH", "tailscale").strip().lower()
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

# Empty means "any user on the tailnet". Set it to lock the UI to named logins.
UI_ALLOWED_LOGINS = frozenset(
    entry.strip().lower()
    for entry in os.getenv("UI_ALLOWED_LOGINS", "").split(",")
    if entry.strip()
)

IDENTITY_HEADER = "tailscale-user-login"
NAME_HEADER = "tailscale-user-name"


def _denied(reason: str) -> JSONResponse:
    """Say which check failed, without leaking who is allowed."""
    return JSONResponse(status_code=403, content={"detail": reason})


def _peer_problem(client_host, forwarded) -> str | None:
    """Why this peer cannot be trusted, or None if it can.

    Uvicorn rewrites the peer address from X-Forwarded-For by default, which
    turns a loopback proxy connection into a tailnet address and would refuse
    every legitimate request. That failure is safe but baffling, so name it.
    """
    if client_host in LOOPBACK:
        return None
    if forwarded:
        return (
            f"Refusing a request whose peer address ({client_host}) came from "
            "a forwarded header. Run this service with proxy headers disabled "
            "(python web_ui.py, or uvicorn --no-proxy-headers) so the real "
            "peer is visible."
        )
    return (
        "This service is only reachable through Tailscale. "
        f"Direct connections are refused (saw {client_host})."
    )


@app.middleware("http")
async def require_identity(request: Request, call_next):
    """Reject anything that did not arrive through the Tailscale proxy."""
    if UI_AUTH == "none":
        request.state.user = "anonymous"
        return await call_next(request)

    problem = _peer_problem(
        request.client.host if request.client else None,
        request.headers.get("x-forwarded-for"),
    )
    if problem:
        # Not via the local proxy, so any identity header on it is forged.
        return _denied(problem)

    login = (request.headers.get(IDENTITY_HEADER) or "").strip().lower()
    if not login:
        return _denied(
            "No Tailscale identity on this request. Reach the UI through its "
            "tailnet URL rather than localhost."
        )
    if UI_ALLOWED_LOGINS and login not in UI_ALLOWED_LOGINS:
        return _denied(f"{login} is not permitted to use this service.")

    request.state.user = login
    return await call_next(request)


@app.get("/api/whoami")
async def whoami(request: Request):
    """Who the proxy says is driving, for the header bar."""
    return {
        "login": getattr(request.state, "user", "anonymous"),
        "name": request.headers.get(NAME_HEADER) or "",
        "auth": UI_AUTH,
        "restricted": bool(UI_ALLOWED_LOGINS),
    }


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

        /* Vendor strip. Deliberately monograms rather than official logos:
           the real marks are trademarked and are not committed to this repo. */
        .vendors {
            margin-left: auto;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .vendor {
            display: inline-flex;
            align-items: center;
            gap: 5px;
            padding: 3px 9px 3px 6px;
            border: 1px solid #24405f;
            border-radius: 999px;
            background: #10182c;
            font-size: 11px;
            letter-spacing: 0.04em;
            color: #90a4ae;
            white-space: nowrap;
        }
        .vendor .dot {
            width: 9px; height: 9px; border-radius: 2px; flex: none;
        }
        .vendor[data-live="down"] { opacity: 0.45; }
        .vendor[data-live="down"] .dot { background: #546e7a !important; }
        header h1 {
            font-size: 1.3rem;
            color: #4fc3f7;
        }
        header .badge {            background: #0f3460;
            color: #81d4fa;
            padding: 0.2rem 0.6rem;
            border-radius: 12px;
            font-size: 0.75rem;
        }
        #workspace {
            flex: 1;
            display: flex;
            /* Without min-height the flex children refuse to shrink and the
               chat pane scrolls the whole page instead of itself. */
            min-height: 0;
        }
        #main {
            flex: 1;
            display: flex;
            flex-direction: column;
            min-width: 0;
            min-height: 0;
        }
        #sidebar {
            width: 250px;
            flex-shrink: 0;
            background: #16213e;
            border-right: 1px solid #0f3460;
            display: flex;
            flex-direction: column;
            min-height: 0;
        }
        #sidebar.hidden { display: none; }
        .sidebar-head {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0.8rem 1rem;
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: #7f8fa6;
            border-bottom: 1px solid #0f3460;
        }
        .sidebar-icon {
            background: none;
            border: 1px solid #0f3460;
            color: #9fb3c8;
            border-radius: 6px;
            width: 24px;
            height: 24px;
            font-size: 1rem;
            line-height: 1;
            cursor: pointer;
        }
        .sidebar-icon:hover { background: #0f3460; color: #eee; }
        .conv-list { overflow-y: auto; padding: 0.4rem; }
        .conv-item {
            display: flex;
            align-items: center;
            gap: 0.4rem;
            padding: 0.5rem 0.6rem;
            border-radius: 8px;
            cursor: pointer;
            font-size: 0.85rem;
            color: #cfd8e3;
        }
        .conv-item:hover { background: #1a2c50; }
        .conv-item.active { background: #0f3460; color: #fff; }
        .conv-title {
            flex: 1;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .conv-meta { font-size: 0.7rem; color: #7f8fa6; }
        .conv-delete {
            background: none;
            border: none;
            color: #7f8fa6;
            cursor: pointer;
            font-size: 0.9rem;
            padding: 0 0.2rem;
            visibility: hidden;
        }
        .conv-item:hover .conv-delete { visibility: visible; }
        .conv-delete:hover { color: #e57373; }
        .conv-empty { padding: 0.8rem; color: #7f8fa6; font-size: 0.8rem; }
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
        /* Tabular results. The pane renders text nodes, so a Markdown table
           used to arrive as literal pipes - unreadable past a few rows. */
        .table-block {
            margin: 0.6rem 0;
            overflow-x: auto;
        }
        .table-block table {
            border-collapse: collapse;
            font-size: 0.82rem;
            width: 100%;
        }
        .table-block th, .table-block td {
            border: 1px solid #2a4a7f;
            padding: 0.25rem 0.5rem;
            text-align: left;
            white-space: nowrap;
        }
        .table-block th {
            background: #0f3460;
            color: #81d4fa;
            position: sticky;
            top: 0;
        }
        .table-block tbody tr:nth-child(even) { background: rgba(255,255,255,0.03); }
        .table-tools {
            display: flex;
            align-items: center;
            gap: 0.6rem;
            margin-bottom: 0.3rem;
        }
        .table-count { font-size: 0.72rem; color: #9fb3c8; }
        .csv-button {
            background: #0f3460;
            color: #81d4fa;
            border: 1px solid #2a4a7f;
            border-radius: 4px;
            padding: 0.15rem 0.5rem;
            font-size: 0.72rem;
            cursor: pointer;
            font-family: inherit;
        }
        .csv-button:hover { background: #17518f; }
        /* Markdown blocks. Without these the model's "### Heading" and
           "**Observation:**" arrive as literal characters, which is fine on
           screen but unusable in an exported report. */
        .message h3, .message h4, .message h5 {
            margin: 0.9rem 0 0.4rem;
            color: #81d4fa;
            font-size: 0.95rem;
            line-height: 1.3;
        }
        .message h4 { font-size: 0.88rem; }
        .message h5 { font-size: 0.83rem; }
        .message p { margin: 0.45rem 0; }
        .message ul, .message ol { margin: 0.45rem 0; padding-left: 1.3rem; }
        .message li { margin: 0.15rem 0; }
        .message code {
            background: rgba(255,255,255,0.08);
            border-radius: 3px;
            padding: 0.05rem 0.3rem;
            font-size: 0.82em;
        }
        .message hr {
            border: 0;
            border-top: 1px solid #2a4a7f;
            margin: 0.9rem 0;
        }
        .message-tools {
            display: flex;
            align-items: center;
            gap: 0.6rem;
            margin-bottom: 0.4rem;
        }
        .pdf-button {
            background: #14402a;
            color: #7fe0a8;
            border: 1px solid #245c3d;
            border-radius: 4px;
            padding: 0.15rem 0.5rem;
            font-size: 0.72rem;
            cursor: pointer;
            font-family: inherit;
        }
        .pdf-button:hover { background: #1d5c3c; }
        .bar-button {
            background: #0f3460;
            color: #81d4fa;
            border: 1px solid #2a4a7f;
            border-radius: 4px;
            padding: 0.1rem 0.5rem;
            font-size: 0.72rem;
            cursor: pointer;
            font-family: inherit;
        }
        .bar-button:hover { background: #17518f; }
        .bar-button:disabled { opacity: 0.5; cursor: default; }
        .memory-state { color: #9fb3c8; font-style: italic; }
        .whoami { color: #7fd4a0; font-size: 0.75rem; margin-left: 0.4rem; }
        .panel {
            background: #16213e;
            border-bottom: 1px solid #0f3460;
            padding: 0.8rem 2rem 1rem;
            font-size: 0.8rem;
            max-height: 45vh;
            overflow-y: auto;
        }
        .panel h2 {
            font-size: 0.8rem;
            color: #81d4fa;
            margin: 0.4rem 0;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }
        .panel-section { margin-bottom: 0.9rem; }
        .panel-row {
            display: flex;
            align-items: center;
            gap: 0.4rem;
            margin-bottom: 0.4rem;
            flex-wrap: wrap;
        }
        .panel-row input[type="text"] {
            flex: 1;
            min-width: 18rem;
            background: #0f3460;
            border: 1px solid #2a4a7f;
            color: #e8e8e8;
            border-radius: 4px;
            padding: 0.3rem 0.5rem;
            font-family: inherit;
            font-size: 0.8rem;
        }
        .panel-row input[type="number"] {
            width: 3.2rem;
            background: #0f3460;
            border: 1px solid #2a4a7f;
            color: #e8e8e8;
            border-radius: 4px;
            padding: 0.25rem 0.3rem;
            font-family: inherit;
        }
        .panel-row select {
            background: #0f3460;
            border: 1px solid #2a4a7f;
            color: #e8e8e8;
            border-radius: 4px;
            padding: 0.25rem 0.4rem;
            font-family: inherit;
            font-size: 0.78rem;
        }
        .panel-row .hint { color: #9fb3c8; font-size: 0.72rem; }
        .panel-item {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.25rem 0;
            border-bottom: 1px solid rgba(255,255,255,0.06);
        }
        .panel-item span { flex: 1; }
        .panel-empty { color: #9fb3c8; font-style: italic; padding: 0.25rem 0; }
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

        .confirm-box {
            margin-top: 12px; padding: 12px 14px;
            border: 1px solid #ffb74d; border-left: 4px solid #ffb74d;
            border-radius: 6px; background: rgba(255,183,77,0.08);
        }
        .confirm-box.irreversible {
            border-color: #ef5350; border-left-color: #ef5350;
            background: rgba(239,83,80,0.10);
        }
        .confirm-title { font-weight: 600; color: #ffb74d; margin-bottom: 6px; }
        .confirm-box.irreversible .confirm-title { color: #ef5350; }
        .confirm-what { font-family: ui-monospace, Menlo, monospace; font-size: 13px; color: #e0e0e0; }
        .confirm-desc { font-size: 13px; color: #b0bec5; margin-top: 4px; }
        .confirm-warning { font-size: 13px; color: #ef9a9a; margin-top: 6px; }
        .confirm-actions { margin-top: 10px; display: flex; gap: 8px; align-items: center; }
        .confirm-actions button {
            padding: 6px 14px; border-radius: 5px; border: none;
            font-size: 13px; cursor: pointer;
        }
        .confirm-actions button:disabled { opacity: 0.5; cursor: default; }
        .confirm-yes { background: #ef5350; color: #fff; }
        .confirm-no { background: #37474f; color: #cfd8dc; }
        .confirm-status { font-size: 13px; color: #b0bec5; }
        .confirm-status.good { color: #81c784; }
        .confirm-status.bad { color: #ef5350; }
        .confirm-status.muted { color: #78909c; }
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
        .pin-btn { margin-left: 10px; }
        .pin-btn.is-pinned { border-color: #ffb74d; color: #ffb74d; }
        .pin-btn.is-busy { opacity: 0.5; cursor: progress; }
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
        <span class="badge">vCenter · VCF Ops · Networks · Logs · Veeam</span>
        <span class="badge">100% On-Premises</span>
        <span class="vendors">
            <span class="vendor" id="v-broadcom"><span class="dot" style="background:#cc092f"></span>Broadcom</span>
            <span class="vendor" id="v-dell"><span class="dot" style="background:#0076ce"></span>Dell</span>
            <span class="vendor" id="v-intel"><span class="dot" style="background:#0068b5"></span>Intel</span>
            <span class="vendor" id="v-veeam"><span class="dot" style="background:#00b336"></span>Veeam</span>
        </span>
    </header>
    <div class="info-bar">
        <span>LLM: {{LLM_BACKEND}}</span>
        <span>APIs: {{API_BACKEND}}</span>
        <span id="timer"></span>
        <span id="memory-state" class="memory-state" title="Follow-up questions see earlier turns in this conversation">new conversation</span>
        <button id="new-chat-btn" class="bar-button" title="Forget the current thread and start fresh">New chat</button>
        <button id="panel-btn" class="bar-button" title="Scheduled questions and stored reports">Schedules &amp; reports</button>
        <span id="gpu-strip" class="gpu-strip"></span>
        <span id="whoami" class="whoami" title="Identity supplied by Tailscale"></span>
        <button id="pin-btn" class="bar-button pin-btn" hidden></button>
    </div>
    <div id="panel" class="panel" hidden>
        <div class="panel-section">
            <h2>Schedule a question</h2>
            <div class="panel-row">
                <input type="text" id="sched-question" placeholder="e.g. Which VMs have no recent restore point?">
            </div>
            <div class="panel-row">
                <select id="sched-kind">
                    <option value="daily">Daily</option>
                    <option value="hourly">Hourly</option>
                    <option value="weekly">Weekly</option>
                </select>
                <select id="sched-weekday" hidden></select>
                <span id="sched-at">at</span>
                <input type="number" id="sched-hour" min="0" max="23" value="7" title="Hour, UTC">
                <span>:</span>
                <input type="number" id="sched-minute" min="0" max="59" value="0" title="Minute">
                <span class="hint">UTC</span>
                <button id="sched-add" class="bar-button">Add schedule</button>
            </div>
            <div id="sched-list" class="panel-list"></div>
        </div>
        <div class="panel-section">
            <h2>Stored reports</h2>
            <div id="runs-list" class="panel-list"></div>
        </div>
    </div>
    <div id="workspace">
        <aside id="sidebar">
            <div class="sidebar-head">
                <span>Conversations</span>
                <button id="sidebar-new" class="sidebar-icon" title="Start a new conversation">+</button>
            </div>
            <div id="conv-list" class="conv-list"></div>
        </aside>
        <div id="main">
            <div id="chat-container">
                <div class="message assistant">{{WELCOME}}</div>
            </div>
            <div id="input-area">
                <select id="model-select"><option value="">Loading models...</option></select>
                <select id="scope-select" title="Which systems the assistant may query"></select>
                <input type="text" id="user-input" placeholder="Ask about your VMware infrastructure..." autofocus>
                <button id="send-btn" onclick="sendMessage()">Send</button>
            </div>
        </div>
    </div>

    <script>
        const chatContainer = document.getElementById('chat-container');
        const userInput = document.getElementById('user-input');
        const sendBtn = document.getElementById('send-btn');
        const modelSelect = document.getElementById('model-select');
        const scopeSelect = document.getElementById('scope-select');
        const timerEl = document.getElementById('timer');
        const memoryState = document.getElementById('memory-state');
        const panel = document.getElementById('panel');
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

        // The question that produced the current answer, carried into the
        // exported report so a printed page says what was asked.
        let lastQuestion = '';

        // --- conversation memory ---------------------------------------------
        //
        // History lives in the orchestrator's database, but the id that reaches
        // it was previously a bare JS variable, so any reload started a new
        // thread while the page still looked like the same session. The comment
        // here used to claim a reload picked the thread back up; it did not.
        // The id now survives in localStorage and the sidebar lists what the
        // server actually holds, so the two can no longer disagree.

        const CONV_KEY = 'oi.conversationId';
        let conversationId = null;

        function setConversationId(id) {
            conversationId = id || null;
            try {
                if (conversationId) {
                    localStorage.setItem(CONV_KEY, conversationId);
                } else {
                    localStorage.removeItem(CONV_KEY);
                }
            } catch (e) {
                // Private browsing denies localStorage. Memory then lasts only
                // as long as the tab, which is the old behaviour, not a crash.
            }
        }

        function storedConversationId() {
            try {
                return localStorage.getItem(CONV_KEY);
            } catch (e) {
                return null;
            }
        }

        function setMemoryState(turns) {
            if (!conversationId) {
                memoryState.textContent = 'new conversation';
                memoryState.title = 'Nothing remembered yet';
                return;
            }
            memoryState.textContent = turns
                ? 'remembering ' + turns + (turns === 1 ? ' turn' : ' turns')
                : 'conversation started';
            memoryState.title = 'Follow-up questions see earlier turns in this '
                + 'conversation (id ' + conversationId + ')';
        }

        function newConversation() {
            setConversationId(null);
            setMemoryState(0);
            addMessage('Started a new conversation. Earlier questions are no '
                       + 'longer used as context.', 'thinking');
            renderConversations();
        }

        // --- conversation sidebar --------------------------------------------

        const convList = document.getElementById('conv-list');
        let conversations = [];

        function relativeTime(iso) {
            const then = Date.parse(iso && iso.endsWith('Z') ? iso : iso + 'Z');
            if (isNaN(then)) { return ''; }
            const mins = Math.round((Date.now() - then) / 60000);
            if (mins < 1) { return 'now'; }
            if (mins < 60) { return mins + 'm'; }
            if (mins < 1440) { return Math.round(mins / 60) + 'h'; }
            return Math.round(mins / 1440) + 'd';
        }

        function renderConversations() {
            convList.textContent = '';
            if (!conversations.length) {
                const empty = document.createElement('div');
                empty.className = 'conv-empty';
                empty.textContent = 'No earlier conversations.';
                convList.appendChild(empty);
                return;
            }
            conversations.forEach(conv => {
                const item = document.createElement('div');
                item.className = 'conv-item' + (conv.id === conversationId ? ' active' : '');

                const title = document.createElement('span');
                title.className = 'conv-title';
                // An untitled thread is one where the first question never
                // landed; show the id rather than an empty clickable strip.
                title.textContent = conv.title || conv.id;
                title.title = conv.title || conv.id;
                item.appendChild(title);

                const meta = document.createElement('span');
                meta.className = 'conv-meta';
                meta.textContent = relativeTime(conv.updated_at);
                item.appendChild(meta);

                const del = document.createElement('button');
                del.className = 'conv-delete';
                del.textContent = '×';
                del.title = 'Delete this conversation';
                del.addEventListener('click', event => {
                    event.stopPropagation();
                    deleteConversation(conv.id);
                });
                item.appendChild(del);

                item.addEventListener('click', () => openConversation(conv.id));
                convList.appendChild(item);
            });
        }

        async function loadConversations() {
            try {
                const response = await fetch('/api/conversations?limit=50');
                if (!response.ok) { return; }
                const data = await response.json();
                conversations = data.conversations || [];
            } catch (e) {
                // The list is navigation, not the product. Losing it must not
                // stop someone asking a question.
                conversations = [];
            }
            renderConversations();
        }

        async function openConversation(id) {
            let data;
            try {
                const response = await fetch('/api/conversations/' + encodeURIComponent(id));
                if (response.status === 404) {
                    // Deleted server-side. Drop it rather than leaving a dead
                    // row that silently sends questions into nothing.
                    if (id === conversationId) { setConversationId(null); }
                    await loadConversations();
                    return;
                }
                if (!response.ok) { return; }
                data = await response.json();
            } catch (e) {
                addMessage('Could not load that conversation: ' + e.message, 'error');
                return;
            }
            setConversationId(id);
            chatContainer.textContent = '';
            const messages = data.messages || [];
            messages.forEach(m => {
                if (m.role === 'user') { lastQuestion = m.content; }
                addMessage(m.content, m.role === 'user' ? 'user' : 'assistant');
            });
            setMemoryState(Math.floor(messages.length / 2));
            renderConversations();
        }

        async function deleteConversation(id) {
            try {
                await fetch('/api/conversations/' + encodeURIComponent(id),
                            { method: 'DELETE' });
            } catch (e) {
                addMessage('Could not delete that conversation: ' + e.message, 'error');
                return;
            }
            if (id === conversationId) {
                setConversationId(null);
                setMemoryState(0);
            }
            await loadConversations();
        }

        async function restoreConversation() {
            const saved = storedConversationId();
            await loadConversations();
            if (saved) { await openConversation(saved); }
        }

        document.getElementById('new-chat-btn')
            .addEventListener('click', newConversation);
        document.getElementById('sidebar-new')
            .addEventListener('click', newConversation);

        // --- schedules and stored reports -------------------------------------

        const kindSelect = document.getElementById('sched-kind');
        const weekdaySelect = document.getElementById('sched-weekday');
        const DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday',
                           'Friday', 'Saturday', 'Sunday'];

        DAY_NAMES.forEach((name, index) => {
            const option = document.createElement('option');
            option.value = String(index);
            option.textContent = name;
            weekdaySelect.appendChild(option);
        });

        function syncScheduleControls() {
            const kind = kindSelect.value;
            weekdaySelect.hidden = kind !== 'weekly';
            document.getElementById('sched-hour').hidden = kind === 'hourly';
            document.getElementById('sched-at').hidden = kind === 'hourly';
        }
        kindSelect.addEventListener('change', syncScheduleControls);
        syncScheduleControls();

        document.getElementById('panel-btn').addEventListener('click', () => {
            panel.hidden = !panel.hidden;
            if (!panel.hidden) { refreshSchedules(); refreshRuns(); }
        });

        function row(text, className) {
            const el = document.createElement('div');
            el.className = className || 'panel-item';
            el.textContent = text;
            return el;
        }

        async function refreshSchedules() {
            const list = document.getElementById('sched-list');
            list.textContent = '';
            try {
                const data = await (await fetch('/api/schedules')).json();
                if (!data.schedules.length) {
                    list.appendChild(row('No schedules yet.', 'panel-empty'));
                    return;
                }
                data.schedules.forEach(s => {
                    const item = document.createElement('div');
                    item.className = 'panel-item';
                    const text = document.createElement('span');
                    text.textContent = s.question + '  —  ' + s.description
                        + '  —  next ' + s.next_run.replace('T', ' ');
                    item.appendChild(text);

                    const runNow = document.createElement('button');
                    runNow.className = 'bar-button';
                    runNow.textContent = 'Run now';
                    runNow.addEventListener('click', () => runScheduleNow(s.id, runNow));
                    item.appendChild(runNow);

                    const remove = document.createElement('button');
                    remove.className = 'bar-button';
                    remove.textContent = 'Delete';
                    remove.addEventListener('click', async () => {
                        await fetch('/api/schedules/' + s.id, {method: 'DELETE'});
                        refreshSchedules();
                    });
                    item.appendChild(remove);
                    list.appendChild(item);
                });
            } catch (e) {
                list.appendChild(row('Could not load schedules: ' + e.message,
                                     'panel-empty'));
            }
        }

        async function runScheduleNow(id, button) {
            button.disabled = true;
            button.textContent = 'Running...';
            try {
                const run = await (await fetch('/api/schedules/' + id + '/run',
                                               {method: 'POST'})).json();
                refreshRuns();
                showRun(run);
            } catch (e) {
                addMessage('Scheduled run failed: ' + e.message, 'error');
            }
            button.disabled = false;
            button.textContent = 'Run now';
        }

        async function refreshRuns() {
            const list = document.getElementById('runs-list');
            list.textContent = '';
            try {
                const data = await (await fetch('/api/runs?limit=25')).json();
                if (!data.runs.length) {
                    list.appendChild(row('No reports stored yet.', 'panel-empty'));
                    return;
                }
                data.runs.forEach(r => {
                    const item = document.createElement('div');
                    item.className = 'panel-item';
                    const text = document.createElement('span');
                    text.textContent = r.started_at.replace('T', ' ') + '  —  '
                        + r.question + (r.status === 'ok' ? '' : '  [' + r.status + ']');
                    item.appendChild(text);
                    const open = document.createElement('button');
                    open.className = 'bar-button';
                    open.textContent = 'Open';
                    open.addEventListener('click', async () => {
                        const full = await (await fetch('/api/runs/' + r.id)).json();
                        showRun(full);
                    });
                    item.appendChild(open);
                    list.appendChild(item);
                });
            } catch (e) {
                list.appendChild(row('Could not load reports: ' + e.message,
                                     'panel-empty'));
            }
        }

        // A stored report is rendered through exactly the same path as a live
        // answer, so it gets the same tables, CSV buttons and PDF export.
        function showRun(run) {
            panel.hidden = true;
            addMessage(run.question, 'user');
            if (run.status === 'ok') {
                lastQuestion = run.question;
                addMessage(run.answer, 'assistant',
                           (run.model || 'scheduled') + ' · '
                           + run.started_at.replace('T', ' '), run);
            } else {
                addMessage('That scheduled run failed: ' + (run.error || 'unknown'),
                           'error');
            }
        }

        document.getElementById('sched-add').addEventListener('click', async () => {
            const question = document.getElementById('sched-question').value.trim();
            if (!question) {
                addMessage('A schedule needs a question.', 'error');
                return;
            }
            const body = {
                question: question,
                kind: kindSelect.value,
                hour: parseInt(document.getElementById('sched-hour').value, 10) || 0,
                minute: parseInt(document.getElementById('sched-minute').value, 10) || 0,
                model: modelSelect.value || null,
                scope: scopeSelect.value || 'all'
            };
            if (kindSelect.value === 'weekly') {
                body.weekday = parseInt(weekdaySelect.value, 10);
            }
            const response = await fetch('/api/schedules', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            });
            if (!response.ok) {
                const err = await response.json();
                addMessage('Schedule rejected: ' + formatError(err.detail, response.status), 'error');
                return;
            }
            const created = await response.json();
            document.getElementById('sched-question').value = '';
            addMessage('Scheduled: ' + question + ' (' + created.description
                       + ', next run ' + created.next_run.replace('T', ' ') + ')',
                       'thinking');
            refreshSchedules();
        });

        // FastAPI reports validation failures as a list of objects. String
        // concatenation turns those into "[object Object]", which is how a
        // precise, actionable server error became an unreadable one on screen.
        function formatError(detail, status) {
            if (typeof detail === 'string' && detail) { return detail; }
            if (Array.isArray(detail)) {
                const lines = detail.map(function (item) {
                    if (typeof item === 'string') { return item; }
                    const where = Array.isArray(item.loc) ? item.loc.join('.') : '';
                    const msg = item.msg || JSON.stringify(item);
                    return where ? where + ': ' + msg : msg;
                });
                if (lines.length) { return lines.join('; '); }
            }
            if (detail && typeof detail === 'object') {
                try { return JSON.stringify(detail); } catch (e) { /* fall through */ }
            }
            return status ? 'Request failed (HTTP ' + status + ')' : 'Unknown error';
        }

        async function sendMessage() {
            const message = userInput.value.trim();
            if (!message) return;

            const model = modelSelect.value;

            lastQuestion = message;
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
                    body: JSON.stringify({ message, model, scope: scopeSelect.value || 'all',
                                           conversation_id: conversationId }),
                });

                stopTimer();
                chatContainer.removeChild(thinkingEl);

                if (response.ok) {
                    const data = await response.json();
                    setConversationId(data.conversation_id || conversationId);
                    setMemoryState(data.history_turns || 0);
                    addMessage(data.answer, 'assistant', data.model, data);
                    refreshTelemetry();
                    // A new thread needs a row in the sidebar; an existing one
                    // needs its position refreshed.
                    loadConversations();
                } else {
                    let err = {};
                    try { err = await response.json(); } catch (e) { /* not JSON */ }
                    addMessage('Error: ' + formatError(err.detail, response.status), 'error');
                }
            } catch (e) {
                stopTimer();
                chatContainer.removeChild(thinkingEl);
                addMessage('Connection error: ' + e.message, 'error');
            }

            sendBtn.disabled = false;
            userInput.focus();
        }

        // --- Markdown table rendering ----------------------------------------
        //
        // The pane builds messages from text nodes, so a Markdown table used to
        // arrive as literal "|---|" pipes. The server was flattening tables to
        // compensate; now they are rendered properly and each one gets a CSV
        // download, because the answer to "which VMs" is usually the start of a
        // piece of work rather than the end of one.
        //
        // Every cell goes in via textContent. Nothing here uses innerHTML: this
        // text comes from a model and must never be able to inject markup.

        const TABLE_ROW = /^\\s*\\|(.+)\\|\\s*$/;
        const TABLE_SEP = /^\\s*\\|[\\s:|-]+\\|\\s*$/;
        const HEADING = /^(#{1,6})\\s+(.*)$/;
        const HRULE = /^\\s*(-{3,}|\\*{3,}|_{3,})\\s*$/;
        const BULLET = /^\\s*[-*+]\\s+(.*)$/;
        const NUMBERED = /^\\s*\\d+[.)]\\s+(.*)$/;
        // Escape, bold, code, italic - in that order, so ** is not eaten by *.
        const INLINE = /(\\\\.)|(\\*\\*[^*]+\\*\\*)|(`[^`]+`)|(\\*[^*\\n]+\\*)/g;

        function inlineParts(text) {
            const t = (text === null || text === undefined) ? '' : String(text);
            const parts = [];
            let last = 0;
            let m;
            INLINE.lastIndex = 0;
            while ((m = INLINE.exec(t)) !== null) {
                if (m.index > last) {
                    parts.push({kind: 'text', value: t.slice(last, m.index)});
                }
                if (m[1]) parts.push({kind: 'text', value: m[1].slice(1)});
                else if (m[2]) parts.push({kind: 'strong', value: m[2].slice(2, -2)});
                else if (m[3]) parts.push({kind: 'code', value: m[3].slice(1, -1)});
                else parts.push({kind: 'em', value: m[4].slice(1, -1)});
                last = m.index + m[0].length;
            }
            if (last < t.length) parts.push({kind: 'text', value: t.slice(last)});
            return parts;
        }

        function renderInline(el, text) {
            inlineParts(text).forEach(part => {
                if (part.kind === 'text') {
                    el.appendChild(document.createTextNode(part.value));
                } else {
                    const tag = part.kind === 'strong' ? 'strong'
                              : part.kind === 'code' ? 'code' : 'em';
                    const node = document.createElement(tag);
                    node.textContent = part.value;
                    el.appendChild(node);
                }
            });
        }

        // A CSV of "**Low** (degradation)" should read "Low (degradation)".
        function stripInline(text) {
            return inlineParts(text).map(p => p.value).join('');
        }

        function splitCells(line) {
            return line.replace(/^\\s*\\|/, '').replace(/\\|\\s*$/, '')
                       .split('|').map(c => c.trim());
        }

        function csvEscape(value) {
            const text = (value === null || value === undefined) ? '' : String(value);
            return /[",\\n\\r]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
        }

        function toCsv(header, rows) {
            return [header].concat(rows)
                .map(r => r.map(c => csvEscape(stripInline(c))).join(','))
                .join('\\r\\n');
        }

        function downloadCsv(header, rows, index) {
            const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
            // BOM so Excel opens UTF-8 names correctly.
            const blob = new Blob(['\\uFEFF' + toCsv(header, rows)],
                                  {type: 'text/csv;charset=utf-8;'});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'ops-table-' + index + '-' + stamp + '.csv';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        }

        let tableSeq = 0;

        function buildTable(header, rows) {
            const block = document.createElement('div');
            block.className = 'table-block';

            const tools = document.createElement('div');
            tools.className = 'table-tools';
            const count = document.createElement('span');
            count.className = 'table-count';
            count.textContent = rows.length + (rows.length === 1 ? ' row' : ' rows');
            const button = document.createElement('button');
            button.className = 'csv-button';
            button.textContent = 'Download CSV';
            const index = ++tableSeq;
            button.addEventListener('click', () => downloadCsv(header, rows, index));
            tools.appendChild(button);
            tools.appendChild(count);
            block.appendChild(tools);

            const table = document.createElement('table');
            const thead = document.createElement('thead');
            const headRow = document.createElement('tr');
            header.forEach(cell => {
                const th = document.createElement('th');
                renderInline(th, cell);
                headRow.appendChild(th);
            });
            thead.appendChild(headRow);
            table.appendChild(thead);

            const tbody = document.createElement('tbody');
            rows.forEach(row => {
                const tr = document.createElement('tr');
                for (let i = 0; i < header.length; i++) {
                    const td = document.createElement('td');
                    renderInline(td, row[i] === undefined ? '' : row[i]);
                    tr.appendChild(td);
                }
                tbody.appendChild(tr);
            });
            table.appendChild(tbody);
            block.appendChild(table);
            return block;
        }

        function renderBlocks(container, lines) {
            let i = 0;
            while (i < lines.length) {
                if (!lines[i].trim()) { i++; continue; }

                const heading = HEADING.exec(lines[i]);
                if (heading) {
                    // Clamp to h3..h5: the page already owns h1/h2.
                    const level = Math.min(Math.max(heading[1].length + 2, 3), 5);
                    const el = document.createElement('h' + level);
                    renderInline(el, heading[2]);
                    container.appendChild(el);
                    i++;
                    continue;
                }

                if (HRULE.test(lines[i])) {
                    container.appendChild(document.createElement('hr'));
                    i++;
                    continue;
                }

                if (BULLET.test(lines[i]) || NUMBERED.test(lines[i])) {
                    const ordered = !BULLET.test(lines[i]);
                    const list = document.createElement(ordered ? 'ol' : 'ul');
                    while (i < lines.length) {
                        const item = BULLET.exec(lines[i]) || NUMBERED.exec(lines[i]);
                        if (!item) break;
                        const li = document.createElement('li');
                        renderInline(li, item[1]);
                        list.appendChild(li);
                        i++;
                    }
                    container.appendChild(list);
                    continue;
                }

                const paragraph = [];
                while (i < lines.length && lines[i].trim() &&
                       !HEADING.test(lines[i]) && !HRULE.test(lines[i]) &&
                       !BULLET.test(lines[i]) && !NUMBERED.test(lines[i])) {
                    paragraph.push(lines[i]);
                    i++;
                }
                const p = document.createElement('p');
                renderInline(p, paragraph.join('\\n'));
                container.appendChild(p);
            }
        }

        function renderBody(container, text) {
            const lines = String(text).split('\\n');
            let buffer = [];
            let i = 0;

            function flushText() {
                if (buffer.length) {
                    renderBlocks(container, buffer);
                    buffer = [];
                }
            }

            while (i < lines.length) {
                const isTableStart = TABLE_ROW.test(lines[i]) &&
                                     i + 1 < lines.length &&
                                     TABLE_SEP.test(lines[i + 1]);
                if (isTableStart) {
                    const header = splitCells(lines[i]);
                    const rows = [];
                    let j = i + 2;
                    while (j < lines.length && TABLE_ROW.test(lines[j]) &&
                           !TABLE_SEP.test(lines[j])) {
                        rows.push(splitCells(lines[j]));
                        j++;
                    }
                    // A header and separator with no rows is not a table worth
                    // framing; fall through and leave it as text.
                    if (rows.length) {
                        flushText();
                        container.appendChild(buildTable(header, rows));
                        i = j;
                        continue;
                    }
                }
                buffer.push(lines[i]);
                i++;
            }
            flushText();
        }

        // --- PDF export -------------------------------------------------------
        //
        // No PDF library, on purpose: this runs on an estate that should not
        // need to fetch a vendored megabyte from a CDN to print a report. The
        // browser already has a PDF writer behind Ctrl-P, so the export opens a
        // clean print view and calls it. The cost is that the user picks
        // "Save as PDF" in the dialog rather than getting a direct download.

        const PRINT_CSS = [
            'body { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;',
            '       color: #111; margin: 24px; }',
            'h1 { font-size: 18px; margin: 0 0 2px; }',
            '.meta { color: #555; font-size: 11px; margin-bottom: 14px; }',
            '.question { font-size: 13px; font-weight: 600; margin: 0 0 16px;',
            '            padding: 8px 10px; background: #f2f4f7;',
            '            border-left: 3px solid #7a8ba0; }',
            'table { border-collapse: collapse; font-size: 10px; width: 100%;',
            '        margin: 4px 0 14px; }',
            'th, td { border: 1px solid #999; padding: 3px 6px; text-align: left; }',
            'th { background: #eceff3; }',
            'h3, h4, h5 { margin: 14px 0 4px; font-size: 13px; }',
            'p { margin: 6px 0; font-size: 12px; }',
            'ul, ol { margin: 6px 0; padding-left: 18px; font-size: 12px; }',
            'li { margin: 2px 0; }',
            'code { background: #eee; padding: 0 3px; }',
            'hr { border: 0; border-top: 1px solid #ccc; margin: 12px 0; }',
            '.message-tools, .table-tools, .model-tag, .usage-bar, .confirm-box',
            '  { display: none !important; }',
            // Repeat headers on every page and avoid splitting a row.
            '@media print { @page { margin: 14mm; }',
            '  thead { display: table-header-group; }',
            '  tr { break-inside: avoid; page-break-inside: avoid; } }'
        ].join('\\n');

        function exportPdf(messageDiv, model, question) {
            const win = window.open('', '_blank');
            if (!win) {
                addMessage('Could not open the report window. Allow pop-ups for '
                           + 'this site, then try again.', 'error');
                return;
            }
            const doc = win.document;
            const stamp = new Date().toISOString().slice(0, 19).replace('T', ' ');

            doc.title = 'Datacenter report ' + stamp;
            const style = doc.createElement('style');
            style.textContent = PRINT_CSS;
            doc.head.appendChild(style);

            const title = doc.createElement('h1');
            title.textContent = 'Operational status report';
            doc.body.appendChild(title);

            const meta = doc.createElement('div');
            meta.className = 'meta';
            meta.textContent = 'Generated ' + stamp + ' UTC'
                + (model ? '  |  model: ' + model : '')
                + '  |  source: live datacenter APIs, read-only';
            doc.body.appendChild(meta);

            if (question) {
                const q = doc.createElement('div');
                q.className = 'question';
                q.textContent = question;
                doc.body.appendChild(q);
            }

            doc.body.appendChild(doc.importNode(messageDiv, true));
            win.focus();
            // Give the imported nodes a tick to lay out before the dialog opens.
            win.setTimeout(function () { win.print(); }, 300);
        }

        function addMessage(text, type, model, data) {
            const div = document.createElement('div');
            div.className = 'message ' + type;
            if (model && type === 'assistant') {
                const tools = document.createElement('div');
                tools.className = 'message-tools';
                const tag = document.createElement('div');
                tag.className = 'model-tag';
                tag.textContent = model;
                const question = lastQuestion;
                const pdfButton = document.createElement('button');
                pdfButton.className = 'pdf-button';
                pdfButton.textContent = 'Export PDF';
                pdfButton.title = 'Opens a print view; choose "Save as PDF"';
                pdfButton.addEventListener('click',
                    () => exportPdf(div, model, question));
                tools.appendChild(pdfButton);
                tools.appendChild(tag);
                div.appendChild(tools);
                renderBody(div, '\\n' + text);
                const usage = buildUsageBar(data);
                if (usage) div.appendChild(usage);
                (data && data.pending_actions || []).forEach(a => {
                    div.appendChild(buildConfirmBox(a));
                });
            } else {
                div.textContent = text;
            }
            chatContainer.appendChild(div);
            chatContainer.scrollTop = chatContainer.scrollHeight;
            return div;
        }

        // --- pending write confirmation --------------------------------------
        //
        // A proposed change is the only thing in this UI that can alter
        // production, so it is deliberately not a link in a paragraph: the
        // affected object and the irreversibility are stated on the button.

        function buildConfirmBox(action) {
            const box = document.createElement('div');
            box.className = 'confirm-box' + (action.irreversible ? ' irreversible' : '');

            const title = document.createElement('div');
            title.className = 'confirm-title';
            title.textContent = (action.irreversible ? '\\u26a0 Irreversible \\u2014 ' : '')
                + 'Confirmation required';
            box.appendChild(title);

            const what = document.createElement('div');
            const args = Object.entries(action.arguments || {})
                .map(([k, v]) => k + '=' + v).join(', ');
            what.textContent = action.tool + (args ? ' (' + args + ')' : '');
            what.className = 'confirm-what';
            box.appendChild(what);

            if (action.action) {
                const desc = document.createElement('div');
                desc.className = 'confirm-desc';
                desc.textContent = action.action;
                box.appendChild(desc);
            }
            if (action.warning) {
                const warn = document.createElement('div');
                warn.className = 'confirm-warning';
                warn.textContent = action.warning;
                box.appendChild(warn);
            }

            const row = document.createElement('div');
            row.className = 'confirm-actions';
            const status = document.createElement('span');
            status.className = 'confirm-status';

            const run = document.createElement('button');
            run.textContent = action.irreversible ? 'Confirm anyway' : 'Confirm';
            run.className = 'confirm-yes';
            const stop = document.createElement('button');
            stop.textContent = 'Cancel';
            stop.className = 'confirm-no';

            function finish(text, cls) {
                run.remove(); stop.remove();
                status.textContent = text;
                status.className = 'confirm-status ' + cls;
            }

            run.onclick = async () => {
                run.disabled = stop.disabled = true;
                status.textContent = 'Executing\\u2026';
                try {
                    const r = await fetch('/api/confirm', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({token: action.confirmation_token}),
                    });
                    const d = await r.json();
                    if (!r.ok) { finish('Failed: ' + formatError(d.detail, r.status), 'bad'); return; }
                    // Report the verified state, not merely that the call returned.
                    const after = d.state_after && (d.state_after.power_state
                        || d.state_after.maintenance_mode);
                    finish(d.status === 'EXECUTED'
                        ? 'Executed' + (after ? ' \\u2014 now ' + after : '')
                        : 'Failed: ' + JSON.stringify(d.result),
                        d.status === 'EXECUTED' ? 'good' : 'bad');
                } catch (e) {
                    finish('Failed: ' + e.message, 'bad');
                }
            };

            stop.onclick = async () => {
                run.disabled = stop.disabled = true;
                try {
                    await fetch('/api/cancel', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({token: action.confirmation_token}),
                    });
                } catch (e) { /* cancelling is best effort */ }
                finish('Cancelled \\u2014 nothing was changed', 'muted');
            };

            row.appendChild(run);
            row.appendChild(stop);
            row.appendChild(status);
            box.appendChild(row);
            return box;
        }

        // --- vendor strip -----------------------------------------------------
        //
        // The chips are wired to real health rather than being decoration: a
        // greyed vendor means that system is not answering. Intel has no probe
        // of its own and stays lit.

        async function updateVendors() {
            try {
                const r = await fetch('/api/health');
                if (!r.ok) return;
                const h = await r.json();
                const apis = {};
                (h.apis || []).forEach(a => { apis[a.name] = a.reachable; });

                const vmware = ['vcenter', 'vcf_ops', 'vcf_networks']
                    .filter(n => n in apis);
                const vmwareUp = vmware.length === 0 || vmware.some(n => apis[n]);
                set('v-broadcom', vmwareUp);
                if ('backup' in apis) set('v-veeam', apis.backup);
                // The GB10 is the Dell box doing inference.
                set('v-dell', !!(h.inference && h.inference.reachable));
            } catch (e) { /* the strip is cosmetic; never break the page */ }
        }

        function set(id, up) {
            const el = document.getElementById(id);
            if (el) el.setAttribute('data-live', up ? 'up' : 'down');
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

        // --- resident model: pin / unpin --------------------------------------
        // The inference host has one pool of unified memory and no MIG, so the
        // pinned assistant model blocks anything else that wants the GPU — an
        // NVIDIA NIM, say. This releases it without an SSH session to the box.

        const pinBtn = document.getElementById('pin-btn');
        let pinBusy = false;

        async function refreshPin() {
            if (pinBusy) return;
            try {
                const r = await fetch('/api/memory');
                if (!r.ok) throw new Error('http ' + r.status);
                const d = await r.json();
                pinBtn.hidden = false;
                pinBtn.classList.toggle('is-pinned', d.pinned);
                pinBtn.textContent = d.pinned ? 'Unpin' : 'Pin';
                pinBtn.title = d.pinned
                    ? d.assistant_model + ' is held in memory (' + d.total_gb +
                      ' GB). Unpin to free it for other GPU work.'
                    : d.assistant_model + ' is not resident. Pin to preload it ' +
                      'and avoid a slow first question.';
                pinBtn.dataset.pinned = d.pinned ? '1' : '';
            } catch (e) {
                pinBtn.hidden = true;   // optional feature; fail quietly
            }
        }

        pinBtn.addEventListener('click', async () => {
            const wasPinned = pinBtn.dataset.pinned === '1';
            pinBusy = true;
            pinBtn.classList.add('is-busy');
            pinBtn.disabled = true;
            // Pinning reloads the whole model from disk, so this is not instant.
            pinBtn.textContent = wasPinned ? 'Freeing\u2026' : 'Loading\u2026';
            try {
                const r = await fetch(wasPinned ? '/api/memory/unpin' : '/api/memory/pin',
                                      { method: 'POST' });
                const d = await r.json();
                if (!r.ok) throw new Error(d.detail || 'failed');
                pinBtn.title = (wasPinned ? 'Freed' : 'Pinned') + ' in ' + d.seconds + 's';
            } catch (e) {
                pinBtn.title = 'Failed: ' + e.message;
            }
            pinBusy = false;
            pinBtn.classList.remove('is-busy');
            pinBtn.disabled = false;
            refreshPin();
            refreshTelemetry();
        });

        refreshPin();
        setInterval(refreshPin, 15000);

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
        async function loadWhoami() {
            // Identity comes from the proxy, never from the page, so this is a
            // display of who the server decided you are - not a claim by the
            // browser about who it would like to be.
            const el = document.getElementById('whoami');
            try {
                const res = await fetch('/api/whoami');
                if (!res.ok) return;
                const who = await res.json();
                if (who.auth === 'none') {
                    el.textContent = 'unauthenticated';
                    el.style.color = '#e0894a';
                    el.title = 'UI_AUTH=none - anyone who can reach this port can use it';
                    return;
                }
                el.textContent = who.name || who.login;
                el.title = 'Signed in as ' + who.login + ' (verified by Tailscale)';
            } catch (e) {
                /* the header bar must never be the reason the page fails */
            }
        }
        loadOptions();
        loadWhoami();
        restoreConversation();
        updateVendors();
        setInterval(updateVendors, 30000);

        setInterval(refreshTelemetry, 5000);
    </script>
</body>
</html>"""


def build_welcome(cfg: dict) -> str:
    """Compose the greeting from the orchestrator's actual configuration.

    The previous greeting was written by hand and named three systems and an
    8B/70B model choice. Logs and Veeam were added, the models changed, and
    the text kept confidently describing a setup that no longer existed —
    the first thing anyone reads, and it was wrong.
    """
    systems = [s.get("label") for s in cfg.get("systems") or [] if s.get("label")]
    if systems:
        named = ", ".join(systems[:-1]) + " and " + systems[-1] if len(systems) > 1 else systems[0]
        opening = (f"Hello. I'm your on-premises VMware assistant. I can query "
                   f"{named} — all running locally, with nothing leaving your network.")
    else:
        # Orchestrator unreachable: say so rather than inventing a system list.
        opening = ("Hello. I'm your on-premises VMware assistant. I could not reach "
                   "the orchestrator, so I cannot say which systems are available "
                   "yet — try a question and the error will tell you more.")

    lines = [opening, "", "Try asking me:"]
    lines += ['\u2022 "Is anything wrong in the estate right now?"',
              '\u2022 "Any errors on the ESXi hosts in the last 24 hours?"',
              '\u2022 "Which VMs have no recent restore point?"',
              '\u2022 "Triage vm <name>" for everything known about one VM']

    tools = cfg.get("tool_count")
    if tools:
        lines += ["", f"{tools} tools across {len(systems) or 'several'} systems."]
    if cfg.get("write_tools_enabled"):
        lines.append("Actions that change state are proposed for your confirmation, "
                     "never run on their own.")
    elif cfg:
        lines.append("Read-only: state-changing actions are currently disabled.")
    return "\n".join(lines)


@app.get("/", response_class=HTMLResponse)
async def index():
    """Render the chat page, labelling the backends actually in use.

    Asks the orchestrator rather than duplicating its config, so a split-site
    deployment shows the real inference host instead of a stale hardcoded IP.
    Uses /config, not /health, so page load never waits on probe timeouts.
    """
    llm, apis, cfg = "unknown", "unknown", {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            cfg = (await client.get(f"{ORCHESTRATOR_URL}/config")).json()
        llm = cfg.get("ollama_url", "unknown")
        apis = cfg.get("mcp_server", "unknown")
    except Exception:
        pass  # the page is still usable; the banner just says unknown

    return (HTML_PAGE
            .replace("{{LLM_BACKEND}}", llm)
            .replace("{{API_BACKEND}}", apis)
            .replace("{{WELCOME}}", build_welcome(cfg)))


@app.post("/api/chat")
async def chat(request: dict):
    """Proxy to the orchestrator."""
    # Was 600s only when the model name contained "70b", which gave the
    # largest model the shortest timeout as soon as the naming changed.
    # Model size is not inferable from its name, so allow the long one always;
    # a fast model simply returns sooner.
    timeout = 600.0
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{ORCHESTRATOR_URL}/chat",
            json={
                "message": request["message"],
                "model": request.get("model"),
                "scope": request.get("scope", "all"),
                "conversation_id": request.get("conversation_id"),
            },
        )
        # raise_for_status() discards the orchestrator's message and returns a
        # bare "Internal Server Error", which turned a one-line diagnosis into
        # a guessing game. Pass the detail through.
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text[:500])
            except Exception:
                detail = response.text[:500]
            raise HTTPException(status_code=response.status_code, detail=detail)
        return response.json()


# Schedules and stored reports are plain pass-throughs. The UI holds no state
# of its own: everything it shows survives a browser refresh because it lives in
# the orchestrator's database, not in a tab.

@app.get("/api/schedules")
async def list_schedules():
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{ORCHESTRATOR_URL}/schedules")
        response.raise_for_status()
        return response.json()


@app.post("/api/schedules")
async def create_schedule(request: dict):
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(f"{ORCHESTRATOR_URL}/schedules", json=request)
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code,
                                detail=response.json().get("detail", "Rejected"))
        return response.json()


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str):
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.delete(f"{ORCHESTRATOR_URL}/schedules/{schedule_id}")
        response.raise_for_status()
        return response.json()


@app.post("/api/schedules/{schedule_id}/run")
async def run_schedule(schedule_id: str):
    # A scheduled question does the same tool-calling work as an interactive
    # one, so it needs the same generous ceiling.
    async with httpx.AsyncClient(timeout=600.0) as client:
        response = await client.post(
            f"{ORCHESTRATOR_URL}/schedules/{schedule_id}/run")
        response.raise_for_status()
        return response.json()


@app.get("/api/runs")
async def list_runs(limit: int = 50):
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{ORCHESTRATOR_URL}/runs",
                                    params={"limit": limit})
        response.raise_for_status()
        return response.json()


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{ORCHESTRATOR_URL}/runs/{run_id}")
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="No such report")
        response.raise_for_status()
        return response.json()


@app.get("/api/conversations")
async def list_conversations(limit: int = 50):
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{ORCHESTRATOR_URL}/conversations",
                                    params={"limit": limit})
        response.raise_for_status()
        return response.json()


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{ORCHESTRATOR_URL}/conversations/{conversation_id}")
        # A conversation the browser remembers may have been deleted. Say so
        # plainly so the page can drop the stale id instead of retrying it.
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="No such conversation")
        response.raise_for_status()
        return response.json()


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.delete(
            f"{ORCHESTRATOR_URL}/conversations/{conversation_id}")
        response.raise_for_status()
        return response.json()


@app.post("/api/confirm")
async def confirm(request: dict):
    """Execute a proposed write. The only UI route that changes production."""
    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.post(
            f"{ORCHESTRATOR_URL}/confirm", json={"token": request.get("token")}
        )
        # Surface the orchestrator's refusal verbatim rather than a generic 500,
        # so an expired token reads as an expired token.
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code,
                                detail=response.json().get("detail", response.text))
        return response.json()


@app.post("/api/cancel")
async def cancel(request: dict):
    """Discard a proposed write."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{ORCHESTRATOR_URL}/cancel", json={"token": request.get("token")}
        )
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code,
                                detail=response.json().get("detail", response.text))
        return response.json()


@app.get("/api/health")
async def health():
    """Backend health, used to light or grey the vendor strip."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{ORCHESTRATOR_URL}/health")
            response.raise_for_status()
            return response.json()
    except Exception:
        return {}


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


@app.get("/api/memory")
async def memory_status():
    """Which models are resident on the inference host."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{ORCHESTRATOR_URL}/memory")
            if response.status_code >= 400:
                detail = response.json().get("detail", response.text)
                raise HTTPException(status_code=response.status_code, detail=detail)
            return response.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/memory/{action}")
async def memory_action(action: str):
    """Pin or unpin the assistant model on the inference host."""
    if action not in ("pin", "unpin"):
        raise HTTPException(status_code=404, detail="expected pin or unpin")
    try:
        # Pinning reloads the model from disk, so allow the same generous
        # window the orchestrator uses rather than timing out mid-load.
        async with httpx.AsyncClient(timeout=900.0) as client:
            response = await client.post(f"{ORCHESTRATOR_URL}/memory/{action}")
            if response.status_code >= 400:
                detail = response.json().get("detail", response.text)
                raise HTTPException(status_code=response.status_code, detail=detail)
            return response.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


if __name__ == "__main__":
    import uvicorn
    # Loopback by default. The Tailscale proxy reaches it over loopback, and
    # anything that cannot is not supposed to be talking to this service.
    if UI_AUTH != "none" and UI_BIND not in LOOPBACK:
        raise SystemExit(
            f"Refusing to start: UI_AUTH={UI_AUTH} trusts the identity headers "
            f"the local proxy injects, but UI_BIND={UI_BIND} accepts direct "
            "connections that can set those headers themselves. Bind to "
            "127.0.0.1, or set UI_AUTH=none and accept an open UI."
        )
    print(f"[web_ui] auth={UI_AUTH} bind={UI_BIND}:8091 "
          f"allowed={sorted(UI_ALLOWED_LOGINS) or 'any tailnet user'}")
    # proxy_headers=False on purpose: with it on, uvicorn rewrites the peer
    # address from X-Forwarded-For, and the peer address is the one thing here
    # that must not be attacker-supplied.
    uvicorn.run(app, host=UI_BIND, port=8091, proxy_headers=False)
