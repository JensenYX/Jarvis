<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark) and (max-width: 600px)" srcset="docs/assets/hero-mobile-dark.svg">
    <source media="(max-width: 600px)" srcset="docs/assets/hero-mobile.svg">
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/hero-dark.svg">
    <img src="docs/assets/hero.svg" width="100%" alt="Jarvis — 对话继续，任务并行。">
  </picture>
</p>

<p align="center">
  <strong>在全双工语音与多模态智能体中<br>协调实时对话与后台任务</strong>
</p>

<p align="center">
  <a href="https://amphionteam.github.io/Jarvis-Demo/">观看演示</a> &nbsp;·&nbsp;
  <a href="#快速开始">快速开始</a> &nbsp;·&nbsp;
  <a href="#工作原理">架构说明</a> &nbsp;·&nbsp;
  <a href="docs/models_zh.md">模型指南</a> &nbsp;·&nbsp;
  <a href="README.md">English</a>
</p>

Jarvis 为全双工对话接入后台推理、工具和记忆。任务运行时，你可以继续交谈、修改要求、打断播报，并在之后重新听取结果。

<table>
  <tr>
    <td width="440" valign="top">
      <p><strong><a href="#快速开始">Jarvis-Audio</a></strong></p>
      <p>边聊边用工具，随时调整后台任务。</p>
      <p><sub>Realtime-Venus-Audio · MiniCPM-o · NVIDIA VoiceChat</sub></p>
    </td>
    <td width="440" valign="top">
      <p><strong><a href="#jarvis-omni">Jarvis-Omni</a></strong></p>
      <p>分享屏幕与游戏音频，围绕画面交流。</p>
      <p><sub>Venus-Omni · 屏幕 + 游戏音频</sub></p>
    </td>
  </tr>
</table>

两者共享任务账本、异步 worker、记忆与播放控制。

## 演示

点击预览图，观看对应的录制演示。

<table>
  <tr>
    <td width="33%" align="center" valign="top">
      <a href="https://amphionteam.github.io/Jarvis-Demo/#demo-audio-daily-en"><img src="docs/assets/audio-daily-en.jpg" width="100%" alt="观看 Jarvis 用英语交谈并处理后台任务"></a>
      <br><strong>英文对话</strong>
      <br><sub>Audio · 任务与工具</sub>
    </td>
    <td width="33%" align="center" valign="top">
      <a href="https://amphionteam.github.io/Jarvis-Demo/#demo-audio-daily-zh"><img src="docs/assets/audio-daily-zh.jpg" width="100%" alt="观看 Jarvis 用中文交谈并处理后台任务"></a>
      <br><strong>中文对话</strong>
      <br><sub>Audio · 任务与工具</sub>
    </td>
    <td width="33%" align="center" valign="top">
      <a href="https://amphionteam.github.io/Jarvis-Demo/#demo-audio-pvz"><img src="docs/assets/audio-pvz.jpg" width="100%" alt="观看 Jarvis 在植物大战僵尸中提供建议并参与操作"></a>
      <br><strong>PvZ</strong>
      <br><sub>Audio · 游戏协作</sub>
    </td>
  </tr>
  <tr>
    <td width="33%" align="center" valign="top">
      <a href="https://amphionteam.github.io/Jarvis-Demo/#demo-audio-minecraft-1"><img src="docs/assets/audio-minecraft-1.jpg" width="100%" alt="观看 Jarvis 与玩家在 VoxeLibre 中探索和建造"></a>
      <br><strong>VoxeLibre</strong>
      <br><sub>Audio · 探索建造</sub>
    </td>
    <td width="33%" align="center" valign="top">
      <a href="https://amphionteam.github.io/Jarvis-Demo/#demo-omni-cs2"><img src="docs/assets/omni-cs2.jpg" width="100%" alt="观看 Jarvis 根据 CS2 画面提供战术建议"></a>
      <br><strong>CS2</strong>
      <br><sub>Omni · 战术建议</sub>
    </td>
    <td width="33%" align="center" valign="top">
      <a href="https://amphionteam.github.io/Jarvis-Demo/#demo-omni-lol"><img src="docs/assets/omni-lol.jpg" width="100%" alt="观看 Jarvis 理解英雄联盟画面并提供游戏建议"></a>
      <br><strong>英雄联盟</strong>
      <br><sub>Omni · 局势交流</sub>
    </td>
  </tr>
</table>

## 快速开始

**Linux · Python 3.11 · 支持 BF16 的 NVIDIA GPU · 支持麦克风的浏览器**

下面将 Venus 和 ASR 分别放在两张 GPU 上。显存需求和远程模型服务配置见[模型指南](docs/models_zh.md)。

### 1. 安装

```bash
git clone https://github.com/JensenYX/Jarvis.git
cd Jarvis
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
cp .env.example .env
```

在 `.env` 中填写 `WORKER_API_KEY` 和 `TYPESAFE_API_KEY`。默认 worker 使用 DeepSeek；通过 `WORKER_BASE_URL` 和 `WORKER_MODEL` 可以连接其他兼容 Chat Completions 的服务。JEV 也可以使用[本地服务](docs/models_zh.md#本地-jev)。

### 2. 准备模型

```bash
jarvis setup venus
jarvis setup asr
jarvis models download venus asr
```

每个模型服务使用独立的 Python 环境。仓库已经包含 Audio 和 Omni 所需的小型适配器，基础模型按需下载。

### 3. 开始对话

打开三个终端，在每个终端中用 `source .venv/bin/activate` 激活 harness 环境。

**终端 1 · 语音模型**

```bash
jarvis models serve venus --gpu 0
```

**终端 2 · 语音识别**

```bash
jarvis models serve asr --gpu 1
```

**终端 3 · 应用**，等待两个模型加载完成后运行：

```bash
jarvis doctor
jarvis serve
```

打开 **http://localhost:8765**，选择语言，点击“**开始对话**”，并允许麦克风访问。

<details>
<summary><strong>可以这样试一试</strong></summary>

- “用计算器核对六百四十五乘以二十七。”
- “创建一个有重新开始按钮的 HTML 小游戏。”
- “把刚才的游戏改成浅色主题。”
- “先别说了。”然后说：“再读一遍刚才的结果。”
- “记住，我喜欢简短的回答。”

</details>

<details>
<summary><strong>在远程服务器上运行</strong></summary>

将应用端口转发到自己的电脑：

```bash
ssh -N -L 8765:127.0.0.1:8765 user@server
```

在本机浏览器中打开 **http://localhost:8765**。通过 HTTPS 提供应用时，也可以使用麦克风和屏幕分享。

</details>

## 工作原理

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/architecture-dark.svg">
  <img src="docs/assets/architecture.svg" width="100%" alt="Jarvis 架构：原生门控、轮次绑定、JEV、任务账本、worker、记忆、结果交付，以及 Omni 证据时间线">
</picture>

<p align="center"><sub><a href="docs/assets/architecture.svg">查看完整尺寸的架构图</a></sub></p>

| 职责 | Jarvis 如何协调 |
| --- | --- |
| **任务委派** | 前端模型持续对话。Venus 在原生控制 token 上使用小型适配器，语音生成权重保持冻结。 |
| **任务版本** | JEV 从运行时根据当前任务构造的选项中选择操作；修改和取消都绑定具体任务版本。 |
| **结果有效性** | worker 在执行工具前检查上下文；已被替换或取消的任务版本停止交付结果。 |
| **播放与交付** | 完整播放回执确认交付；被打断的结果仍可再次播放。 |
| **记忆** | 长期偏好和事实跨会话保存，临时要求留在对应任务中。 |
| **视觉证据** | Omni 将媒体对齐到统一时钟，并在播报前及播放期间检查视觉事实。 |

内置工具包括计算、网页搜索与读取、天气、新闻、文档检索、文件创建与修改，以及个人记忆检索。[添加自己的工具 →](docs/extending_zh.md)

## 选择前端模型

| 前端模型 | 后台任务的准入方式 | 启动应用 |
| --- | --- | --- |
| Realtime-Venus-Audio | 原生控制 token 门控 | `jarvis serve --frontend venus` |
| MiniCPM-o | 因果 ASR 判断，原生文本注入回传结果 | `jarvis serve --frontend minicpm` |
| NVIDIA VoiceChat | 原生函数调用与结果通道 | `jarvis serve --frontend voicechat` |
| Venus-Omni | 原生音视频门控与视觉证据 | `jarvis serve --mode omni` |

切换前端之前，先安装并启动对应的模型服务。[模型安装与配置 →](docs/models_zh.md)

## Jarvis-Omni

分享游戏窗口，围绕眼前的场景交流。Jarvis-Omni 将 Venus-Omni 与媒体时间线、视觉证据检查相结合。

<details>
<summary><strong>安装并运行 Jarvis-Omni</strong></summary>

安装 harness 后，安装 FFmpeg，并准备本地模型：

```bash
jarvis setup venus
jarvis setup asr
jarvis setup omni
jarvis models download venus-omni asr jev-omni vision
```

在 `.env` 中配置 `GEMINI_API_KEY`、`GEMINI_BASE_URL` 和 `GEMINI_MODEL`，用于视觉复核。在**不同终端**中激活 harness 环境，分别启动：

| 终端 | 命令 |
| --- | --- |
| 语音模型 | `jarvis models serve venus-omni --gpu 0` |
| 语音识别 | `jarvis models serve asr --gpu 1` |
| 多模态判断 | `jarvis models serve jev-omni --gpu 2` |
| 视觉描述 | `jarvis models serve vision --gpu 3` |

等待服务加载完成，在另一个终端中启动应用：

```bash
jarvis doctor --mode omni
jarvis serve --mode omni --game cs2
```

打开 **http://localhost:8765**，开始对话，再点击“**选择游戏窗口**”。分享前选择 CS2 或英雄联盟；浏览器支持时，在分享对话框中勾选游戏音频。通过陪玩方式选择器调整 Jarvis 主动发言的频率。

</details>

[视觉读取与内置适配器 →](docs/models_zh.md#视觉读取适配器) &nbsp;·&nbsp; [屏幕与游戏配置 →](docs/games_zh.md)

## 游戏集成

| 游戏 | 交互方式 | 安装与启动 |
| --- | --- | --- |
| PvZ 网页同人游戏 | 询问建议、委派单步操作、开始代打并随时收回 | `jarvis setup pvz`，然后 `jarvis serve --game pvz` |
| Luanti / VoxeLibre | 探索持久世界，协作完成任务 | `jarvis setup voxel`，然后 `jarvis games start voxel` |
| CS2 | 基于屏幕的对话与战术建议 | `jarvis serve --mode omni --game cs2` |
| 英雄联盟 | 基于屏幕的对话、视觉事实与建议 | `jarvis serve --mode omni --game lol` |

PvZ 与 VoxeLibre 通过游戏 bridge 获取状态、申请操作授权并接收引擎回执；CS2 与英雄联盟使用屏幕捕获。[安装、操作与端口 →](docs/games_zh.md)

## 配置与扩展

| 指南 | 你可以做什么 |
| --- | --- |
| [模型与配置](docs/models_zh.md) | 选择前端、规划 GPU 资源并连接模型服务。 |
| [游戏集成](docs/games_zh.md) | 安装 PvZ、Luanti / VoxeLibre 并配置屏幕捕获。 |
| [扩展 Jarvis](docs/extending_zh.md) | 注册工具、接入前端并使用类型化判断接口。 |
| [示例](examples/) | 从自定义工具或模型配置示例开始。 |

`jarvis serve --config path/to/config.yaml` 为包内默认配置加载覆盖项，环境变量用于配置凭据和服务地址。运行数据默认保存在 `~/.local/share/jarvis`；通过 `JARVIS_DATA_DIR` 或 `JARVIS_MODEL_DIR` 可以指定存储位置。

<details>
<summary><strong>代码目录</strong></summary>

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

</details>

## 引用

<details>
<summary><strong>BibTeX</strong></summary>

```bibtex
@techreport{jarvis2026,
  title = {Jarvis: Coordinating Fast Talk and Slow Work in Full-Duplex Audio and Omni Agents},
  author = {{Jarvis Team}},
  year = {2026},
  url = {https://amphionteam.github.io/Jarvis-Demo/}
}
```

</details>

## 许可证

原创 harness 代码和发布的适配器采用 [Apache-2.0](LICENSE)。VoxeLibre bridge 与游戏补丁保留各自许可证。下载的模型和游戏遵循上游许可证；PvZ 同人游戏用于个人非商业用途。详见[第三方组件](licenses/THIRD_PARTY.md)。
