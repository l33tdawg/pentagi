# bench/models — Model Profiles for the SAGE Wrapper Benchmark

This directory holds the **model profiles** the benchmark harness uses to
launch a target LLM and tell pentagi how to reach it. One YAML per model.

> Author note: this is the W4 deliverable for the `sage-wrapper-v2` PR. The
> harness lives at `bench/runner.py` (W2). The schema these profiles speak
> is codified at `bench/contracts.md` (W2). If anything here disagrees with
> `contracts.md`, `contracts.md` wins — open a PR to align.

## How a profile is consumed

A profile has three jobs:

1. **Bring the model up.** `serve_command` is a shell snippet (usually a
   `docker compose` invocation) that the harness executes. For hosted APIs
   like Anthropic this is empty — there's nothing to launch.
2. **Tell pentagi where the model is.** `pentagi_env` is a map of env vars
   that get exported into pentagi's environment before the harness starts
   it. We do **not** add new pentagi config knobs — we only set env vars
   that pentagi already supports (see `backend/pkg/config/config.go` and
   `.env.example`).
3. **Tell the harness how to know the model is up.** `smoke_prompt` is a
   trivial completion the harness fires once before kicking off real tasks.

The `serve.sh` wrapper handles up/down so the harness doesn't have to know
the YAML layout:

```bash
./bench/models/serve.sh list
./bench/models/serve.sh up   qwen3.5-7b-instruct
./bench/models/serve.sh down qwen3.5-7b-instruct
```

## Available profiles

| Profile | Provider | GPU | Min VRAM | HF auth | Notes |
|---|---|---|---|---|---|
| `qwen3.5-7b-instruct` | vLLM (custom) | yes | 20 GB (1x) | no | primary weak baseline |
| `llama-3.1-8b-instruct` | vLLM (custom) | yes | 24 GB (1x) | **yes** | second weak baseline, gated repo |
| `qwen3.5-32b-instruct` | vLLM (custom) | yes | 80 GB (2x preferred) | no | mid-tier sanity check |
| `claude-sonnet-4.5` | Anthropic | no | N/A | no | hosted-API ceiling |

Disk: each vLLM profile pulls 15–80 GB of weights into the HF cache on
first run. The cache is mounted to `${HF_HOME:-./.hf-cache}` on the host so
subsequent runs don't re-download.

## Pentagi env-var keys these profiles set

The vLLM profiles target pentagi's **custom** provider (the OpenAI-compatible
`LLM_SERVER_*` knobs in `backend/pkg/providers/custom/`). The Anthropic
profile targets pentagi's built-in **anthropic** provider.

| Key | Used by | Purpose |
|---|---|---|
| `LLM_SERVER_URL` | custom provider | OpenAI-compatible base URL (e.g. `http://localhost:8000/v1`) |
| `LLM_SERVER_KEY` | custom provider | bearer token (vLLM ignores; we send `dummy`) |
| `LLM_SERVER_MODEL` | custom provider | model id, e.g. `Qwen/Qwen3.5-7B-Instruct` |
| `LLM_SERVER_PROVIDER` | custom provider | model-name prefix filter (`qwen`, `meta`, ...) |
| `ANTHROPIC_API_KEY` | anthropic provider | required to enable Anthropic in pentagi |
| `ANTHROPIC_SERVER_URL` | anthropic provider | API base URL |

Setting `LLM_SERVER_URL` together with `LLM_SERVER_MODEL` is what
**activates** the custom provider in pentagi (see
`backend/pkg/providers/providers.go`). Setting `ANTHROPIC_API_KEY` is what
activates the anthropic provider. No further pentagi config changes are
needed.

## Bringing a model up

### Local vLLM (Qwen 7B / Llama 8B / Qwen 32B)

Prereqs:
- Docker with the NVIDIA Container Toolkit installed.
- Enough GPU(s) per the table above.
- For Llama-3.1: a HuggingFace token with access to the gated repo,
  exported as `HF_TOKEN`.

```bash
# Optional: pin a HF cache dir (default: ./.hf-cache next to compose file).
export HF_HOME=/srv/hf-cache

# Llama only: provide HF token for gated repo.
export HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxx

./bench/models/serve.sh up qwen3.5-7b-instruct
# wait — the first run downloads weights; healthcheck sleeps until /v1/models is 200.

# When done:
./bench/models/serve.sh down qwen3.5-7b-instruct
```

The compose files include a healthcheck against `/v1/models`, so
`docker compose ... up -d --wait` blocks until vLLM is actually serving.

### Anthropic (Sonnet 4.5)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
./bench/models/serve.sh up claude-sonnet-4.5   # no-op; just sanity-checks env
```

The harness reads `pentagi_env.ANTHROPIC_API_KEY` (which is the literal
string `${ANTHROPIC_API_KEY}`) and substitutes it from the current shell at
launch time.

## Adding a new profile

1. Drop a new `bench/models/<name>.yaml` whose top-level fields match the
   schema in `bench/contracts.md`. Keep `name` equal to the file basename.
2. If your model is locally served, add a sibling
   `bench/models/<name>.compose.yaml` and reference it from `serve_command`
   / `teardown_command`. Use the existing files as templates — keep the
   `vllm/vllm-openai` image, the `${HF_HOME:-./.hf-cache}` mount, and a
   healthcheck that polls `/v1/models`.
3. Pick the right pentagi env vars:
   - **OpenAI-compatible (vLLM, llama.cpp server, ollama-as-OpenAI):** use
     the `LLM_SERVER_*` keys (custom provider).
   - **OpenAI itself:** use `OPEN_AI_KEY` + `OPEN_AI_SERVER_URL`.
   - **Anthropic:** use `ANTHROPIC_API_KEY` + `ANTHROPIC_SERVER_URL`.
   - Other native providers (Gemini, Bedrock, DeepSeek, GLM, Kimi, Qwen
     DashScope, Ollama): see `.env.example` in the repo root for the full
     list. **Do not invent new keys** — pentagi's config struct is fixed.
4. Run the validation snippets below; if everything parses, you're done.

## Validation

These are the smoke checks W4 ran locally; rerun them whenever you touch a
profile:

```bash
# YAML well-formedness for every profile.
for f in bench/models/*.yaml; do
    python -c "import yaml,sys; yaml.safe_load(open('$f')); print('ok:', '$f')"
done

# Compose well-formedness for every vLLM compose file.
for f in bench/models/*.compose.yaml; do
    docker compose -f "$f" config >/dev/null && echo "ok: $f"
done
```

The harness will additionally fire `smoke_prompt` against the live endpoint
once `serve.sh up` returns, before running real tasks.

## Files

- `qwen3.5-7b-instruct.yaml` + `qwen3.5-7b-vllm.compose.yaml`
- `llama-3.1-8b-instruct.yaml` + `llama-3.1-8b-vllm.compose.yaml`
- `qwen3.5-32b-instruct.yaml` + `qwen3.5-32b-vllm.compose.yaml`
- `claude-sonnet-4.5.yaml` (no compose; hosted API)
- `serve.sh` — up/down wrapper used by the harness
- `README.md` — this file
