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

## Usage

```bash
# Ask a question
curl -X POST http://localhost:8090/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Are there any critical alerts in my environment?"}'

# List available tools
curl http://localhost:8090/tools

# Health check
curl http://localhost:8090/health
```

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
