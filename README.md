# Operational Intelligence — AI-Driven VMware Operations

A working implementation of **Microsoft Copilot Studio** as a conversational AI interface for VMware infrastructure. Ask natural language questions about your vCenter, VCF Operations, and VCF Network Insight environments and get real answers from live data.

📝 **Blog post:** [AI-Driven VMware Operations with Microsoft Copilot Studio](https://bervid.net/p/ai-driven-vmware-operations-with-microsoft-copilot-studio/)

## What's in this repo

| Folder | Description | Port |
|--------|-------------|------|
| `vcenter/` | FastAPI service wrapping pyVmomi for vCenter operations | 8080 |
| `vcfops/` | FastAPI service for VCF Operations (Aria Operations) | 8081 |
| `vcfNetworks/` | FastAPI service for VCF Network Insight | 8082 |
| `swagger/` | Swagger 2.0 specs for Copilot Studio custom connectors | — |

## Architecture

```
User asks natural language question
  → Microsoft Copilot Studio interprets intent
    → Custom Connector (Swagger 2.0) invokes backend
      → On-Premises Data Gateway proxies to local network
        → FastAPI service processes the call
          → pyVmomi / VCF APIs query infrastructure
            → AI summarizes and reasons over results
```

## Operations

77 total operations across three connectors:

- **vCenter (30):** VM lifecycle, host management, clusters, datastores, snapshots, vMotion, storage vMotion, alarms, events, tasks
- **VCF Operations (29):** Alerts, symptoms, notifications, compliance, health, capacity
- **VCF Network Insight (18):** Entity search, NSX segments, Tier-1 routers, alerts, infrastructure nodes

## Prerequisites

- Python 3.12+
- VMware vCenter with pyVmomi access
- VCF Operations for Networks (optional)
- VCF Operations / Aria Operations (optional)
- Microsoft Copilot Studio license
- Microsoft On-Premises Data Gateway
- Windows Server for the gateway/API host

## Quick Start

1. Clone this repo to your gateway server
2. Install dependencies: `pip install fastapi uvicorn pyvmomi requests`
3. Configure environment variables for vCenter credentials
4. Start services:
   ```bash
   uvicorn vcenter.vcenter_api:app --host 0.0.0.0 --port 8080
   uvicorn vcfops.vcf_ops_api:app --host 0.0.0.0 --port 8081
   uvicorn vcfNetworks.vcf_networks_api:app --host 0.0.0.0 --port 8082
   ```
5. Import Swagger specs from `swagger/` into Copilot Studio custom connectors
6. Enable actions in your Copilot Studio agent

## Key Findings

- **Use Swagger 2.0** — OpenAPI 3.x causes parsing issues in Copilot Studio
- **Use `localhost`** in connector host config — not the machine hostname
- **Keep responses flat** — the AI reasons better over simple JSON structures
- **Enable actions one by one** — there's no bulk-enable in Copilot Studio (77 clicks!)

## License

MIT
