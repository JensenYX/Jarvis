# 模型与配置

[English](models.md) · [简体中文](models_zh.md) · [README](../README_zh.md)

## 部署结构

harness 运行在 CPU 上。语音识别和各个本地模型分别作为独立服务运行，使用独立 Python 环境。每个应用实例连接一个对话前端。

| 服务 | 安装环境 | 默认端口 | 模型 |
| --- | --- | --- | --- |
| `venus` | `venus` | 8766 | Realtime-Venus-Audio |
| `minicpm` | `minicpm` | 8766 | MiniCPM-o |
| `voicechat` | `voicechat` | 8766 | NVIDIA NemotronLabs VoiceChat |
| `venus-omni` | `venus` | 8766 | Realtime-Venus-Omni |
| `asr` | `asr` | 8767 | Qwen3-ASR-1.7B |
| `jev-omni` | `omni` | 8810 | JEV-Omni |
| `vision` | `omni` | 8811 | Gemma 视觉描述模型 |
| `hud` | `omni` | 8812 | JEV-Omni 与 HUD 适配器 |
| `life` | `omni` | 8813 | JEV-Omni 与生命状态适配器 |

harness、Venus、MiniCPM、ASR 和 Omni 环境使用 Python 3.11。VoiceChat 使用 Python 3.12，并需要 CUDA toolkit 编译扩展。Ubuntu 的通用系统依赖可通过以下命令安装：

```bash
sudo apt-get install python3-venv ffmpeg libsndfile1 git build-essential
```

模型服务在 NVIDIA H800 上进行了运行验证。验证会话中，Venus 和 MiniCPM 约使用 20–24 GiB 显存，VoiceChat 约 29 GiB，每个 Omni 判断或视觉服务约 24 GiB。ASR 会为推理缓存预留所在 GPU 的部分显存。更长输入需要额外空间；初次部署可以为每个运行中的模型分配一张 40 GiB 或更大的 GPU。harness 进程不需要 GPU。

模型、环境和游戏默认安装在 `~/.local/share/jarvis`。在安装和启动前设置以下变量，可以使用其他磁盘：

```bash
export JARVIS_DATA_DIR=/path/to/jarvis-data
export JARVIS_MODEL_DIR=/path/to/models
```

`jarvis setup` 安装模型依赖，`jarvis models download` 下载对应权重。模型清单和依赖文件随包分发。更新 Jarvis 后重新执行 setup，可同步模型服务代码。

## 语音前端

ASR 只需安装一次，再选择一个前端。模型服务与 ASR 分别在独立终端中运行。

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

仓库包含通用门控和游戏门控。连接 PvZ 或 VoxeLibre 会话时使用游戏门控，普通对话使用通用门控。

### MiniCPM-o

```bash
jarvis setup minicpm
jarvis models download minicpm
jarvis models serve minicpm --gpu 0
```

保持 ASR 运行，然后执行 `jarvis serve --frontend minicpm`。MiniCPM 根据因果转写进行准入判断；被接受的请求进入共享 JEV 和任务运行时，完成结果通过原生文本预填充接口回传。

### NVIDIA VoiceChat

安装 Python 3.12 和 CUDA toolkit 后运行：

```bash
jarvis setup voicechat --python python3.12
jarvis models download voicechat
jarvis models serve voicechat --gpu 0
```

保持 ASR 运行，再执行 `jarvis serve --frontend voicechat`。安装过程会安装 NVIDIA NeMo Speech 和原生 CUDA 扩展。前端通过原生函数通道提出请求，Jarvis 验证并执行后，通过对应的待处理函数调用返回结果。ASR 提供对话和记忆观察。

## 本地 JEV

文本协调默认使用 TypeSafe API。也可以安装并启动本地 JEV-Omni：

```bash
jarvis setup omni
jarvis models download jev-omni
jarvis models serve jev-omni --gpu 2
```

在 `.env` 中设置：

```dotenv
JEV_BACKEND=local
JEV_ENDPOINT=http://127.0.0.1:8810
```

随后正常启动应用。本地服务通过相同控制器接口提供 Choice 分布、肯定概率和有序评分。只有 `JEV_BACKEND=typesafe` 时需要 `TYPESAFE_API_KEY`。

## Omni 服务

Omni 使用 Venus-Omni 进行语音交互、Qwen3-ASR 转写麦克风、JEV-Omni 进行多模态判断，以及本地 Gemma 服务生成视觉描述。单独配置的 Gemini 服务用于复核不确定的视觉提案。

```bash
jarvis setup venus
jarvis setup asr
jarvis setup omni
jarvis models download venus-omni asr jev-omni vision
```

在不同终端中启动各服务：

```bash
jarvis models serve venus-omni --gpu 0
jarvis models serve asr --gpu 1
jarvis models serve jev-omni --gpu 2
jarvis models serve vision --gpu 3
```

配置 `GEMINI_API_KEY`、`GEMINI_BASE_URL` 和 `GEMINI_MODEL` 后运行：

```bash
jarvis doctor --mode omni
jarvis serve --mode omni --game cs2
```

### 视觉读取适配器

仓库还包含两个推理适配器：`hud` 读取可见游戏事实，`life` 判断 HUD 对应角色的生命状态。每个服务分别加载一个 JEV-Omni 实例。

```bash
jarvis models serve hud --gpu 4
jarvis models serve life --gpu 5
```

```bash
jarvis serve --config examples/omni_readers.yaml --game lol
```

HUD 估计进入视觉提案与复核流程。生命状态读取器提供同一帧中的观察；两者都保留原始捕获时间，不延长证据有效期。

## 凭据与服务地址

CLI 从启动目录读取 `.env`；已有进程环境变量优先。密钥保留在服务端。

| 变量 | 用途 |
| --- | --- |
| `WORKER_API_KEY` | 后台模型凭据 |
| `WORKER_BASE_URL`、`WORKER_MODEL` | Chat Completions 地址和模型 ID |
| `TYPESAFE_API_KEY`、`JEV_MODEL` | 托管 JEV 的凭据和模型 |
| `JEV_BACKEND`、`JEV_ENDPOINT` | 选择 `typesafe` 或 `local` 协调后端 |
| `FRONTEND_ENDPOINT` | 对话模型 WebSocket，包含 `/ws` |
| `ASR_ENDPOINT` | ASR WebSocket，包含 `/ws` |
| `JEV_OMNI_ENDPOINT` | 多模态判断 HTTP 服务 |
| `VISION_ENDPOINT` | 视觉描述 HTTP 服务 |
| `GEMINI_API_KEY`、`GEMINI_BASE_URL`、`GEMINI_MODEL` | 视觉复核服务 |
| `HF_TOKEN` | 所选模型需要的 Hugging Face 访问凭据 |

使用已有模型文件时，为 `jarvis models serve` 传入 `--model-dir /path/to/checkpoint`。更换端口时传入 `--port`，并更新对应 endpoint。模型服务默认监听 localhost，跨机器连接可使用 SSH 转发。

## 检查部署

`jarvis doctor` 检查必要环境变量、包内资源和服务健康状态。使用与应用一致的 `--mode`、`--frontend`、`--game` 和 `--config`。各模型完成初始化后，`/health` 返回就绪状态。

浏览器连接后请求一次计算，确认结果卡片出现且答案正常播放。在播报途中说“停止说话”，然后再次请求播放结果，即可依次检查输入、委派、工具、播放和交付。
