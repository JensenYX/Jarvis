# 扩展 Jarvis

[English](extending.md) · [简体中文](extending_zh.md) · [README](../README_zh.md)

## 添加工具

工具由名称、描述、Pydantic 输入模型和返回字典的处理函数组成。注册表将输入 schema 提供给 worker，并在调用处理函数前验证参数。

```python
from pydantic import BaseModel, ConfigDict


class Temperature(BaseModel):
    model_config = ConfigDict(extra="forbid")
    celsius: float


async def convert(arguments):
    return {"fahrenheit": arguments.celsius * 9 / 5 + 32}


def register(registry):
    registry.register("convert_temperature", "Convert Celsius to Fahrenheit.", Temperature, convert)
```

在配置中添加注册函数：

```yaml
tools:
  plugins:
    - examples.custom_tool:register
```

仓库包含完整示例。保持语音服务运行，在仓库根目录执行：

```bash
python -m jarvis serve --config examples/custom_tool.yaml
```

让 Jarvis 将 20 摄氏度换算成华氏度。在独立项目中，将工具模块安装到 harness 环境，再配置其 `module:function` 名称。异步处理函数在事件循环中运行，同步函数在线程中运行。预期内的工具失败可返回 `{"ok": false, "error": "..."}`。默认作用域为 `read_only`；修改文件的扩展可以使用 `artifact_write`。

worker 保留工具调用 ID 和观察结果。自定义工具同样受任务版本及上下文检查约束，处理函数应在操作结果确定后返回。

## 配置 worker

使用支持工具调用的 Chat Completions 兼容服务：

```dotenv
WORKER_BASE_URL=https://api.deepseek.com/v1
WORKER_MODEL=deepseek-flash
WORKER_API_KEY=
```

可选请求字段通过 YAML 配置：

```yaml
worker:
  max_tool_rounds: 12
  task_timeout_s: 120
  request_options:
    temperature: 0
```

worker 的最终结果包含 `status`、`spoken_answer`、`details`、`evidence` 和 `needs_user_input`。生成文件来自成功的文件工具回执。`needs_input` 结果将需要用户回答的问题放在 `spoken_answer` 中。

## 类型化判断

JEV 接收状态和带显式选项的问题。Choice 返回各命名选项的概率，Noul 返回肯定概率，Score 返回有序量表上的期望值。运行时操作始终绑定构造选项时使用的任务 ID 和版本。

本地服务通过 `POST /decide` 接收 `DecisionInput` 并返回 `DecisionResult`，类型定义位于 `jarvis.omni.decision.base`。请求包含状态、类型化问题，以及需要时的因果图片或音频。返回结果的输入标识和选项顺序在使用前都会经过检查。

```python
import asyncio

from jarvis.omni.decision.base import DecisionInput, Option, Question
from jarvis.omni.decision.jev_omni import JevOmniBackend


async def main():
    backend = JevOmniBackend("http://127.0.0.1:8810")
    request = DecisionInput(
        state={"request": "stop talking", "task_running": True},
        questions=(Question(
            id="operation",
            primitive="choice",
            instructions="Which operation did the user request?",
            options=(
                Option(id="stop_playback", text="Stop the voice and preserve background work."),
                Option(id="cancel_task", text="Cancel the background task."),
            ),
        ),),
    )
    try:
        result = await backend.decide(request)
        print(result.answers[0].choice)
    finally:
        await backend.close()


asyncio.run(main())
```

## 接入前端模型

`jarvis.audio.frontends` 定义前端能力和连接适配器。实现 `send`、`recv`，转换模型的原生事件，再参照已有前端添加运行时类和 CLI 选项。

共享音频传输使用单声道 float32 PCM：输入 16 kHz，输出 24 kHz。模型 step 包含回复 ID、生成 epoch、输入采样位置、文本和可选音频。原生委派提供请求边界，转写观察器将用户原话绑定到该边界。

保留模型真实的轮次边界。没有原生取消能力的模型仍通过客户端播放失效机制停止旧音频。后台结果通过模型支持的文本或函数结果通道回传。

## 浏览器协议与交付

浏览器连接 `/ws`，发送 `start`，然后传输音频帧和控制事件。服务端返回会话信息、事件和音频块。每条 `playback.ack` 包含音频块 ID、生成 epoch 和累计播放采样数；采样数单调递增，不能超过音频块长度。

清空音频时，运行时推进播放 epoch。只有当前播报收到完整播放回执，已完成任务才会标记交付。结果处理需要检查任务版本，并区分停止播报与取消任务。

Omni 增加带时间戳的屏幕、游戏音频和麦克风消息。时间线接收因果观察，视觉事实保留原始证据引用、场景 epoch 和有效期。调度器在语音入队和发送音频块时检查事实，浏览器播放也会在证据截止时间停止。

## 文档检索

将 `tools.documents` 指向一个 JSON 文件，内容为包含 `id`、`title` 和 `text` 的对象列表。配置语料后才会注册文档搜索工具。接入其他检索系统时，可以注册一个返回文档文本和来源标识的工具。
