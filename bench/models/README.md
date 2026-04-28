# bench/models — Model Profiles for the SAGE Wrapper Benchmark

This directory holds the **model profiles** the benchmark harness uses to
launch a target LLM and tell pentagi how to reach it. One YAML per model.

> Author note: this is the W4 deliverable for the `sage-wrapper-v2` PR. The
> harness lives at `bench/runner.py` (W2). The schema these profiles speak
> is codified at `bench/contracts.md` (W2). If anything here disagrees with
> `contracts.md`, `contracts.md` wins — open a PR to align.

## Inference backend: ollama (local) + Anthropic API (ceiling)

The local profiles target **ollama** (already-installed on most dev
machines, Apple-Silicon friendly via Metal, no CUDA required). The hosted
ceiling profile targets the Anthropic API directly. Earlier drafts of this
directory used vLLM compose stacks — that variant is preserved in git
history but isn't part of the shipped benchmark because it requires NVIDIA
GPUs and our reference benchmark machine is an M1 Max.

## How a profile is consumed

A profile has three jobs:

1. **Bring the model up.** `serve_command` is a shell snippet — for ollama
   profiles it just confirms the daemon is reachable and pulls the model
   tag if missing. For Anthropic this is a no-op.
2. **Tell pentagi where the model is.** `pentagi_env` is a map of env vars
   the harness exports into pentagi's environment before launch. We do
   **not** add new pentagi config knobs — only env vars pentagi already
   supports (see `backend/pkg/config/config.go` and `.env.example`).
3. **Tell the harness how to know the model is up.** `smoke_prompt` is a
   trivial completion the harness fires once before real tasks.

The `serve.sh` wrapper handles up/down so the harness doesn't have to know
the YAML layout:

```bash
./bench/models/serve.sh list
./bench/models/serve.sh up   llama3-8b
./bench/models/serve.sh down llama3-8b
```

## Available profiles

| Profile | Backend | GPU | Min unified RAM | Disk | Notes |
|---|---|---|---|---|---|
| `llama3-8b` | ollama | Metal/CUDA via ollama | 16 GB | 5 GB | primary weak baseline |
| `deepseek-r1-8b` | ollama | Metal/CUDA via ollama | 16 GB | 6 GB | reasoning-trace baseline |
| `gemma3-12b` | ollama | Metal/CUDA via ollama | 24 GB | 9 GB | mid-tier capacity sanity check |
| `claude-sonnet-4.5` | Anthropic API | none | none | none | hosted-API ceiling |

`ollama pull` runs once per tag if the weights aren't cached. After that,
ollama keeps them on disk under `~/.ollama/models`.

## Pentagi env-var keys these profiles set

The ollama profiles target pentagi's **custom** provider (the OpenAI-
compatible `LLM_SERVER_*` knobs in `backend/pkg/providers/custom/`). The
Anthropic profile targets pentagi's built-in **anthropic** provider.

| Key | Used by | Purpose |
|---|---|---|
| `LLM_SERVER_URL` | custom provider | OpenAI-compatible base URL — `http://localhost:11434/v1` for ollama |
| `LLM_SERVER_KEY` | custom provider | bearer token (ollama ignores; we send `ollama`) |
| `LLM_SERVER_MODEL` | custom provider | model id, e.g. `llama3:latest`, `gemma3:12b` |
| `LLM_SERVER_PROVIDER` | custom provider | model-name prefix filter (`meta`, `google`, `deepseek`, …) |
| `LLM_SERVER_LEGACY_REASONING` | custom provider | `true` for reasoning models like deepseek-r1 |
| `LLM_SERVER_PRESERVE_REASONING` | custom provider | preserve `<think>` blocks in chain |
| `ANTHROPIC_API_KEY` | anthropic provider | required to enable Anthropic in pentagi |
| `ANTHROPIC_SERVER_URL` | anthropic provider | API base URL |

Setting `LLM_SERVER_URL` together with `LLM_SERVER_MODEL` is what
**activates** the custom provider in pentagi (see
`backend/pkg/providers/providers.go`). Setting `ANTHROPIC_API_KEY` is what
activates the anthropic provider. No further pentagi config changes
needed.

## Bringing a model up

### Local ollama (llama3-8b / deepseek-r1-8b / gemma3-12b)

Prereqs:
- ollama installed and running (`brew install ollama` then `ollama serve`,
  or use the macOS app — it auto-starts as a launchd service).
- Disk: 5–9 GB per model tag.

```bash
./bench/models/serve.sh up llama3-8b
# Pulls the tag if missing; verifies /api/tags is reachable; warms the model.

# When done:
./bench/models/serve.sh down llama3-8b   # no-op for ollama (daemon stays up)
```

### Anthropic (Sonnet 4.5)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
./bench/models/serve.sh up claude-sonnet-4.5   # no-op; just sanity-checks env
```

The harness reads `pentagi_env.ANTHROPIC_API_KEY` (which is the literal
string `${ANTHROPIC_API_KEY}`) and substitutes from the current shell at
launch time.

## Adding a new profile

1. Drop a new `bench/models/<name>.yaml` whose top-level fields match the
   schema in `bench/contracts.md`. Keep `name` equal to the file basename.
2. Pick the right pentagi env vars:
   - **OpenAI-compatible (ollama, vLLM, llama.cpp server):** use the
     `LLM_SERVER_*` keys (custom provider).
   - **OpenAI itself:** use `OPEN_AI_KEY` + `OPEN_AI_SERVER_URL`.
   - **Anthropic:** use `ANTHROPIC_API_KEY` + `ANTHROPIC_SERVER_URL`.
   - Other native providers (Gemini, Bedrock, DeepSeek, GLM, Kimi, Qwen
     DashScope): see `.env.example` for the full list. **Do not invent
     new keys** — pentagi's config struct is fixed.
3. Run the validation snippets below; if everything parses, you're done.

## Validation

```bash
for f in bench/models/*.yaml; do
    python -c "import yaml,sys; yaml.safe_load(open('$f')); print('ok:', '$f')"
done
```

The harness fires `smoke_prompt` against the live endpoint once
`serve.sh up` returns, before running real tasks.

## Files

- `llama3-8b.yaml`
- `deepseek-r1-8b.yaml`
- `gemma3-12b.yaml`
- `claude-sonnet-4.5.yaml` (no serve; hosted API)
- `example.yaml` (schema-shaped placeholder used by the dry-run smoke)
- `serve.sh` — up/down wrapper used by the harness
- `README.md` — this file
