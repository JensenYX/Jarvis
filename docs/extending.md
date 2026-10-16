# Extending Jarvis

[English](extending.md) · [简体中文](extending_zh.md) · [README](../README.md)

## Add a tool

A tool has a name, a description, a Pydantic input model and a handler returning a dictionary. The registry exposes its input schema to workers and validates arguments before calling the handler.

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

Add the registration function to your configuration:

```yaml
tools:
  plugins:
    - examples.custom_tool:register
```

The repository includes this complete example. From the repository root, with the speech services running, launch it with:

```bash
python -m jarvis serve --config examples/custom_tool.yaml
```

Ask Jarvis to convert 20 degrees Celsius to Fahrenheit. For a separate project, install your tool module into the harness environment and use its `module:function` name. Asynchronous handlers run on the event loop; synchronous handlers run in a thread. Return `{"ok": false, "error": "..."}` for an expected tool failure. `read_only` is the default scope; file-changing extensions can use `artifact_write`.

Workers retain tool-call IDs and observations. Task versions and context checks still apply to custom tools. A handler should return only after its operation has a known outcome.

## Configure a worker

Use a Chat Completions-compatible endpoint with tool calling:

```dotenv
WORKER_BASE_URL=https://api.deepseek.com/v1
WORKER_MODEL=deepseek-flash
WORKER_API_KEY=
```

Optional request fields belong in YAML:

```yaml
worker:
  max_tool_rounds: 12
  task_timeout_s: 120
  request_options:
    temperature: 0
```

The worker's final result contains `status`, `spoken_answer`, `details`, `evidence` and `needs_user_input`. Created artifacts come from successful file-tool receipts. A `needs_input` result carries its question in `spoken_answer`.

## Typed decisions

JEV receives a state and questions with explicit options. Choice returns probabilities over named options; Noul returns a yes probability; Score returns an expected ordinal value. Runtime operations remain bound to the task IDs and versions used to construct their options.

The local service accepts `POST /decide` using `DecisionInput` and returns `DecisionResult`. Both types are in `jarvis.omni.decision.base`. A request includes its state, typed questions and, when needed, causal image or audio inputs. The returned input identity and option order are checked before use.

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

## Connect a frontend

`jarvis.audio.frontends` defines frontend capabilities and connection adapters. Implement `send` and `recv`, translate your model's native events, and add its runtime class and CLI choice alongside the existing frontends.

The shared audio transport uses mono float32 PCM: 16 kHz input and 24 kHz output. A model step carries its response ID, generation epoch, input sample position, text and optional audio. Native delegation supplies the request boundary; the transcript observer binds the user's words to that boundary.

Preserve the model's actual turn boundaries. A model without native cancellation still uses client playback invalidation to stop old audio. Backend replies use that model's supported text or function-result channel.

## Browser protocol and delivery

The browser connects to `/ws`, sends a `start` message, then audio frames and control events. The server returns session information, events and audio chunks. Each `playback.ack` identifies a chunk, generation epoch and cumulative played-sample count. Counts must be monotonic and cannot exceed the chunk size.

The runtime advances the playback epoch when clearing audio. A finished task becomes delivered only after the current presentation has complete playback receipts. Use task-version checks for results and preserve the difference between stopping playback and cancelling work.

Omni adds timestamped screen, game-audio and microphone messages. Its timeline accepts causal observations; claims keep their original evidence references, scene epoch and validity limit. The scheduler checks those claims when queueing speech and while sending chunks. Browser playback also stops at the evidence deadline.

## Document search

Set `tools.documents` to a JSON file containing objects with `id`, `title` and `text`. The document-search tool is registered only when a corpus is configured. For a different retrieval system, register a tool that returns document text and source identifiers through the same registry.
