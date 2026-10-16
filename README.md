# Jarvis

<p align="center"><img src="docs/assets/jarvis.svg" width="84" alt="Jarvis"></p>

<p align="center"><b>Coordinating Fast Talk and Slow Work in Full-Duplex Audio and Omni Agents</b></p>

<p align="center">English · <a href="README_zh.md">简体中文</a> · <a href="https://amphionteam.github.io/Jarvis-Demo/">Demos</a></p>

Jarvis connects a full-duplex conversational model to background reasoning, tools and memory. You can keep talking while it works, change a request in progress, stop a reply and return to its result later.

**Jarvis-Audio** supports Realtime-Venus-Audio, MiniCPM-o and NVIDIA VoiceChat. **Jarvis-Omni** adds screen and game-audio input through Venus-Omni. Both use the same task ledger, asynchronous workers, memory and playback control.

![Jarvis architecture](docs/assets/architecture.svg)

## What Jarvis coordinates

- **When to delegate.** The front model keeps the conversation moving. Venus uses a small adapter on its native control tokens to admit background work while its speech weights stay frozen.
- **What the request changes.** JEV selects from operations constructed from the current tasks. Refinements and cancellations name a specific task version.
- **Which results remain useful.** Workers check current context before executing tools; replaced or cancelled task versions cannot deliver late results.
- **What the user heard.** Generation and delivery are separate. Only complete playback receipts confirm delivery; an interruption keeps the result available.
- **What to remember.** Durable preferences and facts persist across sessions. Temporary task instructions stay with their task.
- **Whether visual evidence is current.** Omni aligns media on one clock and checks visual claims before and during playback.

The working tools include calculation, web search, page reading, weather, news, document search, file creation and editing, and personal-memory retrieval. You can register additional tools through the same interface.

## Demos

| Daily conversation | Game interaction | Screen companion |
| --- | --- | --- |
| [![English conversation](docs/assets/audio-daily-en.jpg)](https://amphionteam.github.io/Jarvis-Demo/#demo-audio-daily-en) | [![PvZ](docs/assets/audio-pvz.jpg)](https://amphionteam.github.io/Jarvis-Demo/#demo-audio-pvz) | [![CS2](docs/assets/omni-cs2.jpg)](https://amphionteam.github.io/Jarvis-Demo/#demo-omni-cs2) |
| [English](https://amphionteam.github.io/Jarvis-Demo/#demo-audio-daily-en) · [Chinese](https://amphionteam.github.io/Jarvis-Demo/#demo-audio-daily-zh) | [PvZ](https://amphionteam.github.io/Jarvis-Demo/#demo-audio-pvz) · [VoxeLibre](https://amphionteam.github.io/Jarvis-Demo/#demo-audio-minecraft-1) | [CS2](https://amphionteam.github.io/Jarvis-Demo/#demo-omni-cs2) · [League of Legends](https://amphionteam.github.io/Jarvis-Demo/#demo-omni-lol) |

## Quick start

Use Linux, Python 3.11, an NVIDIA GPU with BF16 support, and a browser with microphone access. The commands below put Venus and ASR on separate GPUs. Model services can also run on another machine; see the [model guide](docs/models.md) for resource planning and endpoints.

### 1. Install the harness

```bash
git clone https://github.com/JensenYX/Jarvis.git
cd Jarvis
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
cp .env.example .env
```

Fill in `WORKER_API_KEY` and `TYPESAFE_API_KEY` in `.env`. The default worker is DeepSeek. `WORKER_BASE_URL` and `WORKER_MODEL` select another compatible Chat Completions endpoint. JEV can use either the TypeSafe API or the [local JEV service](docs/models.md#local-jev).

### 2. Install and download the speech models

```bash
jarvis setup venus
jarvis setup asr
jarvis models download venus asr
```

The setup commands create separate Python environments for model services. The repository already includes the small Audio and Omni adapters. Base models are downloaded on demand.

### 3. Start the model services

Run each command in its own terminal with the harness environment active:

```bash
jarvis models serve venus --gpu 0
```

```bash
jarvis models serve asr --gpu 1
```

Wait for each service to finish loading, then start the application:

```bash
jarvis doctor
jarvis serve
```

Open **http://localhost:8765**, choose a language, and click **开始对话 / Start conversation**. Allow microphone access. You can try:

- “Use a calculator to check 645 times 27.”
- “Create a small HTML game with a restart button.”
- “Change the game to a light theme.”
- “Stop talking.” Then: “Read that result again.”
- “Remember that I prefer concise answers.”

For a remote server, forward the application port to your computer:

```bash
ssh -N -L 8765:127.0.0.1:8765 user@server
```

Open the same localhost address in your local browser. Microphone and screen capture also work when the application is served over HTTPS.

## Choose a frontend

| Frontend | Background-work admission | Start the application |
| --- | --- | --- |
| Realtime-Venus-Audio | Native control-token gate | `jarvis serve --frontend venus` |
| MiniCPM-o | Causal ASR admission, native text prefill for results | `jarvis serve --frontend minicpm` |
| NVIDIA VoiceChat | Native function-call and result channels | `jarvis serve --frontend voicechat` |
| Venus-Omni | Native audio-visual gate and visual evidence | `jarvis serve --mode omni` |

Install and start the matching model service before changing the frontend. [Model installation and configuration →](docs/models.md)

## Jarvis-Omni

Install FFmpeg, then prepare the local models:

```bash
jarvis setup venus
jarvis setup asr
jarvis setup omni
jarvis models download venus-omni asr jev-omni vision
```

Set `GEMINI_API_KEY`, `GEMINI_BASE_URL` and `GEMINI_MODEL` for visual review. Start these services in separate terminals:

```bash
jarvis models serve venus-omni --gpu 0
jarvis models serve asr --gpu 1
jarvis models serve jev-omni --gpu 2
jarvis models serve vision --gpu 3
```

```bash
jarvis doctor --mode omni
jarvis serve --mode omni --game cs2
```

Open **http://localhost:8765**, start the conversation, and select a game window with **选择游戏窗口 / Share game window**. Choose CS2 or League of Legends before sharing. Enable game audio in the browser's sharing dialog when available. The commentary selector controls how often Jarvis speaks proactively.

The [model guide](docs/models.md#visual-readers) explains the included HUD and life-state adapters. [Screen and game setup →](docs/games.md)

## Game integrations

| Game | Interaction | Setup |
| --- | --- | --- |
| PvZ web fan game | Ask for advice, request a move, or hand over and reclaim play | `jarvis setup pvz`, then `jarvis serve --game pvz` |
| Luanti / VoxeLibre | Explore a persistent world, discuss resources and cooperate on tasks | `jarvis setup voxel`, then `jarvis games start voxel` |
| CS2 | Screen-based conversation and tactical advice | `jarvis serve --mode omni --game cs2` |
| League of Legends | Screen-based conversation, visual facts and advice | `jarvis serve --mode omni --game lol` |

The voxel integration uses **Luanti and VoxeLibre**. PvZ and VoxeLibre run through game bridges with explicit action grants and engine receipts. CS2 and League of Legends use screen capture. [Installation, controls and ports →](docs/games.md)

## Configuration and extension

`jarvis serve --config path/to/config.yaml` loads overrides for the packaged defaults. Environment variables configure credentials and endpoints. Runtime data defaults to `~/.local/share/jarvis`; set `JARVIS_DATA_DIR` or `JARVIS_MODEL_DIR` to choose storage locations.

The package is organized around shared responsibilities:

```text
jarvis/
  core/       Tasks, workers, tools, memory and playback
  audio/      Native admission and frontend transports
  omni/       Media timeline, visual claims and screen interaction
  adapters/   Model inference services and native gates
  games/      PvZ and VoxeLibre bridges
  resources/  Prompts, browser client, defaults and small adapters
examples/     Tool registration and configuration examples
```

[Add a tool, connect a frontend, or use the typed decision interface →](docs/extending.md)

## Citation

```bibtex
@techreport{jarvis2026,
  title = {Jarvis: Coordinating Fast Talk and Slow Work in Full-Duplex Audio and Omni Agents},
  author = {{Jarvis Team}},
  year = {2026},
  url = {https://amphionteam.github.io/Jarvis-Demo/}
}
```

## License

Original harness code and released adapters use [Apache-2.0](LICENSE). The VoxeLibre bridge and game patches retain their respective licenses. Downloaded models and games retain their upstream licenses; the PvZ fan game is for personal, noncommercial use. See [third-party components](licenses/THIRD_PARTY.md).
