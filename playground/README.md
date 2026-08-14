# Local LLM Playground (Mac)

A tiny web UI for trying different local LLMs on your Mac and comparing their
performance. No lab / VMware dependency — it talks straight to [Ollama](https://ollama.com).

Pick a model, chat, and see how fast each one runs (tokens/sec, load time,
generate time). Great for deciding which model to later wire into the
`orchestrator/` for VMware tool-calling.

```
Your Mac
┌───────────────────────────────────────────────┐
│  Browser  →  playground.py :8095  →  Ollama :11434 │
│             (model picker + metrics)               │
└───────────────────────────────────────────────┘
```

## 1. Install Ollama

```bash
brew install ollama
```

Start the Ollama server (leave it running in its own terminal, or use the menu-bar app):

```bash
ollama serve
```

## 2. Pull a few models to compare

On Apple Silicon these are good starting points (tool-calling capable):

```bash
ollama pull llama3.2          # 3B  — fastest
ollama pull qwen2.5:7b        # 7B  — strong tool calling for its size
ollama pull llama3.1:8b       # 8B  — solid all-rounder
ollama pull hermes3           # 8B  — tuned for tool calling
```

Rule of thumb for memory: a Q4 model needs roughly its parameter count in GB of
RAM (an 8B model ≈ 5–6 GB). Stick to models that fit comfortably in your Mac's
unified memory.

## 3. Run the playground

```bash
cd playground
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 playground.py
```

Open http://localhost:8095. The dropdown auto-fills with whatever you've pulled —
click **⟳** to refresh after pulling more.

## Configuration

| Env var           | Default                  | Purpose                     |
|-------------------|--------------------------|-----------------------------|
| `OLLAMA_URL`      | `http://localhost:11434` | Where Ollama is listening   |
| `PLAYGROUND_PORT` | `8095`                   | Port for the web UI         |

## What the metrics mean

- **tok/s** — output tokens per second (the main "speed" number)
- **total** — wall-clock time for the whole request
- **load** — time Ollama spent loading the model into memory (0s once warm)
- **gen** — time spent actually generating the answer
- **out / in tok** — output vs. prompt token counts

## Next step

Once you've picked a favourite model, the `../orchestrator/` uses the same Ollama
backend to add VMware tool-calling (vCenter / VCF Ops / Networks) when you're
ready to connect it to your lab.
