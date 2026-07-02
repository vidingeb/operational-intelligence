# Swagger / OpenAPI Specs

Custom connector definitions for Microsoft Copilot Studio.

## Files

- `vcenter.json` — vCenter API connector (Swagger 2.0)
- `vcf_ops.json` — VCF Operations API connector (Swagger 2.0)
- `vcf_networks.json` — VCF Network Insight API connector (Swagger 2.0)

## Usage

1. Edit the spec here (single source of truth)
2. Push to GitHub
3. In Power Platform → Custom Connectors → Edit → Swagger tab → paste updated spec

> **Note:** Use Swagger 2.0 format. OpenAPI 3.x causes parsing issues in Copilot Studio.
