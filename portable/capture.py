#!/usr/bin/env python3
"""Record live API responses as fixtures for offline replay.

Run this on a host that can reach the real APIs - the orchestrator VM. It
imports the tool registry rather than taking a hand-written list of paths, so
whatever the orchestrator can call is what gets recorded, and the two cannot
drift apart.

Only read-only GET tools with no required parameters are captured; write
tools and per-resource lookups are skipped by design.

    python3 capture.py                  # writes fixtures-raw/
    python3 sanitise.py fixtures-raw fixtures
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestrator"))

# Extra endpoints worth recording that take parameters, and so are not
# captured automatically.
EXTRA = [
    (8082, "/ni/flows/recent?hours=24&size=20"),
    (8082, "/ni/health"),
]

TIMEOUT = 30


def fixture_name(port: int, path: str) -> str:
    cleaned = path.strip("/").replace("/", "_").replace("?", "_")
    cleaned = cleaned.replace("&", "_").replace("=", "_")
    return f"{port}-{cleaned}"


def fetch(url: str):
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            return resp.read(), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except Exception as exc:                      # noqa: BLE001 - report anything
        return None, str(exc)


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "fixtures-raw")
    out.mkdir(parents=True, exist_ok=True)

    try:
        import orchestrator as orch
    except Exception as exc:                      # noqa: BLE001
        print(f"Could not import orchestrator.py: {exc}")
        return 1

    targets = []
    for name, spec in orch.TOOL_SPECS.items():
        if spec.get("method") != "GET" or spec.get("write"):
            continue
        url = spec["url"]
        if "{" in url:                            # needs a resource id
            continue
        targets.append((name, url))

    for port, path in EXTRA:
        targets.append((f"extra{port}{path}", f"{orch.MCP_SERVER}:{port}{path}"))

    print(f"Recording {len(targets)} endpoints to {out}/\n")
    ok = failed = 0
    for name, url in sorted(targets, key=lambda t: t[1]):
        rest = url.split("://", 1)[1]
        hostport, _, path = rest.partition("/")
        port = int(hostport.rsplit(":", 1)[-1])
        body, err = fetch(url)

        if err:
            print(f"  {'SKIP':<6} {name:<34} {err}")
            failed += 1
            continue
        try:
            json.loads(body)
        except json.JSONDecodeError:
            print(f"  {'SKIP':<6} {name:<34} response was not JSON")
            failed += 1
            continue

        target = out / f"{fixture_name(port, '/' + path)}.json"
        target.write_bytes(body)
        print(f"  {'OK':<6} {name:<34} {len(body):>8} bytes -> {target.name}")
        ok += 1

    print(f"\n{ok} recorded, {failed} skipped")
    print(f"Next: python3 sanitise.py {out} fixtures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
