# Portable mode

Runs the whole assistant on one machine - local model, local APIs, local UI.
Nothing leaves the laptop.

The orchestrator takes its endpoints from the environment, so the same code
serves three deployments:

| | Inference | APIs |
|---|---|---|
| Demo | GB10 over Tailscale | work estate |
| Customer site | this laptop | customer's estate |
| Offline | this laptop | recorded fixtures |

## Offline

    ollama serve
    ollama pull gpt-oss:20b
    ./run-local.sh

Serves recorded responses on 8080/8081/8082, so it works with no network.

## Against a real estate

    ./run-local.sh live http://their-api-host

Needs the three API wrappers running somewhere that can see their vCenter.

## Recording fixtures

On a host that can reach the real APIs - the orchestrator VM:

    python3 capture.py                        # -> fixtures-raw/
    python3 sanitise.py fixtures-raw fixtures

`capture.py` reads the tool registry rather than a hand-written list, so it
records exactly what the orchestrator can call. `sanitise.py` maps real
hostnames and addresses to `example.lab` and TEST-NET-2 addresses, keeping the
mapping consistent so relationships in the data still make sense.

**Do not commit `fixtures-raw/`** - it contains real estate data.

## Model sizing

`gpt-oss:120b` needs about 65 GB and will not fit on a 48 GB laptop. Use
`gpt-oss:20b` locally. Tool calling behaves the same; reasoning depth does not.
