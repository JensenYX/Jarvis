from __future__ import annotations
import ast
import asyncio
import json
import re
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Callable
import importlib
import inspect
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class CalculateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expression: str = Field(min_length=1, max_length=512)


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=12)


class WeatherArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    city: str = Field(min_length=2, max_length=160)
    days: int = Field(default=3, ge=1, le=7)
    country_code: str | None = Field(default=None, pattern="^[A-Za-z]{2}$")


class FetchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=8, max_length=2000)


class ArtifactArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=300000)
    title: str = Field(min_length=1, max_length=160)


class ReadArtifactArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=250)


class SourceEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    old: str = Field(min_length=1, max_length=20000)
    new: str = Field(max_length=20000)


class EditArtifactArgs(ReadArtifactArgs):
    edits: list[SourceEdit] = Field(min_length=1, max_length=12)
    title: str = Field(min_length=1, max_length=160)


class NewsArgs(SearchArgs):
    timezone: str = "UTC"
    today_only: bool = True


def calculate(expression: str) -> str:
    tree = ast.parse(expression, mode="eval")
    if len(list(ast.walk(tree))) > 128:
        raise ValueError("Expression too complex")

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return Decimal(ast.get_source_segment(expression, node).replace("_", ""))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if not isinstance(node, ast.BinOp):
            raise ValueError("Only numeric arithmetic is allowed")
        left, right = (visit(node.left), visit(node.right))
        if isinstance(node.op, ast.Add):
            value = left + right
        elif isinstance(node.op, ast.Sub):
            value = left - right
        elif isinstance(node.op, ast.Mult):
            value = left * right
        elif isinstance(node.op, ast.Div):
            value = left / right
        elif isinstance(node.op, ast.FloorDiv):
            value = (left / right).to_integral_value(rounding="ROUND_FLOOR")
        elif isinstance(node.op, ast.Mod):
            value = left % right
        elif (
            isinstance(node.op, ast.Pow) and right == int(right) and (abs(right) <= 100)
        ):
            value = left ** int(right)
        else:
            raise ValueError("Unsupported arithmetic operation")
        if not value.is_finite() or abs(value) > Decimal("1e100"):
            raise ValueError("Numeric range exceeded")
        return value

    with localcontext() as context:
        context.prec = 40
        result = visit(tree)
    if not result.is_finite() or abs(result) > Decimal("1e100"):
        raise ValueError("Numeric range exceeded")
    return format(result, "f")


def terms(text: str) -> set[str]:
    text = text.lower()
    words = set(re.findall("[a-z0-9_]+", text))
    for span in re.findall("[\\u3400-\\u9fff]+", text):
        words.update(span)
        words.update((span[i : i + 2] for i in range(len(span) - 1)))
    return words


class ToolRegistry:
    def __init__(self, config: dict, memory_search: Callable | None = None):
        self.mode = "live"
        self.documents_path = (
            Path(config["documents"]) if config.get("documents") else None
        )
        self.memory_search = memory_search
        self.extensions = {}
        self.models = {"calculate": CalculateArgs}
        if self.documents_path is not None:
            self.models["search_documents"] = SearchArgs
        from jarvis.core.web_tools import WebTools

        self.web = WebTools(config)
        self.artifact_root = Path(config.get("artifact_directory", "data/artifacts"))
        if config.get("web_search_provider") == "bing_rss":
            self.models.update(web_search=SearchArgs, fetch_url=FetchArgs)
        if config.get("weather_provider") == "open_meteo":
            self.models["get_weather"] = WeatherArgs
        if config.get("artifacts_enabled"):
            self.models["create_file"] = ArtifactArgs
        if config.get("task_artifacts"):
            self.models["read_file"] = ReadArtifactArgs
            self.models["edit_file"] = EditArtifactArgs
        if config.get("news_provider"):
            self.models["search_news"] = NewsArgs
        if memory_search:
            self.models["memory_search"] = SearchArgs
        for entry in config.get("plugins", []):
            module, separator, function = entry.partition(":")
            if not separator or not module or not function:
                raise ValueError("Tool plugin must be module:function")
            getattr(importlib.import_module(module), function)(self)

    def register(self, name, description, input_model, handler, *, scope="read_only"):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or name in self.models:
            raise ValueError("Tool names must be unique lowercase identifiers")
        if not issubclass(input_model, BaseModel) or not callable(handler):
            raise TypeError(
                "A tool requires a Pydantic input model and a callable handler"
            )
        self.models[name] = input_model
        self.extensions[name] = (description, handler, scope)

    @property
    def cards(self):
        return [
            {
                "name": name,
                "scope": self.extensions[name][2]
                if name in self.extensions
                else "artifact_write"
                if name in {"create_file", "edit_file"}
                else "read_only",
                "mode": "live",
            }
            for name in self.models
        ]

    def schemas(self, allowed_scope: list[str]):
        if "read_only" not in allowed_scope:
            return []
        descriptions = {
            "calculate": "Evaluate an exact numeric arithmetic expression using Decimal, no code execution.",
            "search_documents": f"Search the configured {self.mode} document corpus. Returns source IDs; not internet search.",
            "memory_search": "Retrieve source-backed personal memories for the current authenticated user only.",
            "web_search": "Search the public internet for current information. Returns real source URLs and snippets. Verify current claims with fetch_url.",
            "fetch_url": "Read a public web page for evidence. Local/private hosts and non-web schemes are unavailable.",
            "get_weather": "Get current conditions and a 1–7 day forecast from Open-Meteo for a user-specified city. Ask for the city if unknown; do not invent the user's location.",
            "create_file": "Create a downloadable code/document file. For a browser game, write a complete standalone HTML with inline CSS/JS, playable without dependencies. Only writes a new artifact; does not execute code on the server.",
            "read_file": "Read a previously created file listed in this task's available_artifacts. Before modifying a game, read its actual source, preserve it, then create a new revised file. Cannot read arbitrary server files.",
            "edit_file": "After read_file, apply ALL needed source replacements atomically in edits:[{old,new},...], producing ONE complete revision with parent provenance. Each old span must match exactly once. Include initial speed, restart/reset speed and acceleration together as needed; preserve old files. Further changes must read/edit the returned new URL.",
            "search_news": "Search recent news RSS with publication timestamps and publisher attribution. For today's news use the requested location's IANA timezone (e.g. America/New_York). No results today is distinct from a network failure. A feed supports headlines, not unverified article details.",
        }
        descriptions.update({name: spec[0] for name, spec in self.extensions.items()})
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": descriptions[name],
                    "parameters": model.model_json_schema(),
                },
            }
            for name, model in self.models.items()
            if (
                name not in {"create_file", "edit_file"}
                or "artifact_write" in allowed_scope
            )
            and (
                name not in self.extensions or self.extensions[name][2] in allowed_scope
            )
        ]

    async def execute(
        self, name: str, arguments: str, allowed_scope: list[str], *, artifact_urls=()
    ) -> dict:
        try:
            if name not in self.models or "read_only" not in allowed_scope:
                raise ValueError("Unknown or disallowed tool")
            args = self.models[name].model_validate_json(arguments)
            if name in self.extensions:
                _, handler, scope = self.extensions[name]
                if scope not in allowed_scope:
                    raise ValueError("Tool scope is unavailable for this task")
                result = (
                    await handler(args)
                    if inspect.iscoroutinefunction(handler)
                    else await asyncio.to_thread(handler, args)
                )
                if not isinstance(result, dict):
                    raise TypeError("Tool handlers return a dictionary")
                return {"ok": True, "source": name, **result}
            if name == "web_search":
                return await self.web.search(args.query, args.limit)
            if name == "fetch_url":
                return await self.web.fetch(args.url)
            if name == "get_weather":
                return await self.web.weather(args.city, args.days, args.country_code)
            if name == "search_news":
                return await self.web.news(
                    args.query, args.limit, args.timezone, args.today_only
                )
            if name == "read_file":
                from jarvis.core.artifacts import read_artifact

                return read_artifact(self.artifact_root, args.url, artifact_urls)
            if name == "edit_file":
                if "artifact_write" not in allowed_scope:
                    raise ValueError("Artifact editing is not in this task's scope")
                from jarvis.core.artifacts import edit_artifact_many

                return edit_artifact_many(
                    self.artifact_root,
                    args.url,
                    [e.model_dump() for e in args.edits],
                    args.title,
                    artifact_urls,
                )
            if name == "create_file":
                if "artifact_write" not in allowed_scope:
                    raise ValueError("Artifact writing is not in this task's scope")
                from jarvis.core.artifacts import write_artifact

                return write_artifact(
                    self.artifact_root, args.filename, args.content, args.title
                )
            if name == "calculate":
                return {
                    "ok": True,
                    "source": "calculate",
                    "expression": args.expression,
                    "value": calculate(args.expression),
                }
            if name == "memory_search":
                return {
                    "ok": True,
                    "source": "user_memory",
                    "records": await self.memory_search(args.query, args.limit),
                }
            if not self.documents_path.is_file():
                return {
                    "ok": False,
                    "error": "document_corpus_unavailable",
                    "mode": self.mode,
                }
            documents = json.loads(self.documents_path.read_text())
            query_terms = terms(args.query)
            scored = [
                (
                    len(query_terms & terms(doc["text"] + " " + doc.get("title", ""))),
                    doc,
                )
                for doc in documents
            ]
            scored.sort(key=lambda pair: pair[0], reverse=True)
            return {
                "ok": True,
                "mode": self.mode,
                "documents": [doc for score, doc in scored[: args.limit] if score > 0],
            }
        except (
            ValueError,
            ValidationError,
            ArithmeticError,
            SyntaxError,
            OSError,
            httpx.HTTPError,
            TimeoutError,
        ) as error:
            return {
                "ok": False,
                "error": type(error).__name__,
                "message": str(error)[:300],
            }
