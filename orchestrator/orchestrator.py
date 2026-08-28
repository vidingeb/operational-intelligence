"""
On-prem AI Orchestrator — routes natural-language questions to vCenter,
VCF Operations, and VCF Networks APIs via a local Ollama LLM.

Runs on the LLM VM (10.0.0.141) and calls APIs on the MCP server (10.0.0.140).
"""

import os
import json
import time
import httpx
import asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="On-Prem AI Orchestrator", version="1.0")

# Configuration
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MCP_SERVER = os.getenv("MCP_SERVER", "http://10.0.0.140")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gpt-oss:120b")

# Descriptions for models we know about. This is annotation only — the
# authoritative list is whatever Ollama actually has installed, fetched at
# runtime. Hardcoding a menu of models that aren't on the box just produces
# a dropdown where every option fails.
MODEL_NOTES = {
    "gpt-oss:120b": "Largest — best reasoning, stays resident",
    "gpt-oss:20b": "Mid-size, good balance",
    "qwen3:8b": "Fast — good for daily use",
    "llama3.1:8b": "Fast (~30-60s) — good for daily use",
    "hermes3": "Optimized for tool calling",
    "nemotron-3-nano:4b": "NVIDIA agent-optimized",
    "qwen2.5:7b": "Excellent tool calling for its size",
    "llama3.1:70b": "Slow — best accuracy",
    "llama3.2": "Fastest — basic queries",
}

VCENTER_BASE = f"{MCP_SERVER}:8080"
OPS_BASE = f"{MCP_SERVER}:8081"
NETWORKS_BASE = f"{MCP_SERVER}:8082"

# Tool definitions for Ollama (subset of most useful operations)
TOOLS = [
    # --- vCenter tools ---
    {
        "type": "function",
        "function": {
            "name": "vcenter_list_hosts",
            "description": "List all ESXi hosts with their status and resource usage",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "vcenter_list_vms",
            "description": "List all virtual machines with power state and basic info",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "vcenter_search_vms",
            "description": "Search for virtual machines by name",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "VM name to search for"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "vcenter_vm_details",
            "description": "Get detailed information about a specific VM including CPU, memory, disks, network",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact VM name"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "vcenter_host_usage",
            "description": "Show ESXi host resource usage (CPU, memory utilization)",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "vcenter_datastores",
            "description": "List all datastores with capacity and free space",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "vcenter_alarms",
            "description": "List active vCenter alarms",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "vcenter_clusters",
            "description": "List vSphere clusters with summary info",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "vcenter_recent_tasks",
            "description": "Show recent vCenter tasks",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of tasks (default 20)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "vcenter_powered_off_vms",
            "description": "List virtual machines that are powered off",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "vcenter_old_snapshots",
            "description": "List old snapshots that may need cleanup",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Age threshold in days (default 14)"}
                },
                "required": []
            }
        }
    },
    # --- VCF Operations tools ---
    {
        "type": "function",
        "function": {
            "name": "ops_summary",
            "description": "Get VCF Operations environment summary (overall health, resource counts)",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ops_alerts",
            "description": "List active VCF Operations alerts",
            "parameters": {
                "type": "object",
                "properties": {
                    "activeOnly": {"type": "boolean", "description": "Only active alerts (default true)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ops_critical_alerts",
            "description": "List only critical severity alerts from VCF Operations",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ops_top_alerts",
            "description": "List top active alerts sorted by severity",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of alerts (default 10)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ops_resources_search",
            "description": "Search VCF Operations resources (VMs, hosts, clusters) by name",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Resource name or partial name"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ops_recommendations",
            "description": "List VCF Operations recommendations for optimization",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ops_symptoms",
            "description": "List active symptoms detected by VCF Operations",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    # --- VCF Networks tools ---
    {
        "type": "function",
        "function": {
            "name": "networks_search",
            "description": "Search VCF Networks (Network Insight) for VMs, switches, routers by name or IP",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Name, IP, or filter expression"},
                    "entity_type": {"type": "string", "description": "Entity type: VirtualMachine, NSXTLogicalSwitch, NSXTLogicalRouter"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "networks_alerts",
            "description": "List active network alerts from VCF Networks",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "networks_vms",
            "description": "List virtual machines from a network perspective (IPs, segments, flows)",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "networks_nsx_segments",
            "description": "List NSX segments/logical switches",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Segment name filter"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "networks_hosts",
            "description": "List hosts from a network perspective",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "networks_clusters",
            "description": "List clusters from a network perspective",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
]

# Map tool names to actual API endpoints
TOOL_ENDPOINTS = {
    "vcenter_list_hosts": ("GET", f"{VCENTER_BASE}/hosts"),
    "vcenter_list_vms": ("GET", f"{VCENTER_BASE}/vms"),
    "vcenter_search_vms": ("GET", f"{VCENTER_BASE}/vms/search"),
    "vcenter_vm_details": ("GET", f"{VCENTER_BASE}/vm/details"),
    "vcenter_host_usage": ("GET", f"{VCENTER_BASE}/hosts/usage"),
    "vcenter_datastores": ("GET", f"{VCENTER_BASE}/datastores"),
    "vcenter_alarms": ("GET", f"{VCENTER_BASE}/alarms"),
    "vcenter_clusters": ("GET", f"{VCENTER_BASE}/clusters/summary"),
    "vcenter_recent_tasks": ("GET", f"{VCENTER_BASE}/tasks/recent"),
    "vcenter_powered_off_vms": ("GET", f"{VCENTER_BASE}/vms/poweredoff"),
    "vcenter_old_snapshots": ("GET", f"{VCENTER_BASE}/snapshots/old"),
    "ops_summary": ("GET", f"{OPS_BASE}/ops/summary"),
    "ops_alerts": ("GET", f"{OPS_BASE}/ops/alerts"),
    "ops_critical_alerts": ("GET", f"{OPS_BASE}/ops/critical-alerts"),
    "ops_top_alerts": ("GET", f"{OPS_BASE}/ops/top-alerts"),
    "ops_resources_search": ("GET", f"{OPS_BASE}/ops/resources/search"),
    "ops_recommendations": ("GET", f"{OPS_BASE}/ops/recommendations"),
    "ops_symptoms": ("GET", f"{OPS_BASE}/ops/symptoms"),
    "networks_search": ("GET", f"{NETWORKS_BASE}/ni/search"),
    "networks_alerts": ("GET", f"{NETWORKS_BASE}/ni/alerts"),
    "networks_vms": ("GET", f"{NETWORKS_BASE}/ni/vms"),
    "networks_nsx_segments": ("GET", f"{NETWORKS_BASE}/ni/entities/nsx-segments"),
    "networks_hosts": ("GET", f"{NETWORKS_BASE}/ni/hosts"),
    "networks_clusters": ("GET", f"{NETWORKS_BASE}/ni/clusters"),
}

SYSTEM_PROMPT = """You are an on-premises VMware infrastructure assistant. You have access to three API systems:

1. **vCenter API** — manages VMs, hosts, clusters, datastores, snapshots, alarms, and power operations
2. **VCF Operations API** — monitors health, alerts, recommendations, symptoms, cost analysis, and performance metrics
3. **VCF Networks API** — provides network topology, traffic flows, NSX segments, security policies, and connectivity

When a user asks a question:
- Use the appropriate tool(s) to gather data before answering
- Call multiple tools in parallel when the question spans multiple domains
- Provide concise, actionable summaries
- If something looks unhealthy, suggest next steps
- Never guess — always check the APIs first"""


async def call_api(tool_name: str, arguments: dict) -> dict:
    """Execute an API call based on the tool name and arguments."""
    if tool_name not in TOOL_ENDPOINTS:
        return {"error": f"Unknown tool: {tool_name}"}

    method, url = TOOL_ENDPOINTS[tool_name]

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            if method == "GET":
                response = await client.get(url, params=arguments or None)
            else:
                response = await client.post(url, json=arguments or None)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            return {"error": f"API returned {e.response.status_code}: {e.response.text}"}
        except httpx.ConnectError:
            return {"error": f"Cannot connect to {url} — is the MCP server running?"}
        except Exception as e:
            return {"error": str(e)}


async def chat_with_tools(user_message: str, model: str = None, conversation: list = None) -> str:
    """Send a message to Ollama with tool-calling, execute tools, return final answer."""
    use_model = model or DEFAULT_MODEL
    if conversation is None:
        conversation = [{"role": "system", "content": SYSTEM_PROMPT}]

    conversation.append({"role": "user", "content": user_message})

    # Use longer timeout for 70B model
    timeout = 600.0 if "70b" in use_model else 120.0

    async with httpx.AsyncClient(timeout=timeout) as client:
        # First call — LLM decides which tools to use
        response = await client.post(f"{OLLAMA_URL}/api/chat", json={
            "model": use_model,
            "messages": conversation,
            "tools": TOOLS,
            "stream": False,
        })
        response.raise_for_status()
        result = response.json()

        assistant_message = result["message"]
        conversation.append(assistant_message)

        # If no tool calls, return the direct response
        if not assistant_message.get("tool_calls"):
            return assistant_message.get("content", "")

        # Execute tool calls in parallel
        tool_calls = assistant_message["tool_calls"]
        tasks = []
        for tc in tool_calls:
            fn = tc["function"]
            tasks.append(call_api(fn["name"], fn.get("arguments", {})))

        results = await asyncio.gather(*tasks)

        # Feed results back to LLM
        for tc, result_data in zip(tool_calls, results):
            conversation.append({
                "role": "tool",
                "content": json.dumps(result_data, default=str)[:4000],  # Truncate large responses
            })

        # Second call — LLM synthesizes the answer
        response = await client.post(f"{OLLAMA_URL}/api/chat", json={
            "model": use_model,
            "messages": conversation,
            "stream": False,
        })
        response.raise_for_status()
        final = response.json()

        return final["message"].get("content", "")


# --- API Endpoints ---

class ChatRequest(BaseModel):
    message: str
    model: str = None  # Optional model override

class ChatResponse(BaseModel):
    answer: str
    model: str


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "default_model": DEFAULT_MODEL,
        "installed_models": await installed_models(),
        "mcp_server": MCP_SERVER,
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Ask a question about your VMware infrastructure."""
    use_model = request.model or DEFAULT_MODEL
    available = await installed_models()
    if available and use_model not in available:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{use_model}' is not installed on this host. "
                   f"Installed: {available}. Pull it with: ollama pull {use_model}",
        )
    try:
        answer = await chat_with_tools(request.message, model=use_model)
        return ChatResponse(answer=answer, model=use_model)
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Cannot connect to Ollama — is it running?")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def installed_models():
    """Ask Ollama what it actually has. Empty list if it cannot be reached."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{OLLAMA_URL}/api/tags")
            response.raise_for_status()
            return [m["name"] for m in response.json().get("models") or []]
    except httpx.HTTPError:
        return []


@app.get("/models")
async def list_models():
    """List the models this host can actually serve, largest first."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{OLLAMA_URL}/api/tags")
            response.raise_for_status()
            models = response.json().get("models") or []
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Cannot reach Ollama: {e}")

    models.sort(key=lambda m: m.get("size") or 0, reverse=True)
    return [
        {
            "id": m["name"],
            "size_gb": round((m.get("size") or 0) / 1024**3, 1),
            "description": MODEL_NOTES.get(m["name"], ""),
            "default": m["name"] == DEFAULT_MODEL,
        }
        for m in models
    ]


@app.get("/tools")
async def list_tools():
    """List all available tools the LLM can use."""
    return [
        {"name": t["function"]["name"], "description": t["function"]["description"]}
        for t in TOOLS
    ]


# --- Memory management -------------------------------------------------
# The GB10 has one pool of unified memory and no MIG, so a pinned model
# blocks anything else that wants the GPU. These endpoints let the UI
# release it on demand instead of requiring an SSH session.

ASSISTANT_MODEL = os.getenv("ASSISTANT_MODEL", DEFAULT_MODEL)


async def _set_keep_alive(model: str, keep_alive):
    """A request with no prompt loads or evicts without generating tokens."""
    async with httpx.AsyncClient(timeout=900.0) as client:
        response = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model, "keep_alive": keep_alive},
        )
        response.raise_for_status()
        return response.json()


@app.get("/memory")
async def memory_status():
    """Which models are resident, and how much memory they hold."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{OLLAMA_URL}/api/ps")
            response.raise_for_status()
            models = response.json().get("models") or []
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Cannot reach Ollama: {e}")

    return {
        "assistant_model": ASSISTANT_MODEL,
        "pinned": any(m.get("name") == ASSISTANT_MODEL for m in models),
        "total_gb": round(sum(m.get("size") or 0 for m in models) / 1024**3, 1),
        "models": [
            {
                "name": m.get("name"),
                "size_gb": round((m.get("size") or 0) / 1024**3, 1),
                "expires_at": m.get("expires_at"),
            }
            for m in models
        ],
    }


@app.post("/memory/pin")
async def memory_pin(model: str = None):
    """Load the assistant model and hold it resident indefinitely."""
    target = model or ASSISTANT_MODEL
    started = time.monotonic()
    try:
        await _set_keep_alive(target, -1)
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Could not pin {target} — is something else holding memory? ({e})",
        )
    return {"pinned": target, "seconds": round(time.monotonic() - started, 1)}


@app.post("/memory/unpin")
async def memory_unpin(model: str = None):
    """Evict the assistant model now, freeing its memory for other work."""
    target = model or ASSISTANT_MODEL
    started = time.monotonic()
    try:
        await _set_keep_alive(target, 0)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Could not unpin {target}: {e}")
    return {"unpinned": target, "seconds": round(time.monotonic() - started, 1)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
