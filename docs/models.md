# Models and configuration

[English](models.md) · [简体中文](models_zh.md) · [README](../README.md)

## Deployment layout

The harness runs on the CPU. Speech recognition and each local model run as separate services, each with its own Python environment. Start one conversational frontend per application instance.

| Service | Setup environment | Default port | Model |
| --- | --- | --- | --- |
| `venus` | `venus` | 8766 | Realtime-Venus-Audio |
| `minicpm` | `minicpm` | 8766 | MiniCPM-o |
| `voicechat` | `voicechat` | 8766 | NVIDIA NemotronLabs VoiceChat |
| `venus-omni` | `venus` | 8766 | Realtime-Venus-Omni |
| `asr` | `asr` | 8767 | Qwen3-ASR-1.7B |
| `jev-omni` | `omni` | 8810 | JEV-Omni |
| `vision` | `omni` | 8811 | Gemma visual description model |
| `hud` | `omni` | 8812 | JEV-Omni with the HUD adapter |
| `life` | `omni` | 8813 | JEV-Omni with the life-state adapter |

Use Python 3.11 for the harness and the Venus, MiniCPM, ASR and Omni environments. VoiceChat uses Python 3.12 and a CUDA toolkit for its compiled extensions. On Ubuntu, install the common system dependencies with:

```bash
sudo apt-get install python3-venv ffmpeg libsndfile1 git build-essential
```

The model services were exercised on NVIDIA H800 GPUs. In these sessions, Venus and MiniCPM used roughly 20–24 GiB, VoiceChat about 29 GiB, and each Omni judgment or vision service about 24 GiB. ASR reserves a fraction of its GPU for the inference cache. Allow additional memory for longer inputs; using a separate 40 GiB or larger GPU for each active model is a practical starting configuration. The small harness process needs no GPU.

Model weights, environments and game installations default to `~/.local/share/jarvis`. Set these variables before setup and startup to use another disk:

```bash
export JARVIS_DATA_DIR=/path/to/jarvis-data
export JARVIS_MODEL_DIR=/path/to/models
```

`jarvis setup` installs model dependencies; `jarvis models download` obtains the corresponding checkpoint. The model catalog and dependency files live inside the package. Re-run setup after updating Jarvis to refresh the model service code.

## Audio frontends

Install ASR once, then choose a frontend. Run the model and ASR commands in separate terminals.

### Venus-Audio

```bash
jarvis setup venus
jarvis setup asr
jarvis models download venus asr
jarvis models serve venus --gpu 0
```

```bash
jarvis models serve asr --gpu 1
```

```bash
jarvis serve --frontend venus
```

The general and game gates are included. A connected PvZ or VoxeLibre session selects the game gate; ordinary conversation uses the general gate.

### MiniCPM-o

```bash
jarvis setup minicpm
jarvis models download minicpm
jarvis models serve minicpm --gpu 0
```

Keep ASR running, then start `jarvis serve --frontend minicpm`. MiniCPM uses a causal transcript admission controller. Accepted requests go through the shared JEV and task runtime; completed results enter MiniCPM through its native text-prefill interface.

### NVIDIA VoiceChat

Install Python 3.12 and the CUDA toolkit, then run:

```bash
jarvis setup voicechat --python python3.12
jarvis models download voicechat
jarvis models serve voicechat --gpu 0
```

Keep ASR running, then start `jarvis serve --frontend voicechat`. Setup installs the NVIDIA NeMo Speech runtime and native CUDA extensions. The frontend emits native function intents; Jarvis validates and executes them and returns the result through the pending function call. ASR supplies conversation and memory observations.

## Local JEV

Text coordination defaults to the TypeSafe API. To run JEV locally, install and start JEV-Omni:

```bash
jarvis setup omni
jarvis models download jev-omni
jarvis models serve jev-omni --gpu 2
```

Set the following in `.env`:

```dotenv
JEV_BACKEND=local
JEV_ENDPOINT=http://127.0.0.1:8810
```

Start the application normally. The local service supplies Choice distributions, yes probabilities and ordinal scores through the same controller interface. `TYPESAFE_API_KEY` is needed only for `JEV_BACKEND=typesafe`.

## Omni services

Omni uses Venus-Omni for speech, Qwen3-ASR for microphone transcription, JEV-Omni for multimodal judgments, and a local Gemma service for visual descriptions. A separately configured Gemini endpoint reviews uncertain visual proposals.

```bash
jarvis setup venus
jarvis setup asr
jarvis setup omni
jarvis models download venus-omni asr jev-omni vision
```

Run each service in its own terminal:

```bash
jarvis models serve venus-omni --gpu 0
jarvis models serve asr --gpu 1
jarvis models serve jev-omni --gpu 2
jarvis models serve vision --gpu 3
```

Configure `GEMINI_API_KEY`, `GEMINI_BASE_URL` and `GEMINI_MODEL`, then run:

```bash
jarvis doctor --mode omni
jarvis serve --mode omni --game cs2
```

### Visual readers

Two additional inference-only adapters are included: `hud` reads visible game facts, and `life` classifies the HUD-controlled character's life state. Each service loads its own JEV-Omni instance.

```bash
jarvis models serve hud --gpu 4
jarvis models serve life --gpu 5
```

```bash
jarvis serve --config examples/omni_readers.yaml --game lol
```

The HUD estimates enter the visual proposal and review path. The life-state reader contributes observations from the same frame; neither changes its capture timestamp or extends its validity period.

## Credentials and endpoints

The CLI reads `.env` from the directory where it is launched. Existing process environment variables take precedence. Keys stay on the server.

| Variable | Purpose |
| --- | --- |
| `WORKER_API_KEY` | Background-model credential |
| `WORKER_BASE_URL`, `WORKER_MODEL` | Chat Completions endpoint and model ID |
| `TYPESAFE_API_KEY`, `JEV_MODEL` | Hosted JEV credential and model |
| `JEV_BACKEND`, `JEV_ENDPOINT` | Select `typesafe` or `local` coordination |
| `FRONTEND_ENDPOINT` | Conversational model WebSocket, including `/ws` |
| `ASR_ENDPOINT` | ASR WebSocket, including `/ws` |
| `JEV_OMNI_ENDPOINT` | Multimodal decision HTTP service |
| `VISION_ENDPOINT` | Visual-description HTTP service |
| `GEMINI_API_KEY`, `GEMINI_BASE_URL`, `GEMINI_MODEL` | Visual review service |
| `HF_TOKEN` | Hugging Face access when required by a selected model |

To use existing model files, pass `--model-dir /path/to/checkpoint` to `jarvis models serve`. To run on another port, pass `--port` and update the corresponding endpoint. Model services bind to localhost by default; use SSH forwarding to connect services across machines.

## Checking a deployment

`jarvis doctor` checks required variables, packaged resources and service health. Use the same `--mode`, `--frontend`, `--game` and `--config` as the application. Each model service's `/health` becomes ready after initialization.

After starting the browser session, ask for a calculation and confirm that a result card appears and the answer plays. Use “stop talking” during a reply, then request the result again. This exercises input, delegation, tools, playback and delivery in one short session.
