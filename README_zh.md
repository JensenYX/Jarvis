# Jarvis

<p align="center"><img src="docs/assets/jarvis.svg" width="84" alt="Jarvis"></p>

<p align="center"><b>在全双工语音与多模态智能体中协调实时对话与后台任务</b></p>

<p align="center"><a href="README.md">English</a> · 简体中文 · <a href="https://amphionteam.github.io/Jarvis-Demo/">演示页面</a></p>

Jarvis 为全双工对话模型接入后台推理、工具和记忆。任务运行时，你可以继续交谈、修改要求、打断播报，并在之后重新听取结果。

**Jarvis-Audio** 支持 Realtime-Venus-Audio、MiniCPM-o 和 NVIDIA VoiceChat。**Jarvis-Omni** 通过 Venus-Omni 接收屏幕和游戏音频。两者共享任务账本、异步 worker、记忆与播放控制。

![Jarvis 架构](docs/assets/architecture.svg)

## Jarvis 协调什么

- **何时委派。** 前端模型持续对话；Venus 在原生控制 token 上使用小型适配器判断何时启动后台工作，语音生成权重保持冻结。
- **请求改变哪项工作。** JEV 从运行时根据当前任务构造的选项中选择操作。修改和取消都绑定具体任务版本。
- **哪些结果仍然有效。** worker 在执行工具前检查当前上下文；已被替换或取消的版本不能交付迟到结果。
- **用户听到了什么。** 生成结束和交付完成分别记录。完整播放回执才能确认交付；被打断的结果仍可再次播放。
- **哪些信息值得记住。** 长期偏好和事实跨会话保存，临时要求留在对应任务中。
- **画面证据是否仍然有效。** Omni 将媒体对齐到统一时钟，并在播报前及播放期间检查视觉事实。

现有工具包括计算、网页搜索与读取、天气、新闻、文档检索、文件创建与修改，以及个人记忆检索。你可以通过相同接口注册自己的工具。

## 演示

| 日常对话 | 游戏协作 | 屏幕陪伴 |
| --- | --- | --- |
| [![中文对话](docs/assets/audio-daily-zh.jpg)](https://amphionteam.github.io/Jarvis-Demo/#demo-audio-daily-zh) | [![PvZ](docs/assets/audio-pvz.jpg)](https://amphionteam.github.io/Jarvis-Demo/#demo-audio-pvz) | [![CS2](docs/assets/omni-cs2.jpg)](https://amphionteam.github.io/Jarvis-Demo/#demo-omni-cs2) |
| [中文](https://amphionteam.github.io/Jarvis-Demo/#demo-audio-daily-zh) · [英文](https://amphionteam.github.io/Jarvis-Demo/#demo-audio-daily-en) | [PvZ](https://amphionteam.github.io/Jarvis-Demo/#demo-audio-pvz) · [VoxeLibre](https://amphionteam.github.io/Jarvis-Demo/#demo-audio-minecraft-1) | [CS2](https://amphionteam.github.io/Jarvis-Demo/#demo-omni-cs2) · [英雄联盟](https://amphionteam.github.io/Jarvis-Demo/#demo-omni-lol) |

## 快速开始

使用 Linux、Python 3.11、支持 BF16 的 NVIDIA GPU，以及支持麦克风的浏览器。下面将 Venus 和 ASR 分别放在两张 GPU 上；模型服务也可以运行在其他机器，资源规划和地址配置见[模型指南](docs/models_zh.md)。

### 1. 安装 harness

```bash
git clone https://github.com/JensenYX/Jarvis.git
cd Jarvis
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
cp .env.example .env
```

在 `.env` 中填写 `WORKER_API_KEY` 和 `TYPESAFE_API_KEY`。默认 worker 使用 DeepSeek；通过 `WORKER_BASE_URL` 和 `WORKER_MODEL` 可以连接其他兼容 Chat Completions 的服务。JEV 可以使用 TypeSafe API，也可以使用[本地 JEV 服务](docs/models_zh.md#本地-jev)。

### 2. 安装并下载语音模型

```bash
jarvis setup venus
jarvis setup asr
jarvis models download venus asr
```

安装命令会为模型服务创建独立 Python 环境。仓库已经包含 Audio 和 Omni 所需的小型适配器，基础模型按需下载。

### 3. 启动模型服务

在不同终端中激活 harness 环境，分别运行：

```bash
jarvis models serve venus --gpu 0
```

```bash
jarvis models serve asr --gpu 1
```

等待模型加载完成，再启动应用：

```bash
jarvis doctor
jarvis serve
```

打开 **http://localhost:8765**，选择语言，点击“开始对话”并允许麦克风访问。可以尝试：

- “用计算器核对六百四十五乘以二十七。”
- “创建一个有重新开始按钮的 HTML 小游戏。”
- “把刚才的游戏改成浅色主题。”
- “先别说了。”然后说：“再读一遍刚才的结果。”
- “记住，我喜欢简短的回答。”

在远程服务器上运行时，将应用端口转发到自己的电脑：

```bash
ssh -N -L 8765:127.0.0.1:8765 user@server
```

在本机浏览器中打开相同的 localhost 地址。通过 HTTPS 提供应用时，也可以使用麦克风和屏幕分享。

## 选择前端模型

| 前端模型 | 后台任务的准入方式 | 启动应用 |
| --- | --- | --- |
| Realtime-Venus-Audio | 原生控制 token 门控 | `jarvis serve --frontend venus` |
| MiniCPM-o | 因果 ASR 判断，原生文本注入回传结果 | `jarvis serve --frontend minicpm` |
| NVIDIA VoiceChat | 原生函数调用与结果通道 | `jarvis serve --frontend voicechat` |
| Venus-Omni | 原生音视频门控与视觉证据 | `jarvis serve --mode omni` |

切换前端之前，先安装并启动对应的模型服务。[模型安装与配置 →](docs/models_zh.md)

## Jarvis-Omni

安装 FFmpeg，然后准备本地模型：

```bash
jarvis setup venus
jarvis setup asr
jarvis setup omni
jarvis models download venus-omni asr jev-omni vision
```

为视觉复核配置 `GEMINI_API_KEY`、`GEMINI_BASE_URL` 和 `GEMINI_MODEL`。在不同终端中启动：

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

打开 **http://localhost:8765**，开始对话，再点击“选择游戏窗口”。分享前选择 CS2 或英雄联盟；浏览器支持时，在分享对话框中勾选游戏音频。通过陪玩方式选择器调整 Jarvis 主动发言的频率。

[模型指南](docs/models_zh.md#视觉读取适配器)介绍了仓库内的 HUD 和角色生命状态适配器。[屏幕与游戏配置 →](docs/games_zh.md)

## 游戏集成

| 游戏 | 交互方式 | 安装与启动 |
| --- | --- | --- |
| PvZ 网页同人游戏 | 询问建议、委派单步操作、开始代打并随时收回 | `jarvis setup pvz`，然后 `jarvis serve --game pvz` |
| Luanti / VoxeLibre | 探索持久世界、讨论资源并协作完成任务 | `jarvis setup voxel`，然后 `jarvis games start voxel` |
| CS2 | 基于屏幕的对话与战术建议 | `jarvis serve --mode omni --game cs2` |
| 英雄联盟 | 基于屏幕的对话、视觉事实与建议 | `jarvis serve --mode omni --game lol` |

体素游戏实际使用 **Luanti 和 VoxeLibre**。PvZ 与 VoxeLibre 通过游戏 bridge 获取状态、申请操作授权并接收引擎回执；CS2 与英雄联盟使用屏幕捕获。[安装、操作与端口 →](docs/games_zh.md)

## 配置与扩展

`jarvis serve --config path/to/config.yaml` 为包内默认配置加载覆盖项，环境变量用于配置凭据和服务地址。运行数据默认保存在 `~/.local/share/jarvis`；通过 `JARVIS_DATA_DIR` 或 `JARVIS_MODEL_DIR` 可以指定存储位置。

代码按共享职责组织：

```text
jarvis/
  core/       任务、worker、工具、记忆与播放
  audio/      原生准入与前端通信适配
  omni/       媒体时间线、视觉事实与屏幕交互
  adapters/   模型推理服务与原生门控
  games/      PvZ 与 VoxeLibre bridge
  resources/  提示词、浏览器客户端、默认配置与小型适配器
examples/     工具注册和配置示例
```

[添加工具、接入前端或使用类型化判断接口 →](docs/extending_zh.md)

## 引用

```bibtex
@techreport{jarvis2026,
  title = {Jarvis: Coordinating Fast Talk and Slow Work in Full-Duplex Audio and Omni Agents},
  author = {{Jarvis Team}},
  year = {2026},
  url = {https://amphionteam.github.io/Jarvis-Demo/}
}
```

## 许可证

原创 harness 代码和发布的适配器采用 [Apache-2.0](LICENSE)。VoxeLibre bridge 与游戏补丁保留各自许可证。下载的模型和游戏遵循上游许可证；PvZ 同人游戏用于个人非商业用途。详见[第三方组件](licenses/THIRD_PARTY.md)。
