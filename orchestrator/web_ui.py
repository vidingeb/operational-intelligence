"""
Simple chat web UI for the On-Prem AI Orchestrator.
Serves a single-page chat interface on port 8091.
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import httpx

app = FastAPI(title="On-Prem AI Chat")

ORCHESTRATOR_URL = "http://localhost:8090"

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
        }
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
    </style>
</head>
<body>
    <header>
        <h1>🖥️ On-Prem AI Assistant</h1>
        <span class="badge">Llama 3.1 70B · Local</span>
        <span class="badge">vCenter · VCF Ops · Networks</span>
    </header>
    <div class="info-bar">
        <span>LLM: 10.0.0.141 (Ollama)</span>
        <span>APIs: 10.0.0.140 (MCP Server)</span>
        <span>Model: llama3.1 70B Q4_K_M</span>
    </div>
    <div id="chat-container">
        <div class="message assistant">Hello! I'm your on-premises VMware infrastructure assistant. I can query vCenter, VCF Operations, and VCF Networks — all running locally with no cloud dependency.

Try asking me:
• "What VMs are running?"
• "Are there any critical alerts?"
• "Show me host resource usage"
• "Which datastores are low on space?"</div>
    </div>
    <div id="input-area">
        <input type="text" id="user-input" placeholder="Ask about your VMware infrastructure..." autofocus>
        <button id="send-btn" onclick="sendMessage()">Send</button>
    </div>

    <script>
        const chatContainer = document.getElementById('chat-container');
        const userInput = document.getElementById('user-input');
        const sendBtn = document.getElementById('send-btn');

        userInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });

        async function sendMessage() {
            const message = userInput.value.trim();
            if (!message) return;

            // Add user message
            addMessage(message, 'user');
            userInput.value = '';
            sendBtn.disabled = true;

            // Add thinking indicator
            const thinkingEl = addMessage('Thinking... (querying APIs, this may take 30-60s)', 'thinking');

            try {
                const response = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message }),
                });

                chatContainer.removeChild(thinkingEl);

                if (response.ok) {
                    const data = await response.json();
                    addMessage(data.answer, 'assistant');
                } else {
                    const err = await response.json();
                    addMessage('Error: ' + (err.detail || 'Unknown error'), 'error');
                }
            } catch (e) {
                chatContainer.removeChild(thinkingEl);
                addMessage('Connection error: ' + e.message, 'error');
            }

            sendBtn.disabled = false;
            userInput.focus();
        }

        function addMessage(text, type) {
            const div = document.createElement('div');
            div.className = 'message ' + type;
            div.textContent = text;
            chatContainer.appendChild(div);
            chatContainer.scrollTop = chatContainer.scrollHeight;
            return div;
        }
    </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.post("/api/chat")
async def chat(request: dict):
    """Proxy to the orchestrator."""
    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.post(
            f"{ORCHESTRATOR_URL}/chat",
            json={"message": request["message"]},
        )
        response.raise_for_status()
        return response.json()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8091)
