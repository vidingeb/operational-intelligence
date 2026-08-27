#!/usr/bin/env python3
"""Replace real estate identifiers in captured fixtures with generic ones.

Fixtures are recorded from a live vCenter/Operations/Network Insight estate,
so they carry real hostnames, addresses and VM names. Anything shown to a
customer - or committed anywhere - should be scrubbed first. The mapping is
deterministic so relationships survive: the same host is always the same
replacement, and a flow between two VMs still connects the same pair.
"""
import json
import re
import sys
from pathlib import Path

DOMAIN = "vcf.local"
NEW_DOMAIN = "example.lab"

# Ordered: more specific patterns first
HOST_RE = re.compile(r"\b(esx\d+)\.%s\b" % re.escape(DOMAIN), re.I)
VC_RE = re.compile(r"\b(vc\d+)\.%s\b" % re.escape(DOMAIN), re.I)
GENERIC_FQDN_RE = re.compile(r"\b([a-z0-9][a-z0-9-]*)\.%s\b" % re.escape(DOMAIN), re.I)
IP_RE = re.compile(r"\b(?:10\.0\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b")

_ip_map: dict = {}


def map_ip(match: re.Match) -> str:
    """Map each real address to a stable 198.51.100.x address (TEST-NET-2)."""
    original = match.group(0)
    if original not in _ip_map:
        _ip_map[original] = f"198.51.100.{len(_ip_map) + 10}"
    return _ip_map[original]


def scrub(text: str) -> str:
    text = HOST_RE.sub(lambda m: f"{m.group(1).lower()}.{NEW_DOMAIN}", text)
    text = VC_RE.sub(lambda m: f"{m.group(1).lower()}.{NEW_DOMAIN}", text)
    text = GENERIC_FQDN_RE.sub(lambda m: f"{m.group(1).lower()}.{NEW_DOMAIN}", text)
    text = IP_RE.sub(map_ip, text)
    return text


def main(src: Path, dst: Path) -> int:
    dst.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted(src.glob("*.json")):
        raw = path.read_text(encoding="utf-8", errors="replace")
        cleaned = scrub(raw)
        try:
            json.loads(cleaned)
        except json.JSONDecodeError as exc:
            print(f"  SKIP {path.name}: sanitising broke the JSON ({exc})")
            continue
        (dst / path.name).write_text(cleaned, encoding="utf-8")
        changed = "changed" if cleaned != raw else "no change"
        print(f"  {path.name:<45} {changed}")
        count += 1

    print(f"\n{count} fixtures written to {dst}")
    if _ip_map:
        print("address mapping:")
        for real, fake in _ip_map.items():
            print(f"  {real:<16} -> {fake}")
    return 0


if __name__ == "__main__":
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "fixtures-raw")
    dst = Path(sys.argv[2] if len(sys.argv) > 2 else "fixtures")
    sys.exit(main(src, dst))
