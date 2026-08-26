# On-Prem AI Orchestrator

A local LLM-powered orchestrator that routes natural-language questions to your VMware APIs using Ollama tool-calling.

## Architecture

```
User → Orchestrator (port 8090) → Ollama (local LLM, port 11434)
                                 → vCenter API (10.0.0.140:8080)
                                 → VCF Operations API (10.0.0.140:8081)
                                 → VCF Networks API (10.0.0.140:8082)
```

## Setup

```bash
# Install Python and pip (on Photon OS)
tdnf install -y python3 python3-pip

# Install dependencies
pip3 install -r requirements.txt

# Run the orchestrator
python3 orchestrator.py
```

## Configuration

All defaults match the original single-site deployment, so running with no
environment set behaves exactly as before.

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434` | Inference endpoint |
| `MCP_SERVER` | `http://10.0.0.140` | Base URL of the three APIs |
| `DEFAULT_MODEL` | `llama3.1:8b` | Model used when the request omits one |
| `OLLAMA_TIMEOUT` | per-model | Seconds; overrides the built-in ceiling |
| `ORCHESTRATOR_URL` | `http://localhost:8090` | Used by `web_ui.py` only |

## Split-site deployment (remote inference)

Inference does not have to run beside the data. Pointing `OLLAMA_URL` at a
larger machine elsewhere — for example a DGX Spark GB10 over a tailnet — buys
much stronger multi-step tool calling than an 8B model can manage:

```
       site with the data                    site with the GPU
┌────────────────────────────┐        ┌──────────────────────────┐
│ Orchestrator :8090         │───────►│ GB10 · Ollama :11434     │
│  → vCenter API :8080       │ tailnet│   gpt-oss:120b resident  │
│  → VCF Ops API :8081       │  text  └──────────────────────────┘
│  → VCF Networks API :8082  │   only
└────────────────────────────┘
```

```bash
OLLAMA_URL=http://gb10.your-tailnet.ts.net:11434 \
DEFAULT_MODEL=gpt-oss:120b \
MCP_SERVER=http://10.0.0.140 \
python3 orchestrator.py
```

Push inference to the data, not the other way round. Only prompts and tool
results cross the link; vCenter credentials, `pyVmomi`, and the API surface
never leave the site. The inference host needs no access to vCenter at all.

Because Tailscale is outbound-only, the orchestrator dials out and no inbound
firewall rule or subnet router is required.

### Restrict the tailnet ACL

Grant the orchestrator exactly one destination port and nothing else. Tag both
nodes, then in the Tailscale admin console:

```jsonc
{
  "tagOwners": {
    "tag:orchestrator": ["autogroup:admin"],
    "tag:inference":    ["autogroup:admin"]
  },
  "acls": [
    {
      // the orchestrator may reach the model, and nothing else
      "action": "accept",
      "src":    ["tag:orchestrator"],
      "dst":    ["tag:inference:11434"]
    }
  ]
}
```

Tagged nodes do not expire, which matters for an unattended host — untagged
devices expire (default 180 days) and would silently drop off.

> **Before connecting anything to a network you do not own:** this creates a
> persistent path in and out of that network which bypasses the corporate VPN
> and the controls attached to it, and sends operational data (hostnames, IPs,
> alerts, capacity) to a machine that network's owner does not control. Get
> explicit sign-off first. A lab or nested environment reproduces the same
> setup with none of the exposure.

## Usage

```bash
# Ask a question
curl -X POST http://localhost:8090/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Are there any critical alerts in my environment?"}'

# List available tools
curl http://localhost:8090/tools

# Which backends are configured (instant, no probing)
curl http://localhost:8090/config

# Liveness of inference and each API
curl http://localhost:8090/health
```

`/health` always returns HTTP 200 so a probe can read the detail; branch on
the `status` field instead:

| `status` | Meaning |
|---|---|
| `ok` | Inference and all three APIs reachable |
| `degraded` | Inference up, at least one API unreachable — answers won't be grounded in live data |
| `unavailable` | Inference unreachable — nothing will work |

It also reports `models_resident`, so you can tell a warm model from one that
will pay a load cost on the next request.

## How it works

1. User sends a natural-language question to `/chat`
2. The orchestrator forwards it to Ollama (Llama 3.2) with tool definitions
3. The LLM decides which API(s) to call based on the question
4. The orchestrator executes those API calls against the MCP server
5. Results are fed back to the LLM for synthesis
6. A human-readable answer is returned

## Example Questions

- "What's the overall health of my environment?"
- "Are there any VMs with old snapshots?"
- "Show me the resource usage on my ESXi hosts"
- "Are there any critical alerts I should worry about?"
- "What network segments is the VM 'web-01' connected to?"
- "Which datastores are running low on space?"
