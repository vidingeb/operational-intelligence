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
| `FLOW_INVENTORY_CACHE_TTL` | `60` | VCF Networks service: seconds to reuse a resolved flow inventory; `0` disables |
| `FLOW_INVENTORY_CACHE_MAX_ENTRIES` | `32` | VCF Networks service: maximum cached inventory variants |
| `UI_BIND` | `127.0.0.1` | Interface `web_ui.py` listens on |
| `UI_AUTH` | `tailscale` | `tailscale` or `none`; any other value refuses to start |
| `UI_ALLOWED_LOGINS` | *(empty)* | Comma-separated logins; empty means any tailnet user |
| `ORCHESTRATOR_BIND` | `127.0.0.1` | Interface `orchestrator.py` listens on |
| `DIAGRAM_PUBLIC_URL_BASE` | *(empty)* | Absolute HTTPS `/diagrams` proxy URL for measured PNGs, shared by both clients |

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

### Open WebUI progress

The OpenAI-compatible `/v1/chat/completions` stream reports operational
milestones before the answer: tool selection, each named tool execution, flow
inventory resolution, and final answer analysis. Status chunks use
`choices[0].delta.reasoning_content` plus a machine-readable top-level
`x_copilot_status` object:

```json
{
  "choices": [{"delta": {"reasoning_content": "Executing networks_flow_inventory\n"}}],
  "x_copilot_status": {
    "stage": "executing_tool",
    "message": "Executing networks_flow_inventory",
    "elapsed_seconds": 2.184,
    "tool": "networks_flow_inventory"
  }
}
```

Current Open WebUI renders `reasoning_content` in its separate collapsible
activity/reasoning panel. It is never sent as an answer `content` delta or
added to the model conversation. Other OpenAI clients may ignore that optional
delta field and the `x_copilot_status` extension; normal answer chunks and the
terminal `data: [DONE]` remain unchanged. This reports milestones, not model
reasoning or chain-of-thought.

### Screenshot input and Qwen3-VL

The existing `assistant-*` OpenAI models accept screenshots as inline
`image_url` data URLs. The orchestrator uses the configured vision sidecar only
to transcribe/describe the image, labels that transcription as unverified, and
then hands the bounded text to `DEFAULT_MODEL` for VMware reasoning and tool
use. Qwen3-VL is not exposed as a separate assistant model and does not receive
tool credentials or raw API results.

The lab deployment uses:

```ini
Environment="VISION_MODEL=qwen3-vl:30b"
Environment="VISION_NUM_CTX=8192"
Environment="VISION_KEEP_ALIVE=2m"
Environment="VISION_MAX_IMAGES=4"
Environment="VISION_MAX_IMAGE_BYTES=8388608"
Environment="VISION_MAX_TOTAL_BYTES=16777216"
```

`qwen3-vl:30b` requires Ollama 0.12.7 or newer. Check `/api/version`, free disk,
and available memory before pulling it. The two-minute keep-alive permits
follow-up screenshot turns without pinning the vision model indefinitely;
`gpt-oss:120b` remains the primary reasoning/tool model. Observe `/api/ps`
after representative requests and lower the keep-alive if the host experiences
memory pressure.

Open WebUI sends the image through the existing OpenAI connection. The custom
On-Prem AI Assistant supports file selection and clipboard paste, shows
removable thumbnail/name previews, and sends the same inline data-URL format.
Both paths accept PNG, JPEG, WebP, and GIF only, with at most four images,
8 MiB per decoded image, and 16 MiB decoded total per turn. Remote image URLs
are rejected and never fetched. Malformed, mismatched-MIME, empty, oversized,
or excessive attachments return an explicit error rather than being dropped.
The custom UI clears attachments only after a successful response.

For streaming OpenAI requests, `analyzing_image` is emitted before normal tool
milestones. The custom UI displays “Analyzing image before VMware checks…”
while its conversation-preserving `/chat` request runs. Images are not written
to conversation history; an image-only custom-UI turn stores only
`[Image attached]`. Vision logs contain model, count, elapsed time, and error
class, never image data or transcription.

Image text is evidence supplied by a vision model, not measured infrastructure
state. Hostnames, addresses, digits, statuses, and measurements remain
unverified until a read-only tool confirms them. If vision is disabled or
fails, the assistant says that the image was unread rather than guessing.

### vCenter folder inventory

`vcenter_folders` is the authoritative read-only folder list/search tool. Its
vCenter endpoint is `GET /folders`, with optional case-insensitive `name` and
`exact=true|false` parameters. Results include stable managed-object IDs and full inventory paths,
parent path/name, VM/host/network/datastore/generic category, an explicit
system-folder flag, and a bounded immediate-child summary. Duplicate folder
names remain distinct through their full paths. The response reports
`complete`, result count, and exact-versus-contains match semantics;
Datacenter objects are explicitly excluded because they are not folders.

The assistant may claim that a folder is absent only after this endpoint
returns `complete=true` with zero matches. VM, host, cluster, datastore, and
NSX searches do not prove folder absence. A screenshot response from a general
model such as `qwen3:8b` is likewise unsupported evidence until the folder tool
confirms it.

### Measured infrastructure diagrams

`folder_topology_diagram` is a local **read-only** tool in the `all` and
`vcenter` scopes. Python, not the model, collects the inventory, builds a
typed graph, emits Mermaid, renders a local PNG and writes the final report.
The tool loop returns that report directly, without a model synthesis round.
Usage accounting, conversation history, pending write approvals and scheduled
read-only operation keep their existing behavior. Model-generated Mermaid or
links to unverified diagram images are withheld rather than passed off as
measured topology. This is an intentional change from syntax-only validation.

The tool accepts:

```json
{"selector":{"path":"/Datacenters/DC/vm/Apps"},"recursive":true}
```

Exactly one selector field is allowed: `folder_id`, `path`, or `name`.
Paths are full, case-sensitive inventory paths; each segment is percent-encoded
so a slash in a folder name is not a path separator. `name` is an exact,
case-insensitive match, **not** a VM-name search. Duplicate folder names produce
HTTP 409 with candidate IDs/paths; choose an ID or full path rather than the
first result. `GET /folders` supplies these selectors.

Paths include the inventory root's name when present (for example
`/Datacenters/DC/vm/Apps`), retaining the deployed `/folders` path format.
Parent traversal identifies the root by managed-object type, ID and server GUID,
not Python reference identity: pyVmomi may return separate proxies for the same
root. A real missing ancestor, wrong root/server or cyclic chain still fails
collection; reaching a null parent is not accepted as proof of the expected root.

The Windows wrapper supplies `GET /folders/topology` with the same
`folder_id|path|name` selector and `recursive=true|false`. Recursive membership
includes descendant folders/vApps; `false` includes only immediate VM children.
The response explicitly states the mode. Views are destroyed in `finally`.
Failed folder enumeration/resolution returns an error, not a claim of absence.
A complete scan with no matching folder returns 404. An old wrapper's generic
404 `Not Found` is reported as **unsupported: update Windows first**.

The response schema is `folder-topology-v1`: vCenter source, collection UTC
timestamp, exact folder identity, membership mode, completeness/errors,
enumerated/examined/returned VM counts, truncation and per-VM identities,
runtime host, datastores and per-vNIC backing. V1 does not truncate. An empty
folder is reported only after a complete collection; partial results retain
observed objects and explicit per-object/per-relationship errors. Failed
identity reads do not generate placeholder VM names. Counts can differ on
incomplete collections, and the report shows that difference. PNG/Mermaid
output is withheld if any enumerated VM identity is missing without explicit
truncation; rendered VM counts must equal the enumerated population. Partial
host/storage/network reads can still render all identified VMs with explicit
errors and only the proven edges.

The normalized `measured-graph-v1` graph contains typed nodes and edges.
Node IDs encode the complete type/object ID, never the display name or a
shortened hash. Shared objects deduplicate by identity, duplicate names stay
distinct, labels are escaped, and nodes/edges sort deterministically. Every
edge has `source`, `endpoint`, `object_id`, `relationship` and `target_id`
evidence. Only these edges exist:

- Selected folder to enumerated VMs: immediate or recursive membership.
- VM to its observed `runtime.host`.
- VM to datastores returned by `VirtualMachine.datastore`.
- VM to its actual device-key vNIC, then to its resolved network backing.

Distributed backing resolves the actual switch UUID/portgroup key; unresolved
backing remains an explicit error with the observed keys. Opaque network
IDs/types stay opaque, **not** inferred NSX segments. Storage/network edges
mean backing/attachment, **not traffic**. No folder-to-host or host-to-storage
shortcut, application role, SNMP/API dependency, backup protection/repository,
firewall ALLOW/DROP rule, forced firewall hop or host metric is inferred.
Firewall rules, traffic and host metrics are explicitly not examined in v1.
A proposed design is not measured evidence and must be discussed separately
as a proposal; this tool does not draw hypothetical topology.

Progress callbacks emit `folder_resolution`, `topology_collection`,
`diagram_generation` and `diagram_rendering`. OpenAI streaming carries these
through the existing status frames. Collection and renderer failures remain
explicit; a retained Mermaid source is not described as a successful PNG.

#### Images in both clients

Set `DIAGRAM_PUBLIC_URL_BASE` to the authenticated absolute HTTPS origin/path
that actually serves `/diagrams`, for example
`https://assistant.your-tailnet.ts.net/diagrams`. Both the custom `/chat`
response and OpenAI `/v1/chat/completions` response embed the **same URL**.
Relative URLs are not emitted by this tool: Open WebUI runs at another
origin and cannot be assumed to have that route. A missing/invalid public
base is an explicit image error, not a public-endpoint fallback.

The custom UI creates responsive image elements only for hash-named PNGs on
the local `/diagrams` path or the configured trusted HTTPS base. It never
interprets arbitrary HTML or fetches images from other origins. Mermaid source
is collapsed in optional details; image failures show an explicit message.
Open/download controls use the same authenticated URL. Stored answers use the
same renderer, and PDF export waits for images (or prints a visible load error)
instead of printing a Markdown image token or silently omitting the diagram.

Preserve the existing Tailscale Serve `/diagrams` mapping to
`http://127.0.0.1:8090/diagrams` and `DIAGRAM_AUTH=tailscale`.
Confirm the route with `tailscale serve status`, then retrieve a rendered
image through HTTPS as an authenticated tailnet user. No new public route,
auth bypass, third-party image model, export service or Ollama runtime is
needed. PNG rendering reuses the existing offline Mermaid container and
content-hash filenames. Leave vision configuration unchanged.

#### Windows-first rollout and rollback

**Manual deployment gate:** this change must not restart either host during
development. Obtain coordinated approval before running these instructions.
Use the tested commit SHA supplied in the implementation handoff.

On the Windows API host, run elevated PowerShell:

```powershell
$ErrorActionPreference = 'Stop'
$ExpectedCommit = '<tested commit SHA from handoff>'
Set-Location C:\MCP
if (git status --porcelain --untracked-files=no) { throw 'Tracked changes: stop and preserve them' }
git fetch origin
if ($LASTEXITCODE -ne 0) { throw 'git fetch failed' }
git switch vidingeb-verified-infrastructure-diagrams
if ($LASTEXITCODE -ne 0) { throw 'git switch failed' }
git pull --ff-only origin vidingeb-verified-infrastructure-diagrams
if ($LASTEXITCODE -ne 0) { throw 'git pull failed' }
if ((git rev-parse HEAD).Trim() -ne $ExpectedCommit) { throw 'Unexpected checkout SHA' }
& C:\Python\python.exe -m py_compile C:\MCP\vcenter\vcenter_api.py
if ($LASTEXITCODE -ne 0) { throw 'Python syntax check failed' }
Restart-Service mcp-vcenter
Get-Service mcp-vcenter
$matches = Invoke-RestMethod 'http://127.0.0.1:8080/folders?name=Tn3-Pod1&exact=true'
if (-not $matches.complete -or $matches.count -ne 1) { throw 'Resolve folder ambiguity/errors first' }
$path = [Uri]::EscapeDataString($matches.folders[0].path)
$topology = Invoke-RestMethod "http://127.0.0.1:8080/folders/topology?path=$path&recursive=true"
$topology | ConvertTo-Json -Depth 12
if ($topology.schema_version -ne 'folder-topology-v1' -or -not $topology.complete) {
    throw 'Topology unsupported/incomplete: inspect errors before Linux rollout'
}
if ($topology.counts.enumerated -ne $topology.counts.returned -or
    $topology.counts.returned -ne @($topology.vms).Count) { throw 'Topology count mismatch' }
```

No dependency install or NSSM service-path change is required. The existing
service runs `C:\Python\python.exe -m uvicorn vcenter_api:app --host 0.0.0.0
--port 8080` in `C:\MCP\vcenter`; diagnostics are in
`C:\MCP\logs\mcp-vcenter.log`.

Only after Windows success and coordinated approval, update
`/opt/operational-intelligence` on the orchestrator to the same pinned branch
and SHA (fetch, switch, fast-forward only, verify SHA). Stop if tracked files
are modified. Preserve the state DB, the untracked DB backup and all existing
systemd environment entries; do not clean, reset or replace the checkout.
Add only `Environment="DIAGRAM_PUBLIC_URL_BASE=<verified HTTPS /diagrams URL>"`
to an orchestrator-service drop-in, leaving the existing renderer, auth,
vision, model and cache settings intact. Then run `systemctl daemon-reload`
and restart `orchestrator.service` and `orchestrator-ui.service` to load the
new image renderer. No Tailscale Serve reconfiguration is needed.

Verify `/health`, a folder diagram in both UIs, exact VM counts/names and the
same authenticated PNG URL. Check a screenshot turn, daily health report and
cached flow lookup for regressions. A live membership preview before Windows
update can use `/folders?name=Tn3-Pod1&exact=true`, but this proves only folder
identity/child summaries, **not host/storage/network topology**.

Rollback in reverse order after approval: check out the prior deployed
commit `f4aeb52d0d9094797e114dbe9fb04a6829fb27ef` on Linux, remove only the
new public-URL environment entry, reload systemd and restart both orchestrator
and custom UI services.
On Windows, restore that same commit and restart `mcp-vcenter`. Preserve all
other env values, databases/backups and service configuration. No model
removal is involved.

For the vision feature, rollback is configuration-only after reverting its application commit: restore
the prior `VISION_MODEL`/`VISION_NUM_CTX` values in the existing systemd
drop-in, run `systemctl daemon-reload`, and restart only the orchestrator and
custom WebUI services. Removing the downloaded Ollama model is optional and
should be a separate, explicit storage-management decision.

### Flow inventory cache

`/ni/flows/inventory` caches only fully resolved successful responses in the
VCF Networks process. The exact `(hours, limit, traffic_type, vm)` request is
the key. Entries expire after `FLOW_INVENTORY_CACHE_TTL` seconds, the oldest
entry is evicted above `FLOW_INVENTORY_CACHE_MAX_ENTRIES`, and callers receive
deep copies. Identical concurrent misses share one upstream resolution.
Failures and partial results containing a failed flow detail are not cached.
Set the TTL to `0` to disable both reuse and request coalescing.

### Deterministic daily health report

`POST /reports/daily-health` runs a fixed, read-only collection plan across
vCenter, VCF Operations, Logs, VCF Networks, and Veeam. It does not ask the
model which tools to call, count records in prose, calculate capacity, assign
core status, or construct correlations. Python normalizes each query into:

`source`, `query`, `collected_at`, `window`, `status`, `truncated`,
`records_examined`, `records_returned`, `error`, and `data`.

Statuses are `complete`, `partial`, `truncated`, `failed`, `not_configured`, or
`unsupported`. A missing required check produces `UNKNOWN`, never a healthy
aggregate. Direct Dell switch telemetry and BMC/iDRAC hardware health are not
configured in this environment; IPMI log messages remain log evidence only.
The Veeam wrapper does not expose repository capacity, so datastore/vSAN
capacity is never relabelled as backup-repository capacity.

Run and save a preview:

```bash
curl -s -X POST http://127.0.0.1:8090/reports/daily-health \
  -H 'Content-Type: application/json' \
  -d '{"hours":24,"flow_limit":100}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["report_markdown"])'
```

The response includes the normalized sources, deterministic analysis, and
`report_markdown`. `GET /reports/daily-health/latest` returns the most recent
persisted snapshot. Snapshots are versioned and later runs calculate
new/resolved/worsened/improved/unchanged findings against the previous
comparable run. The first run states that no baseline exists.

The same collector is exposed as the read-only `daily_health_report` local
assistant tool. The model is instructed to reproduce `report_markdown`
verbatim, not rewrite its facts. Streaming clients receive collection phases
through the existing status mechanism.

For unattended execution, create a normal schedule whose question is exactly
`daily health report`. That exact scheduled question bypasses inference and
stores the deterministic Markdown directly:

```bash
curl -s -X POST http://127.0.0.1:8090/schedules \
  -H 'Content-Type: application/json' \
  -d '{"question":"daily health report","kind":"daily","hour":7,"minute":0}'
```

Times are UTC. The v1 report is intentionally conservative: direct switch
health, direct server sensors, storage latency/path health, and Veeam
repository capacity remain unknown until matching APIs are configured.

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
5. Results are fed back to the LLM for synthesis, except measured folder diagrams,
   whose authoritative Python report returns directly
6. A human-readable answer is returned

## Example Questions

- "What's the overall health of my environment?"
- "Are there any VMs with old snapshots?"
- "Show me the resource usage on my ESXi hosts"
- "Are there any critical alerts I should worry about?"
- "What network segments is the VM 'web-01' connected to?"
- "Which datastores are running low on space?"
