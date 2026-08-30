# On-Prem AI Orchestrator

A local LLM-powered orchestrator that routes natural-language questions to your VMware APIs using Ollama tool-calling.

## Architecture

```
User → Orchestrator (port 8090) → Ollama (local LLM, port 11434)
                                 → vCenter API (192.0.2.140:8080)
                                 → VCF Operations API (192.0.2.140:8081)
                                 → VCF Networks API (192.0.2.140:8082)
```

Addresses in this repo use the [RFC 5737](https://datatracker.ietf.org/doc/html/rfc5737)
documentation range (`192.0.2.0/24`). They are placeholders, not a real deployment —
point `MCP_SERVER` at your own host.

## Setup

```bash
# Install Python and pip (on Photon OS)
tdnf install -y python3 python3-pip

# Install dependencies
pip3 install -r requirements.txt

# Run the orchestrator (point it at your MCP server)
export MCP_SERVER="http://your-mcp-host"
python3 orchestrator.py
```

## Configuration

All defaults match the original single-site deployment, so running with no
environment set behaves exactly as before.

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434` | Inference endpoint |
| `MCP_SERVER` | `http://192.0.2.140` | Base URL of the five APIs |
| `DEFAULT_MODEL` | `llama3.1:8b` | Model used when the request omits one |
| `OLLAMA_TIMEOUT` | per-model | Seconds; overrides the built-in ceiling |
| `ORCHESTRATOR_URL` | `http://localhost:8090` | Used by `web_ui.py` only |
| `STATE_DB` | `orchestrator/state.db` | Conversations, schedules and stored reports |
| `HISTORY_TURNS` | `6` | Prior exchanges replayed into a follow-up question |
| `SCHEDULER_ENABLED` | `true` | Set false to run without the schedule runner |
| `SCHEDULER_TICK` | `30` | Seconds between checks for a due schedule |
| `UI_BIND` | `127.0.0.1` | Interface `web_ui.py` listens on |
| `UI_AUTH` | `tailscale` | `tailscale` or `none`; any other value refuses to start |
| `UI_ALLOWED_LOGINS` | *(empty)* | Comma-separated logins; empty means any tailnet user |
| `ORCHESTRATOR_BIND` | `127.0.0.1` | Interface `orchestrator.py` listens on |

## Access control

The tailnet limits *which machines* can connect. It says nothing about *who*
is at the keyboard, and until recently neither did the application: anything
that could open a socket to :8091 got a chat box wired to five production
systems, and :8090 answered all 72 tools with no check at all.

`tailscale serve` terminates TLS and injects the caller's identity:

```
Tailscale-User-Login: someone@example.com
Tailscale-User-Name:  Some One
```

Two properties make this usable, both verified against a running `serve`
rather than taken from the documentation:

- A client that sets `Tailscale-User-Login` itself has it **overwritten** on
  the way through the proxy.
- The same forged header sent **directly** to :8091 arrives untouched.

So the header is only trustworthy on the proxied path. That is why identity
alone is not enough, and why the peer address must also be loopback:

| Layer | Question answered | Guarantee |
|---|---|---|
| Loopback bind | *Did this arrive via the local proxy?* | Network — check it with `ss -lntp` |
| Identity header | *Who sent it?* | Tailscale — only meaningful given the above |

Startup **refuses** `UI_AUTH=tailscale` with a non-loopback `UI_BIND`, because
that combination looks protected and is not: anyone who can reach the port can
supply their own header. The check is enforced as middleware over every route,
so a new endpoint cannot forget it.

`serve` must therefore target loopback. Confirm before restarting:

```bash
tailscale serve status     # expect: |-- / proxy http://127.0.0.1:8091
```

To restrict further, name the accounts:

```
Environment=UI_ALLOWED_LOGINS=you@example.com,colleague@example.com
```

Falling back to the previous behaviour is `UI_AUTH=none UI_BIND=0.0.0.0`.

### If you launch it with the uvicorn CLI

`web_ui.py` must run as `python3 web_ui.py`. Its `__main__` sets
`proxy_headers=False` deliberately. Under uvicorn's defaults, `X-Forwarded-For`
rewrites `request.client.host`, so behind a real proxy the peer becomes the
*caller's* tailnet address rather than `127.0.0.1` — and every legitimate
request is refused. Unit tests do not catch this, because Starlette's
`TestClient` never runs that middleware. Launching via the CLI instead needs:

```bash
uvicorn web_ui:app --host 127.0.0.1 --port 8091 --no-proxy-headers
```

A request whose peer address has been rewritten this way returns a 403 that
names the cause, rather than a bare refusal.

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
MCP_SERVER=http://192.0.2.140 \
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

## Deployment on the orchestrator VM

Two services, both reading this repo from `/opt/operational-intelligence`:

| Unit | What it runs |
|---|---|
| `orchestrator.service` | `orchestrator.py` — the agent loop and API on :8090 |
| `orchestrator-ui.service` | `web_ui.py` — the chat page |

```bash
cd /opt/operational-intelligence
git pull
systemctl restart orchestrator orchestrator-ui
```

Both are restarted together because `web_ui.py` and `orchestrator.py` change in
step. `systemctl is-active` only reports that a process is alive; to confirm the
code actually deployed, ask the page for something the new version serves:

```bash
curl -s http://localhost:8090/schedules | head -c 200
```

Auth is worth confirming from the outside as well as the inside, because the
two paths are supposed to behave differently:

```bash
curl -s https://<host>.ts.net/api/whoami        # 200, and your own login
curl -s http://localhost:8091/api/whoami        # 403 — no identity on this path
ss -lntp | grep -E '809[01]'                    # both should show 127.0.0.1, not *
```

## Memory and scheduled reports

The chat endpoint is stateless unless given a `conversation_id`. Pass one and
the previous exchanges are replayed, which is what makes "and which of those are
powered off?" resolve. Only prose is replayed — tool results are not, because a
single estate answer can be 12k tokens of JSON and three of those would push the
real question out of the context window.

```bash
# First question - returns a conversation_id
curl -X POST http://localhost:8090/chat -H "Content-Type: application/json" \
  -d '{"message": "what VMs are running?"}'

# Follow-up, in the same thread
curl -X POST http://localhost:8090/chat -H "Content-Type: application/json" \
  -d '{"message": "which of those are powered off?", "conversation_id": "abc123"}'
```

Schedules run questions unattended and store the answer:

```bash
curl -X POST http://localhost:8090/schedules -H "Content-Type: application/json" \
  -d '{"question": "Which VMs have no recent restore point?", "kind": "daily", "hour": 7, "minute": 0}'

curl http://localhost:8090/schedules      # what is scheduled, and when it next runs
curl http://localhost:8090/runs           # stored reports
curl -X POST http://localhost:8090/schedules/<id>/run   # run one now, without waiting
```

Times are **UTC**. A schedule that shifts by an hour twice a year is a bug that
takes months to notice.

**Scheduled runs are always read-only.** State-changing tools are withheld from
them regardless of `ENABLE_WRITE_TOOLS`, and a call to one is refused even if
the model names it anyway — nobody is watching a job that fires at 07:00.

A missed window fires **once**, not once per missed slot: two days of downtime
must not release two days of backlog against five production APIs.

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
