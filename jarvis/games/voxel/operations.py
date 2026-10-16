from __future__ import annotations
import asyncio
import copy
import json
import math
import time
from collections import deque
from uuid import uuid4
from . import survival_intent as semantic
from .adapter import IDENTITY
from .attention import holds_actions


class Operations:
    def __init__(self, mode):
        self.mode = mode
        self.r = mode.r
        self.task = None
        self.generation = 0
        self.world = None
        self.identity = None
        self.job = None
        self.step_job = None
        self.pending = False
        self.recent = deque(maxlen=12)
        self.last_step = 0
        self.failures = 0
        self.missing_since = None
        self.visited = {}
        self.resource_memory = {}
        self.facilities = {}
        self.emergency_job = None
        self.emergency_checked = 0.0
        self.paused_goal = None
        self.removed_resources = set()
        self.search_views = {}
        self.item_knowledge = {}
        self.observation_memory = {}
        self.native_item_memory = {}
        self.retry_plan = None
        self.entity_memory = {}
        self.last_activity = None
        self.activity_at = 0.0
        self.last_effect_at = 0.0

    @property
    def bridge(self):
        return self.mode.bridge

    def emit(self):
        self.r.emit(
            "voxel.task",
            task=copy.deepcopy(self.task),
            active=bool(self.bridge and self.bridge.task_current()),
            generation=self.generation,
        )

    def activity(self):
        t = self.task
        if not t:
            return
        state = "manual"
        if t.get("status") == "PLANNING":
            state = "planning"
        elif t.get("authorized") and t.get("status") in {"READY", "ARMING"}:
            state = "synchronizing"
        elif t.get("status") == "RUNNING":
            if not self.bridge.fresh():
                state = "synchronizing"
            elif holds_actions(self.mode):
                state = "listening"
            elif self.pending:
                state = "executing"
            elif self.job and (not self.job.done()):
                state = "planning"
            else:
                state = "deciding"
        if t.get("status") == "COMPLETED":
            state = "completed"
        now = time.monotonic()
        key = (t["id"], state, t.get("completed", 0))
        if key != self.last_activity or now - self.activity_at >= 2:
            self.last_activity = key
            self.activity_at = now
            self.r.emit(
                "voxel.task.activity",
                task_id=t["id"],
                state=state,
                confirmed_effects=t.get("completed", 0),
                seconds_since_effect=round(now - self.last_effect_at, 1)
                if self.last_effect_at
                else None,
            )

    async def save(self, source):
        if not self.task:
            return
        value = copy.deepcopy(self.task)
        value["authorized"] = False
        value.pop("deadline", None)
        value.pop("confirmation", None)
        value["observed_resources"] = list(self.resource_memory.values())[-24:]
        value["removed_resources"] = list(self.removed_resources)[-96:]
        value["known_facilities"] = list(self.facilities.values())[-24:]
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        parts = [raw[i : i + 900] for i in range(0, len(raw), 900)]
        await self.r.memory.replace_task_bundle(
            self.r.user_id,
            "voxel-survival:" + self.world,
            [
                json.dumps({"index": i, "data": part}, ensure_ascii=False)
                for i, part in enumerate(parts)
            ],
            source,
        )

    async def bind(self, s):
        identity = tuple((s[k] for k in IDENTITY))
        if self.identity == identity:
            return
        self.stop("task_binding_changed")
        self.identity = identity
        if self.world != s["world_id"]:
            self.observation_memory.clear()
            self.native_item_memory.clear()
            self.world = s["world_id"]
            self.task = None
            self.recent.clear()
            self.resource_memory.clear()
            self.removed_resources.clear()
            self.facilities.clear()
            self.search_views.clear()
            self.item_knowledge.clear()
            self.entity_memory.clear()
            rows = await self.r.memory.read_task_bundle(
                self.r.user_id, "voxel-survival:" + self.world
            )
            try:
                parts = sorted(
                    (json.loads(r["text"]) for r in rows), key=lambda v: v["index"]
                )
                if parts:
                    self.task = json.loads("".join((r["data"] for r in parts)))
                    self.task["authorized"] = False
                    self.resource_memory = {
                        str(r["position"]): r
                        for r in self.task.get("observed_resources", [])
                    }
                    self.removed_resources = set(self.task.get("removed_resources", []))
                    self.facilities = {
                        str(r["position"]): r
                        for r in self.task.get("known_facilities", [])
                    }
                    if self.task["status"] not in {"COMPLETED", "CANCELLED"}:
                        self.task["status"] = "PAUSED"
            except (ValueError, KeyError, TypeError):
                self.task = None
        self.emit()

    def stop(self, reason):
        arming = bool(self.task and self.task.get("status") == "ARMING")
        self.generation += 1
        if self.job and self.job is not asyncio.current_task():
            self.job.cancel()
        if self.step_job and self.step_job is not asyncio.current_task():
            self.step_job.cancel()
        if self.emergency_job and self.emergency_job is not asyncio.current_task():
            self.emergency_job.cancel()
        self.emergency_job = None
        self.step_job = None
        self.job = None
        self.retry_plan = None
        if reason not in {"urgent_survival", "rescue_complete"}:
            self.paused_goal = None
        if self.task:
            self.task.pop("completion_pending", None)
            self.task.pop("pending_outcome_plan", None)
            deadline = self.task.pop("deadline", None)
            if deadline is not None:
                self.task["remaining_ms"] = max(
                    0, int((deadline - time.monotonic()) * 1000)
                )
            self.task.pop("confirmation", None)
            self.task["authorized"] = False
            if self.task["status"] not in {"COMPLETED", "CANCELLED"}:
                self.task["status"] = "PAUSED"
        if self.bridge and (self.bridge.task_grant or arming):
            self.bridge.revoke(reason)
            self.r.spawn(self.bridge.display.release())
        if self.mode.building:
            self.mode.building.pending_voice = None

    def say(self, text=None, cue=None):
        if self.mode.building:
            self.mode.building.say(text, cue)
        else:
            self.mode.say(text, "game_task", cue=cue)

    def cue(self, question):
        return (
            "<|im_start|>user\n根据当前任务、已观察事实和真实回执，用简短中文回答用户。区分打过/挖过/合成过与仅计划；不要虚构完成。\n"
            + json.dumps(
                {
                    "question": question,
                    "task": semantic.brief(self.task),
                    "game": semantic.context(self.bridge.snapshot),
                    "recent": list(self.recent),
                },
                ensure_ascii=False,
            )
            + "<|im_end|>"
        )

    def status_text(self):
        t = self.task
        if not t:
            return "当前没有在执行任务，你可以让我探索、采集或说明想做的事。"
        if t.get("pending_question"):
            return t["pending_question"]
        if t.get("outcome"):
            o = t["outcome"]
            status = t.get("status")
            if status == "COMPLETED":
                return o["objective"] + "已经完成，结果已核对。"
            if status in {"PAUSED", "CANCELLED"}:
                return (
                    o["objective"]
                    + "目前已"
                    + ("取消。" if status == "CANCELLED" else "暂停，进度保留着。")
                )
            checked = t.get("outcome_progress", {}).get("checks", [])
            pending = [
                c["description"]
                for i, c in enumerate(o.get("success_checks", []))
                if i >= len(checked) or not checked[i]
            ]
            if t.get("stalled_s", 0) >= 12:
                return (
                    "还没完成"
                    + o["objective"].rstrip("。")
                    + "，刚才没有推进，正在重新检查可行的办法。"
                )
            return (
                "目标是"
                + o["objective"].rstrip("。")
                + "。"
                + (
                    "还在完成：" + pending[0].rstrip("。") + "。"
                    if pending
                    else o["strategy"]
                )
            )
        label = (t.get("stage_goal") or {}).get("label") or t.get(
            "goal", t.get("request", "当前任务")
        )
        if t.get("stalled_s", 0) >= 12:
            return f"还没完成{label}，刚才没有推进。我正在根据可见障碍重新选路。"
        if t.get("status") == "RUNNING":
            return (
                f"正在{label}，已经确认完成{t.get('completed', 0)}次采挖、放置或合成。"
            )
        if t.get("status") == "COMPLETED":
            return f"{label}已经完成。"
        return f"{label}目前已暂停，进度保留着。"

    @staticmethod
    def scope_ready(task):
        scope = task.get("scope") or {}
        kind = scope.get("kind")
        if kind in {"DIG", "PLACE"}:
            return bool(
                scope.get("targets") or (kind == "DIG" and scope.get("corridor"))
            )
        if kind == "CRAFT":
            return bool(scope.get("recipes"))
        if kind == "COMBAT":
            return bool(scope.get("entities") or scope.get("hostiles"))
        if task.get("agent_goal") == "OUTCOME":
            return kind == "AGENT" and bool(
                task.get("outcome", {}).get("success_checks")
            )
        return kind in {"AGENT", "TRAVEL"} and bool(task.get("agent_goal"))

    async def review_authorization(self, text, a):
        route = a.get("effective_route", a["route"]["choice"])
        authorize = semantic.authorized(a)
        if (
            route
            in {
                "DIG",
                "PLACE",
                "CRAFT",
                "COMBAT",
                "RESUME",
                "REVISE",
                "TRAVEL",
                "AGENT",
            }
            and (not authorize)
            and (a["reclaim"]["noul"] < 0.8)
        ):
            clauses = await self.r.controller.judge_typed(
                "voxel_current_request_clauses",
                {
                    "current_utterance": text,
                    "pending_reply": {
                        k: self.task.get(k)
                        for k in ("request", "confirmation", "pending_question")
                    }
                    if self.task and route == "RESUME"
                    else None,
                },
                {
                    "actor": {
                        "type": "choice",
                        "instructions": "Who is asked to perform the affirmative CURRENT action? An imperative without an explicit subject addresses the companion. “自由探索一下这片区域，不用再做工作台” requests companion exploration and forbids crafting tables; the negative clause does not cancel exploration. “我自己探索/别操作/只给建议” means human. Quoted/hypothetical commands are DISCUSS. Short yes/continue needs pending_reply.",
                        "criteria": {
                            "COMPANION": "Companion should do the affirmative game action now",
                            "HUMAN": "Human will act; companion should stop",
                            "DISCUSS": "Only discussion, quotation, hypothetical or no affirmative action",
                        },
                    }
                },
                deadline_ms=2200,
            )
            actor = clauses["actor"]
            self.r.emit("voxel.task.clause_authorization", answer=clauses)
            if actor["choice"] == "COMPANION" and actor["confidence"] >= 0.8:
                a["authorization_reviewed"] = True
                return a
            state = {
                "utterance": text,
                "request_type": route,
                "current_task": {
                    k: self.task.get(k)
                    for k in (
                        "request",
                        "agent_goal",
                        "pending_question",
                        "confirmation",
                    )
                }
                if self.task
                else None,
                "context_rule": "A NEW explicit request creates its own task; no pre-existing task is required. Prior task is only needed to resolve short yes/continue replies.",
            }
            review = await self.r.controller.judge_typed(
                "voxel_authorization_review",
                state,
                {
                    "request": {
                        "type": "choice",
                        "instructions": "只判断用户当前是否要陪玩执行。新指令“能不能帮我挖通这条路”即请求你行动，不需要已有任务，目标清晰度和游戏能否执行不影响意图。只有“是的/继续”这类省略回答需要current_task指代。忽略旧控制权。否定、引用、假设、只讨论或用户自己来不授权。",
                        "criteria": {
                            "EXECUTE": "用户当前明确请求陪玩操作",
                            "ADVISE": "只讨论、展示方案或解释",
                            "HUMAN": "用户自己操作或停止陪玩",
                            "UNCLEAR": "缺少必要确认",
                        },
                    }
                },
                deadline_ms=2000,
            )
            a["authorization_reviewed"] = (
                review["request"]["choice"] == "EXECUTE"
                and review["request"]["confidence"] >= 0.85
            )
            self.r.emit("voxel.task.authorization_reviewed", answer=review)
            if not a["authorization_reviewed"] and review["request"]["choice"] in {
                "UNCLEAR",
                "EXECUTE",
            }:
                async with self.r.worker_slots, asyncio.timeout(12):
                    reply = await self.mode.reasoning.complete(
                        [
                            {
                                "role": "system",
                                "content": "Resolve ONLY whether the current utterance asks the companion to act. Return JSON {execute:boolean,reason:string}. A NEW explicit polite command (Can you help me clear this path? 帮我挖通路) is TRUE and creates a task even when current_task=null. Imperative “自由探索这片区域，不用再做工作台” is TRUE: do explore; do not craft a table. A negative restriction on a DIFFERENT action never cancels an affirmative request. 我自己探索/不要操作 are FALSE. Do not require an existing task, resources, exact target or execution readiness for explicit instructions. Only short yes/start/continue replies need a pending referent. Quoted/hypothetical commands and advice without an affirmative action request are false.",
                            },
                            {
                                "role": "user",
                                "content": json.dumps(state, ensure_ascii=False),
                            },
                        ],
                        json_mode=True,
                        max_tokens=300,
                        reasoning_effort="none",
                    )
                c = reply["choices"][0]
                v = json.loads(c["message"]["content"])
                if (
                    c.get("finish_reason") != "stop"
                    or type(v.get("execute")) is not bool
                ):
                    raise ValueError("Incomplete authorization review")
                a["authorization_reviewed"] = v["execute"]
                self.r.emit(
                    "voxel.task.authorization_reviewed", answer=v, provider="worker"
                )
        return a

    async def apply(self, update, a, origin, s):
        await self.bind(s)
        if hasattr(self.bridge, "synced_origin"):
            origin = self.bridge.synced_origin(origin) or origin
        route = a.get("effective_route", a["route"]["choice"])
        authorize = semantic.authorized(a)
        if route in {"OTHER", "BUILD", "ADVICE"}:
            return False
        if route == "STATUS":
            if a.get("handoff", {}).get("choice") == "PREVIEW":
                self.stop("preview_only")
                self.emit()
            self.say(self.status_text())
            return True
        if (
            route == "REVISE"
            and self.task
            and (a.get("handoff", {}).get("choice") == "CONDITION")
        ):
            self.task["constraints"] = (
                self.task.get("constraints", []) + [update.text]
            )[-12:]
            self.task["history"] = (
                self.task.get("history", [])
                + [{"text": update.text, "source": update.utterance_id}]
            )[-12:]
            self.say("记住了，后续会按这个要求判断。")
            await self.save(update.utterance_id)
            self.emit()
            return True
        if route == "RECLAIM" and a["change"]["noul"] < 0.8:
            self.stop("semantic_task_reclaim")
            self.emit()
            await self.save(update.utterance_id)
            return False
        if not origin or not self.bridge.current(origin):
            self.r.emit(
                "voxel.task.authorization_stale",
                has_origin=bool(origin),
                current_generation=self.bridge.generation,
                source_generation=(origin or {}).get("generation"),
                source_epoch=(origin or {}).get("anchor", {}).get("control_epoch"),
                current_epoch=self.bridge.snapshot.get("control_epoch"),
            )
            authorize = False
        if route == "RESUME" and self.task:
            if self.task["status"] == "CANCELLED":
                self.say("那项任务已取消。可以直接告诉我新的目标。")
                return True
            if not self.scope_ready(self.task) or self.task.get("pending_question"):
                original = self.task.get("request", "")
                a = {
                    **a,
                    "effective_route": self.task["kind"],
                    "route": {**a["route"], "choice": self.task["kind"]},
                }
                update = update.model_copy(
                    update={"text": original + "；当前补充：" + update.text}
                )
                return await self.apply(update, a, origin, s)
            if not authorize:
                self.task["confirmation"] = {
                    "kind": "execute",
                    "task_id": self.task["id"],
                    "goal": self.task.get("goal"),
                    "question": "要继续执行当前任务吗？",
                }
                self.say("要继续执行当前任务吗？")
                self.emit()
                return True
            if self.task["status"] in {"RUNNING", "ARMING"} and (
                not a.get("duration_ms")
            ):
                self.say("正在继续执行当前任务。")
                return True
            if self.bridge.task_grant:
                self.stop("explicit_task_resume")
            self.task["authorized"] = True
            self.task["source"] = update.utterance_id
            self.task["generation"] = self.bridge.generation
            self.task["duration_ms"] = (
                a.get("duration_ms")
                or self.task.get("remaining_ms", self.task.get("duration_ms", 900000))
                or self.task.get("duration_ms", 900000)
            )
            self.task.pop("deadline", None)
            self.task.pop("confirmation", None)
            self.task["pending_question"] = None
            self.task.pop("arming_started", None)
            building = self.mode.building
            if (
                self.task.get("agent_goal") == "MATERIALS"
                and building
                and building.project
                and (self.task.get("project_id") == building.project.project_id)
            ):
                building.project.authorization = {
                    "source": update.utterance_id,
                    "duration_ms": self.task["duration_ms"],
                    "generation": self.bridge.generation,
                    "supply": True,
                }
                if not building.project.blueprint and (
                    not building.job or building.job.done()
                ):
                    building.design(update.utterance_id, True)
            if self.task["kind"] == "TRAVEL" and self.task.get("surface_reported"):
                self.task["surface_reported"] = False
            self.r.spawn(self.begin_after_revoke(update.utterance_id))
            return True
        if route == "REVISE" and self.task:
            kind = self.task["kind"]
        elif route in {"DIG", "PLACE", "CRAFT", "COMBAT", "AGENT", "TRAVEL"}:
            kind = route
        elif self.task and a["change"]["noul"] >= 0.8:
            kind = self.task["kind"]
        else:
            return False
        if kind == "COMBAT" and a["scope"]["choice"] != "ONCE":
            kind = "AGENT"
        if a["scope"]["choice"] == "TIMED" and (not a.get("duration_ms")):
            self.say("时间范围还不明确，请说希望我操作多久。")
            return True
        if a.get("duration_ms") and (not 1000 <= a["duration_ms"] <= 900000):
            self.say("一次持续任务最长十五分钟，进度会保留，之后可以继续。")
            return True
        if self.mode.building:
            self.mode.building.stop("survival_task_requested")
        previous = copy.deepcopy(self.task)
        self.stop("task_revised")
        self.recent.clear()
        self.task = {
            "id": uuid4().hex,
            "kind": kind,
            "status": "PLANNING",
            "request": update.text,
            "source": update.utterance_id,
            "world_id": self.world,
            "goal": update.text,
            "constraints": [],
            "quantity": a.get("quantity")
            or (1 if a["scope"]["choice"] == "ONCE" else None),
            "completed": 0,
            "duration_ms": a.get("duration_ms") or 900000,
            "scope": {"kind": kind},
            "authorized": authorize,
            "generation": self.bridge.generation,
            "pending_question": None,
            "history": (
                (previous or {}).get("history", [])
                + [{"text": update.text, "source": update.utterance_id}]
            )[-12:],
            "stop_kind": a["scope"]["choice"],
        }
        self.task.update(
            start_position=copy.deepcopy(s["view"]["self"]["position"]),
            start_yaw=s["view"]["self"].get("yaw", 0),
            baseline_inventory=semantic.context(s)["inventory"],
        )
        if authorize:
            self.task["deadline"] = time.monotonic() + self.task["duration_ms"] / 1000
        if getattr(self.mode, "memory", None):
            self.task["constraints"] = list(self.mode.memory.preferences.values())[-6:]
        self.visited.clear()
        if kind == "COMBAT" and a["scope"]["choice"] != "ONCE":
            self.task["quantity"] = None
        gen = self.generation
        task = self.task
        snapshot = copy.deepcopy(s)

        async def plan():
            try:
                if kind == "AGENT":
                    from . import outcomes

                    strategy, means = await outcomes.authorized_plan(
                        self.mode, task, snapshot, previous
                    )
                    resolved = {
                        "agent_goal": "OUTCOME",
                        "outcome": strategy,
                        "autonomy": True,
                        "scope": {
                            "kind": "AGENT",
                            "recipes": outcomes.recipe_scope(self.mode, strategy),
                            "focus_items": strategy.get("focus_items", []),
                            **means,
                        },
                    }
                    if strategy.get("question"):
                        resolved["question"] = strategy["question"]
                elif kind == "TRAVEL":
                    from .agency import objective

                    project = self.mode.building.project if self.mode.building else None
                    if project and project.status == "CANCELLED":
                        project = None
                    resolved = await objective(
                        self.r.controller,
                        update.text,
                        snapshot,
                        previous,
                        project,
                        kind=kind,
                        reasoning=self.mode.reasoning,
                    )
                    if a.get("named_objective"):
                        resolved["agent_goal"] = a["named_objective"]
                    resolved["scope"] = {
                        "kind": kind,
                        "recipes": [],
                        "navigation": resolved.get("navigation_authorized", False),
                    }
                    if resolved["agent_goal"] == "OUTCOME" or (
                        resolved.get("extent") == "DESTINATION"
                        and resolved["agent_goal"] in {"ASCEND", "EXPLORE"}
                    ):
                        from . import outcomes

                        strategy, means = await outcomes.authorized_plan(
                            self.mode, task, snapshot, previous
                        )
                        resolved = {
                            "agent_goal": "OUTCOME",
                            "outcome": strategy,
                            "autonomy": True,
                            "kind_override": "AGENT",
                            "scope": {
                                "kind": "AGENT",
                                "recipes": outcomes.recipe_scope(self.mode, strategy),
                                "focus_items": strategy.get("focus_items", []),
                                **means,
                            },
                        }
                elif kind == "CRAFT":
                    resolved = await self.plan_craft(
                        update.text, snapshot, previous, discussion=a.get("discussion")
                    )
                    ready = not resolved.get("question") and any(
                        (
                            r["recipe_id"] in resolved.get("recipes", [])
                            for r in snapshot["view"]
                            .get("survival", {})
                            .get("recipes", [])
                        )
                    )
                    if not resolved.get("question") and (not ready):
                        from . import outcomes

                        item = resolved["item_id"]
                        count = resolved["quantity"]
                        task["resolved_product"] = {"item_id": item, "quantity": count}
                        label = next(
                            (
                                r.get("name_zh") or r["name"].split("\n")[0]
                                for r in self.bridge.recipe_catalog.values()
                                if r["item_id"] == item
                            ),
                            item,
                        )
                        task["outcome"] = {
                            "objective": f"合成{count}个{label}",
                            "strategy": "准备所需材料和合成设施，然后完成指定物品。",
                            "success_checks": [
                                {
                                    "description": f"新增合成{count}个{item}并收入背包",
                                    "evidence": ["inventory"],
                                    "item_ids": [item],
                                    "minimum": count,
                                    "inventory_mode": "gained",
                                }
                            ],
                            "craft_products": [item],
                            "focus_items": [item],
                        }
                        strategy, means = await outcomes.authorized_plan(
                            self.mode, task, snapshot, previous
                        )
                        resolved = {
                            "agent_goal": "OUTCOME",
                            "outcome": strategy,
                            "autonomy": True,
                            "kind_override": "AGENT",
                            "scope": {
                                "kind": "AGENT",
                                "recipes": outcomes.recipe_scope(self.mode, strategy),
                                "focus_items": strategy.get("focus_items", []),
                                **means,
                            },
                        }
                elif kind == "PLACE":
                    resolved = await semantic.placement(
                        self.r.controller, update.text, snapshot, previous
                    )
                    if resolved.get("question"):
                        from . import outcomes

                        strategy, means = await outcomes.authorized_plan(
                            self.mode, task, snapshot, previous
                        )
                        resolved = {
                            "agent_goal": "OUTCOME",
                            "outcome": strategy,
                            "autonomy": True,
                            "kind_override": "AGENT",
                            "scope": {
                                "kind": "AGENT",
                                "recipes": outcomes.recipe_scope(self.mode, strategy),
                                "focus_items": strategy.get("focus_items", []),
                                **means,
                            },
                        }
                else:
                    extra = []
                    if (
                        kind == "DIG"
                        and self.mode.building
                        and self.mode.building.project
                    ):
                        project = self.mode.building.project
                        extra = [
                            {
                                "target_id": "project:" + str(i),
                                "item_id": r["item_id"],
                                "name": r["item_id"],
                                "position": r["target"],
                                "historical": True,
                            }
                            for i, r in enumerate(project.completed)
                            if r.get("target") and (not r.get("removed"))
                        ]
                    shape = (
                        await semantic.dig_scope(
                            self.r.controller,
                            update.text,
                            snapshot,
                            previous,
                            reasoning=self.mode.reasoning,
                        )
                        if kind == "DIG"
                        else {}
                    )
                    if shape.get("question"):
                        resolved = {"question": shape["question"]}
                    elif shape.get("corridor"):
                        resolved = {
                            "scope": {
                                "kind": "DIG",
                                "targets": [],
                                "corridor": shape["corridor"],
                            }
                        }
                        self.r.emit(
                            "voxel.task.scope_resolved",
                            scope=resolved["scope"],
                            judgment=shape["judgment"],
                        )
                    else:
                        if shape.get("shape") == "FOCUS":
                            selected = await semantic.select_single_target(
                                self.r.controller,
                                update.text,
                                snapshot,
                                previous,
                                extra,
                            )
                        else:
                            selected = await semantic.select_targets(
                                self.r.controller,
                                update.text,
                                snapshot,
                                previous,
                                extra,
                            )
                        uncertain = any(
                            (
                                v.get("choice") in {"YES", "UNCLEAR"}
                                and v.get("confidence", 1) < 0.75
                                for v in selected.get("judgments", {}).values()
                            )
                        )
                        if uncertain or selected.get("ambiguous"):
                            selected = await self.review_targets(
                                update.text, snapshot, previous, extra, selected
                            )
                        resolved = {"selected": selected}
                self.r.post(
                    "voxel.task.planned",
                    {
                        "generation": gen,
                        "resolved": resolved,
                        "origin": origin,
                        "source": update.utterance_id,
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.r.post(
                    "voxel.task.planned",
                    {
                        "generation": gen,
                        "error": str(error)[:180]
                        if isinstance(error, ValueError)
                        else type(error).__name__,
                    },
                )

        async def bounded_plan():
            try:
                async with asyncio.timeout(
                    55 if kind in {"AGENT", "TRAVEL", "CRAFT", "PLACE"} else 20
                ):
                    await plan()
            except TimeoutError:
                self.r.post(
                    "voxel.task.planned",
                    {"generation": gen, "error": "planning_timeout"},
                )

        self.retry_plan = bounded_plan
        self.job = self.r.spawn(bounded_plan())
        self.emit()
        return True

    async def review_targets(self, text, s, previous, extra, initial):
        observed = s["view"].get("survival") or {}
        options = {
            r["target_id"]: r
            for r in [
                *(observed.get("nodes") or []),
                *(observed.get("entities") or []),
                *extra,
            ]
        }
        compact = {
            k: {
                f: v
                for f, v in r.items()
                if f in {"name", "item_id", "kind", "position", "focused", "historical"}
            }
            for k, r in options.items()
        }
        async with self.r.worker_slots, asyncio.timeout(7):
            reply = await self.mode.reasoning.complete(
                [
                    {
                        "role": "system",
                        "content": "Resolve only target references for a human-authorized game task. Return JSON {targets:[exact target_id],clear:boolean}. Match material/type/location and explicit exceptions. A request for both/all includes all matching objects. Do not choose protected or unrelated targets as substitutes. Historical project coordinates identify intended targets, not current visibility. No tools, execution or new coordinates.",
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "request": text,
                                "previous": semantic.brief(previous),
                                "candidates": compact,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                json_mode=True,
                max_tokens=1800,
                reasoning_effort="none",
            )
        choice = reply["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ValueError(
                "Target review incomplete: " + str(choice.get("finish_reason"))
            )
        v = json.loads(choice["message"]["content"])
        if (
            type(v.get("clear")) is not bool
            or not isinstance(v.get("targets"), list)
            or (not set(v["targets"]) <= options.keys())
        ):
            raise ValueError("Invalid target review")
        self.r.emit(
            "voxel.task.target_reviewed", targets=v["targets"], clear=v["clear"]
        )
        return {
            **initial,
            "ambiguous": not v["clear"],
            "targets": [
                {
                    "key": (
                        "E"
                        if "kind" in options[i] and "target_id" in options[i]
                        else "N"
                    )
                    + str(n),
                    **options[i],
                }
                for n, i in enumerate(v["targets"])
            ],
        }

    async def plan_craft(self, text, s, previous, discussion=None):
        if (
            not self.bridge.catalog_total
            or len(self.bridge.recipe_catalog) < self.bridge.catalog_total
        ):
            await self.bridge.load_recipes()
        catalog = list(self.bridge.recipe_catalog.values())
        from .outcomes import relevant_products

        products = await relevant_products(self.mode, {"request": text}, s)
        from .judgment import bounded_choice

        reference = await bounded_choice(
            self.r.controller,
            "voxel_craft_product_reference",
            {
                "current_request": text,
                "delivered_explanation": (discussion or {}).get("presented_text"),
                "prior_craft": semantic.brief(previous)
                if previous and previous.get("kind") == "CRAFT"
                else None,
            },
            "Which portable product does the CURRENT user ask you to craft? Resolve omitted nouns using the delivered explanation: when a station is explained by an example tool it can make, the tool is the output and the station is the means. Ingredients and descriptions of alternative grids do not make them equally requested products. Never select the station just because it was the subject of the earlier explanation. An explicitly named current product wins. Choose AMBIGUOUS only if multiple equally intended outputs remain.",
            {
                "AMBIGUOUS": "No uniquely intended output; ask a concrete choice of products",
                **products,
            },
            deadline_ms=1800,
        )
        ref = reference["action"]
        selected_product = (
            ref["choice"]
            if ref["confidence"] >= 0.7 and ref["choice"] in products
            else None
        )
        async with self.r.worker_slots, asyncio.timeout(30):
            response = await self.mode.reasoning.complete(
                [
                    {
                        "role": "system",
                        "content": "Resolve a VoxeLibre crafting request. Read-only JSON {item_id,quantity,question}. Choose item_id ONLY from products keys. quantity is requested output units (1..64), default one native recipe batch if no amount specified. Resolve ASR and omitted nouns from the actual delivered_explanation and prior_task. In an explanation, the prominently recommended end-product is the referent; background ingredients/stations are not competing requested products. Do not ask for a name already supplied by that reference. If several equally prominent products really remain ambiguous, ask ONE concrete Chinese choice naming them, never a generic what-do-you-want question. Then item_id=null. Do not invent recipes or execute.",
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "request": text,
                                "delivered_explanation": (discussion or {}).get(
                                    "presented_text"
                                ),
                                "prior_task": semantic.brief(previous),
                                "products": {
                                    selected_product: products[selected_product]
                                }
                                if selected_product
                                else products,
                                "jev_product_reference": ref,
                                "inventory": semantic.context(s)["inventory"],
                                "recipe_output_counts": {
                                    r["item_id"]: r["count"]
                                    for r in catalog
                                    if r["item_id"]
                                    in (
                                        {selected_product}
                                        if selected_product
                                        else products
                                    )
                                },
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                json_mode=True,
                max_tokens=550,
                reasoning_effort="none",
                planning=True,
            )
        choice = response["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ValueError("Craft request incomplete")
        v = json.loads(choice["message"]["content"])
        if v.get("question"):
            return {"question": str(v["question"])[:180]}
        if (
            v.get("item_id") not in products
            or type(v.get("quantity")) is not int
            or (not 1 <= v["quantity"] <= 64)
        ):
            raise ValueError("Invalid craft item/amount")
        recipes = [r for r in catalog if r["item_id"] == v["item_id"]]
        verified = await self.r.controller.judge_typed(
            "voxel_craft_request",
            {
                "request": text,
                "delivered_explanation": (discussion or {}).get("presented_text"),
                "proposed_item": products[v["item_id"]],
                "new_units": v["quantity"],
                "prior_task": semantic.brief(previous)
                if previous and previous.get("kind") == "CRAFT"
                else None,
            },
            {
                "conflict": {
                    "type": "noul",
                    "instructions": "Does proposed_item/new_units contradict the current crafting request after resolving omitted nouns from the delivered explanation? Its prominent end-product is a valid reference; background ingredients/stations are not competing requested products. Unspecified amount permits one recipe batch. YES only for actual wrong product/amount, not several ways of saying the same request.",
                },
                "unresolved": {
                    "type": "noul",
                    "instructions": "After using the actual delivered explanation, are there multiple equally prominent intended END PRODUCTS and no basis to choose? Ingredients and background examples of other crafting grids are not equal alternatives to the described tool. An omitted noun with a clear referent is NO.",
                },
            },
            deadline_ms=2000,
        )
        if (
            verified["conflict"]["noul"] >= 0.45
            or verified["unresolved"]["noul"] >= 0.6
        ):
            from .judgment import choose

            check = await choose(
                self.mode.reasoning,
                "Verify the proposed crafting item against the actual delivered explanation and current request. Its prominently described end-product may supply an omitted noun. Ingredients/stations mentioned in background do not compete with that product. ACCEPT if the reference is resolved; otherwise ASK with one Chinese question naming the concrete alternatives.",
                {
                    "request": text,
                    "explanation": (discussion or {}).get("presented_text"),
                    "product": products[v["item_id"]],
                    "count": v["quantity"],
                    "jev": verified,
                },
                {
                    "ACCEPT": "Product and amount follow the user request",
                    "ASK": "Object genuinely ambiguous; state concrete alternatives",
                },
            )
            if check["choice"] != "ACCEPT":
                return {"question": check["reason"]}
        a = await self.r.controller.judge_typed(
            "voxel_craft_recipe",
            {
                "item": products[v["item_id"]],
                "inventory": semantic.context(s)["inventory"],
            },
            {
                "recipe": {
                    "type": "choice",
                    "instructions": "Choose the registered recipe variant fitting current materials. No invented recipe. If materials missing select a relevant variant to report the deficit.",
                    "criteria": {
                        r["recipe_id"]: {
                            "ingredients": r["ingredients"],
                            "count": r["count"],
                            "grid": r["grid"],
                        }
                        for r in recipes[:8]
                    },
                }
            },
            deadline_ms=2000,
        )
        recipe = next((r for r in recipes if r["recipe_id"] == a["recipe"]["choice"]))
        if v["quantity"] % recipe["count"]:
            rounded = (
                (v["quantity"] + recipe["count"] - 1)
                // recipe["count"]
                * recipe["count"]
            )
            return {
                "question": f"这个配方一次产出{recipe['count']}个，无法正好合成{v['quantity']}个。做{rounded}个可以吗？"
            }
        return {
            "item_id": v["item_id"],
            "quantity": v["quantity"],
            "recipes": [a["recipe"]["choice"]],
        }

    def begin(self, origin):
        if (
            not self.task
            or not self.task["authorized"]
            or self.task["status"] == "CANCELLED"
        ):
            return
        if self.task.get("status") == "ARMING":
            return
        if self.task.get("pending_question") or not self.scope_ready(self.task):
            self.task["status"] = "NEEDS_INPUT"
            self.task["authorized"] = False
            self.task["pending_question"] = (
                self.task.get("pending_question")
                or "还没有明确操作目标，请把准星对准目标，或说明要去哪里。"
            )
            self.say(self.task["pending_question"])
            self.emit()
            return
        if self.task.get("generation") != self.bridge.generation:
            self.task["authorized"] = False
            self.task["status"] = "READY"
            self.say("操作状态改变了，任务保留，准备好后让我继续。")
            self.emit()
            return
        if not self.bridge.snapshot["capabilities"].get("survival_tasks"):
            self.say("当前执行器未启用，目标已记住。")
            return
        if not self.bridge.current(origin) or self.bridge.input_mode not in {
            "look",
            "watch",
        }:
            if (
                self.bridge.input_owner
                and self.bridge.input_mode == "watch"
                and (time.monotonic() - self.bridge.input_seen < 1.2)
                and (not self.bridge.fresh())
            ):
                self.task["status"] = "READY"
                return
            self.task["status"] = "READY"
            self.task["authorized"] = False
            self.say("任务已准备好，进入游戏或开启旁观后让我继续。")
            self.emit()
            return
        gen = self.generation
        task = self.task
        task.setdefault("deadline", time.monotonic() + task["duration_ms"] / 1000)
        task["duration_ms"] = max(0, int((task["deadline"] - time.monotonic()) * 1000))
        if task["duration_ms"] < 1000:
            self.stop("task_deadline")
            self.say("约定时间到了，进度已保留。")
            self.emit()
            return
        task["status"] = "ARMING"
        task.setdefault("arming_started", time.monotonic())
        if task.get("progress"):
            task["progress"]["last_effect_at"] = time.monotonic()
        self.emit()

        async def arm():
            try:
                current_origin = origin
                for attempt in range(3):
                    r = await self.bridge.arm_task(task, current_origin)
                    if (
                        r.get("reason")
                        not in {"native_client_not_ready", "source_expired"}
                        or gen != self.generation
                        or (not self.bridge.current(origin))
                    ):
                        break
                    await asyncio.sleep(0.25)
                    origin2 = self.bridge.origin(task["source"])
                    if not self.bridge.current(origin2):
                        break
                    current_origin = origin2
                self.r.post("voxel.task.armed", {"generation": gen, "result": r})
            except Exception as e:
                self.r.post(
                    "voxel.task.armed",
                    {
                        "generation": gen,
                        "result": {"status": "REJECTED", "reason": type(e).__name__},
                    },
                )

        self.job = self.r.spawn(arm())

    async def begin_after_revoke(self, source):
        generation = self.generation
        sequence = self.bridge.snapshot["snapshot_seq"]
        deadline = time.monotonic() + 6
        while self.generation == generation and time.monotonic() < deadline:
            if (
                self.bridge.snapshot["snapshot_seq"] > sequence + 1
                and self.bridge.fresh()
                and (not self.pending)
            ):
                self.begin(self.bridge.origin(source))
                return
            await asyncio.sleep(0.05)
        if self.generation == generation and self.task:
            self.task["status"] = "PAUSED"
            self.task["authorized"] = False
            self.say("控制状态暂时未同步，任务已保留。")
            self.emit()

    async def on_snapshot(self, s):
        await self.bind(s)
        self.activity()
        for row in s["view"].get("survival", {}).get("item_help", []):
            self.item_knowledge[row["item_id"]] = row
        for row in s["view"].get("survival", {}).get("item_facts", []):
            self.native_item_memory[row["item_id"]] = row
        for row in s["view"].get("survival", {}).get("entities", []):
            self.entity_memory[row["target_id"]] = {
                **row,
                "last_seen": time.monotonic(),
            }
        self.entity_memory = {
            k: v
            for k, v in self.entity_memory.items()
            if time.monotonic() - v["last_seen"] < 180
        }
        own = s["view"]["self"]
        position = own.get("position")
        if position is None:
            return
        import math

        key = f"{round(position['x'] / 3)},{round(position['z'] / 3)}"
        direction = round(own.get("yaw", 0) % (2 * math.pi) / (math.pi / 2)) % 4
        views = self.search_views.setdefault(key, [])
        if direction not in views:
            views.append(direction)
        if len(self.search_views) > 128:
            self.search_views.pop(next(iter(self.search_views)))
        for row in s["view"].get("survival", {}).get("nodes", []):
            if str(row["position"]) not in self.removed_resources:
                self.observation_memory[str(row["position"])] = {
                    **row,
                    "last_observed": time.time(),
                    "historical": True,
                }
            if str(row["position"]) not in self.removed_resources and any(
                (w in row["item_id"] for w in ("tree", "wood", "stone", "cobble"))
            ):
                self.resource_memory[str(row["position"])] = {
                    "item_id": row["item_id"],
                    "position": row["position"],
                }
            if row["item_id"] == "mcl_crafting_table:crafting_table":
                self.facilities[str(row["position"])] = {
                    "item_id": row["item_id"],
                    "position": row["position"],
                    "evidence": "observed",
                    "last_seen": time.time(),
                }
            elif str(row["position"]) in self.facilities:
                self.facilities.pop(str(row["position"]), None)
        if len(self.observation_memory) > 256:
            self.observation_memory = dict(
                sorted(
                    self.observation_memory.items(), key=lambda r: r[1]["last_observed"]
                )[-256:]
            )
        t = self.task
        if (
            t
            and t.get("authorized")
            and (time.monotonic() >= t.get("deadline", float("inf")))
        ):
            self.stop("task_deadline")
            self.say("约定的代打时间到了，进度已保留，现在由你操作。")
            self.emit()
            await self.save("task_deadline")
            return
        if t and t["status"] == "RUNNING" and t.get("start_position"):
            from .locomotion import observe

            observe(t, semantic.context(s), time.monotonic())
        if (
            t
            and t["status"] == "READY"
            and t.get("authorized")
            and (not self.job)
            and self.bridge.fresh()
            and (not holds_actions(self.mode))
        ):
            self.begin(self.bridge.origin(t["source"]))
        if not t or t["status"] != "RUNNING":
            return
        if t["kind"] in {"AGENT", "DIG", "CRAFT"} and self.bridge.task_current():
            p = s["view"]["self"]
            previous = getattr(self.mode, "previous", None)
            danger = p.get("head_medium") == "drowning_medium" or (
                previous and p.get("hp", 20) < previous["view"]["self"].get("hp", 20)
            )
            if (
                danger
                and (not self.emergency_job)
                and (time.monotonic() - self.emergency_checked > 1)
            ):
                self.emergency_checked = time.monotonic()
                gen = self.generation

                async def check():
                    try:
                        moves = s["view"].get("survival", {}).get("travel", [])
                        enemies = s["view"].get("survival", {}).get("entities", [])
                        options = {
                            str(i): {
                                **m,
                                "enemy_distance": min(
                                    (
                                        sum(
                                            (
                                                (m["target"][k] - e["position"][k]) ** 2
                                                for k in ("x", "y", "z")
                                            )
                                        )
                                        ** 0.5
                                        for e in enemies
                                    ),
                                    default=None,
                                ),
                            }
                            for i, m in enumerate(moves)
                        }
                        a = await self.r.controller.judge_typed(
                            "voxel_active_survival_interrupt",
                            {
                                "request": t["request"],
                                "player": {
                                    k: p.get(k) for k in ("hp", "breath", "head_medium")
                                },
                                "visible_threats": s["view"]
                                .get("survival", {})
                                .get("entities", []),
                            },
                            {
                                "response": {
                                    "type": "choice",
                                    "instructions": "The assistant already controls the player for this task. Should it interrupt slow work to get air or evade current danger? AIR if submerged losing breath; EVADE if being attacked without time to finish. CONTINUE if no immediate threat.",
                                    "criteria": {
                                        "AIR": "Surface before continuing",
                                        "EVADE": "Retreat before continuing",
                                        "CONTINUE": "Continue goal",
                                    },
                                },
                                "move": {
                                    "type": "choice",
                                    "instructions": "Assuming immediate survival action is needed: underwater choose SWIM_UP; for an enemy choose movement with largest enemy_distance. No further planning delay.",
                                    "criteria": {
                                        "NONE": "No available escape motion",
                                        **options,
                                    },
                                },
                            },
                            deadline_ms=1200,
                        )
                        self.r.post(
                            "voxel.task.emergency",
                            {"generation": gen, "answer": a, "moves": moves},
                        )
                    except Exception:
                        pass
                    finally:
                        self.emergency_job = None

                self.emergency_job = self.r.spawn(check())
        if self.bridge.task_grant and self.bridge.fresh() is False:
            return
        if not self.bridge.task_current():
            continuity = self.bridge.continuity(self.bridge.task_grant)
            if continuity == "wait":
                return
            if continuity == "rearm":
                if self.pending:
                    return
                if self.step_job:
                    self.step_job.cancel()
                    self.step_job = None
                if self.job:
                    self.job.cancel()
                    self.job = None
                self.generation += 1
                t.pop("completion_pending", None)
                t.pop("pending_outcome_plan", None)
                t["source"] = (
                    t["source"][:55].split("-sync-")[0] + "-sync-" + uuid4().hex[:8]
                )
                t["status"] = "READY"
                self.bridge.task_grant = None
                self.r.emit(
                    "voxel.task.resuming",
                    task_id=t["id"],
                    reason="native_transport_restored",
                    deadline=t.get("deadline"),
                )
                if not holds_actions(self.mode):
                    self.begin(self.bridge.origin(t["source"]))
                return
            timed_out = (
                time.monotonic() >= t.get("deadline", float("inf"))
                or (s.get("control", {}).get("last_revocation") or {}).get("reason")
                == "task_deadline"
            )
            self.stop("task_deadline" if timed_out else "task_ended")
            if timed_out:
                self.say("约定的代打时间到了，进度已保留，现在由你操作。")
            self.emit()
            await self.save("control_ended")
            return
        native = s["view"].get("survival", {}).get("task")
        if not native or native.get("grant_id") != self.bridge.task_grant["id"]:
            return
        if t["kind"] == "DIG":
            absent = {
                tuple((r["position"][k] for k in ("x", "y", "z")))
                for r in native.get("target_states", [])
                if r.get("reason") == "target_changed"
            }
            if absent:
                t["scope"]["targets"] = [
                    r
                    for r in t["scope"]["targets"]
                    if tuple((r["position"][k] for k in ("x", "y", "z"))) not in absent
                ]
        t["defeated"] = list(
            set(t.get("defeated", [])) | set(native.get("defeated") or [])
        )
        if (
            self.pending
            or self.step_job
            or holds_actions(self.mode)
            or (time.monotonic() < t.get("decision_retry_at", 0))
        ):
            return
        if t["kind"] in {"AGENT", "TRAVEL"}:
            await self.agent_snapshot(s, native)
            return
        if (
            t["kind"] == "COMBAT"
            and t["stop_kind"] == "GOAL"
            and t["scope"].get("entities")
            and (set(t["scope"]["entities"]) <= set(t.get("defeated", [])))
        ):
            await self.complete()
            return
        if native.get("path", {}).get("complete") and t["scope"].get("corridor"):
            await self.complete()
            return
        path = native.get("path") or {}
        if (
            t["scope"].get("corridor")
            and path.get("floor_gap")
            and (not any((a["kind"] == "DIG" for a in native.get("actions", []))))
        ):
            self.stop("observed_passage_gap")
            self.say(
                "挡路的方块已处理到这里，前方没有可站立的地面。我先停在实地上；继续过去需要另行搭支撑或换路线。"
            )
            self.emit()
            await self.save("observed_passage_gap")
            return
        if (
            t["quantity"]
            and t["completed"] >= t["quantity"]
            or (
                t["kind"] == "DIG"
                and (not t["scope"]["targets"])
                and (not t["scope"].get("corridor"))
            )
        ):
            await self.complete()
            return
        actions = native.get("actions") or []
        if not actions:
            self.missing_since = self.missing_since or time.monotonic()
            if time.monotonic() - self.missing_since > 3 and native.get(
                "blocked_reason"
            ):
                self.stop("missing_requirement")
                self.say(
                    cue=self.cue("说明当前阻碍和缺少的材料或工作台，给一个具体下一步。")
                )
                self.emit()
            return
        self.missing_since = None
        if time.monotonic() - self.last_step < 0.2:
            return
        gen = self.generation
        task = copy.deepcopy(t)
        snapshot = copy.deepcopy(s)

        async def decide():
            try:
                a = await semantic.choose_step(
                    self.r.controller, snapshot, task, actions, self.recent
                )
                self.r.post(
                    "voxel.task.decision",
                    {"generation": gen, "answer": a, "actions": actions},
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.r.post(
                    "voxel.task.decision",
                    {"generation": gen, "error": type(e).__name__},
                )

        self.last_step = time.monotonic()
        self.step_job = self.r.spawn(decide())

    async def complete(self):
        if self.task.get("outcome") and self.task.get("stop_kind") in {
            "TIMED",
            "ONGOING",
        }:
            self.task["completed_milestones"] = (
                self.task.get("completed_milestones", [])
                + [copy.deepcopy(self.task["outcome"])]
            )[-8:]
            self.task["milestone_complete"] = True
            self.replan_outcome(
                "The current milestone is achieved; choose a new useful outcome within the still-active timed/ongoing user delegation."
            )
            return
        if self.paused_goal and self.bridge.task_current():
            old = self.paused_goal
            self.paused_goal = None
            deadline = self.bridge.task_grant["deadline"]
            self.stop("rescue_complete")
            old.update(
                status="READY",
                authorized=True,
                generation=self.bridge.generation,
                duration_ms=max(1000, int((deadline - time.monotonic()) * 1000)),
                source=old["source"] + "-after-rescue",
            )
            self.task = old
            self.r.spawn(self.begin_after_revoke(old["source"]))
            self.say("已经脱险，继续刚才的任务。")
            self.emit()
            return
        self.task["status"] = "COMPLETED"
        self.task["authorized"] = False
        had_grant = bool(self.bridge.task_grant)
        self.stop("task_completed")
        if not had_grant:
            self.bridge.revoke("task_completed")
        outcome = self.task.get("outcome")
        self.say(
            (
                outcome["objective"].rstrip("。") + "，已完成。"
                if outcome
                else "这项任务已完成。"
            )
            + "接下来由你操作。"
        )
        self.emit()
        await self.save("task_completed")

    async def handle(self, kind, p):
        if not kind.startswith("voxel.task."):
            return False
        if kind == "voxel.task.result":
            self.pending = False
            if (
                p["generation"] != self.generation
                and self.task
                and (p.get("task_id") == self.task["id"])
                and (p["result"].get("status") == "SUCCEEDED")
            ):
                self.apply_receipt(p["action"], p["result"])
                await self.save(p["result"].get("command_id", "late_receipt"))
                self.emit()
        if p["generation"] != self.generation:
            return True
        t = self.task
        if kind == "voxel.task.harvest_target":
            self.step_job = None
            t.pop("harvested_source", None)
            if not self.bridge.task_current():
                return True
            if p.get("target"):
                t["outcome"]["target"] = p["target"]
                t.pop("strategy_invalid", None)
                self.r.emit("voxel.outcome.drop_target", target=p["target"])
            else:
                t["outcome"]["target"] = None
                t.pop("strategy_invalid", None)
            return True
        if kind == "voxel.task.outcome_review":
            self.step_job = None
            if p.get("error"):
                self.r.emit("voxel.outcome.review_failed", reason=p["error"])
                t["outcome_review_at"] = time.monotonic()
                return True
            if not self.bridge.task_current():
                return True
            result = p["result"]
            t["outcome_review_at"] = time.monotonic()
            t["outcome_progress"] = result
            if result.get("needs_completion_review"):
                self.review_completion()
                return True
            if result["complete"]:
                await self.complete()
            elif result["replan"]:
                self.replan_outcome(
                    "current approach no longer useful or observable progress has stalled"
                )
            return True
        if kind == "voxel.task.completion_review":
            self.job = None
            t.pop("completion_pending", None)
            if not self.bridge.task_current():
                return True
            if p.get("error"):
                self.r.emit("voxel.outcome.completion_review_failed", reason=p["error"])
                t["completion_wait_until"] = time.monotonic() + 2
                t["completion_retry"] = True
                return True
            if p["result"]["choice"] == "COMPLETE":
                await self.complete()
            else:
                t["outcome_review_at"] = 0
                self.replan_outcome(
                    "the final semantic review found an explicit user requirement still missing"
                )
            return True
        if kind == "voxel.task.outcome_plan":
            if self.pending or self.step_job:
                t["pending_outcome_plan"] = p
                return True
            self.job = None
            if "error" in p:
                t["outcome_plan_errors"] = t.get("outcome_plan_errors", 0) + 1
                t["outcome_replan_after"] = time.monotonic() + min(
                    8, 2 ** min(t["outcome_plan_errors"], 3)
                )
                self.r.emit(
                    "voxel.outcome.replan_failed",
                    reason=p["error"],
                    attempt=t["outcome_plan_errors"],
                )
                if t["outcome_plan_errors"] == 2:
                    self.say("下一步的规划暂时慢了些，我会自动重试，目标和进度保留着。")
                    self.emit()
                return True
            if not self.bridge.task_current():
                return True
            t.pop("strategy_invalid", None)
            from .outcomes import recipe_scope

            previous = t.get("outcome") or {}
            t["outcome"] = p["outcome"]
            t["outcome_review_at"] = time.monotonic()
            t["outcome_review_version"] = t.get("effect_version", 0)
            if t.get("milestone_complete"):
                t["baseline_inventory"] = semantic.context(self.bridge.snapshot)[
                    "inventory"
                ]
                t["completed_milestone_effects"] = (
                    t.get("completed_milestone_effects", []) + t.get("effects", [])
                )[-192:]
                t["effects"] = []
                t["defeated"] = []
                t.pop("outcome_review_inventory", None)
            t.pop("milestone_complete", None)
            if (
                not t["outcome"].get("target")
                and previous.get("target")
                and previous["target"].get("last_seen")
                and previous["target"].get("relation")
            ):
                t["outcome"]["target"] = {**previous["target"], "historical": True}
            t["agent_goal"] = "OUTCOME"
            t.pop("agenda", None)
            t.pop("stage_goal", None)
            t["outcome_plan_errors"] = 0
            t["navigation"] = {}
            t["outcome_replan_after"] = time.monotonic() + 5
            recipes = recipe_scope(self.mode, t["outcome"])
            if (
                recipes != t["scope"].get("recipes")
                or t["outcome"].get("focus_items") != t["scope"].get("focus_items")
                or (not t["scope"].get("model_outcome"))
            ):
                t["scope"].update(
                    recipes=recipes,
                    model_outcome=True,
                    focus_items=t["outcome"].get("focus_items", []),
                )
                t["status"] = "READY"
                t["source"] = (
                    t["source"].split("-outcome-")[0] + "-outcome-" + uuid4().hex[:8]
                )
                self.bridge.task_grant = None
                self.begin(self.bridge.origin(t["source"]))
            await self.save(t["source"])
            self.emit()
            return True
        if kind == "voxel.task.outcome_decision":
            self.step_job = None
            if p.get("error"):
                return await self.handle("voxel.task.decision", p)
            if not self.bridge.task_current():
                return True
            if p["answer"].get("tracked_target"):
                t["outcome"]["target"] = p["answer"]["tracked_target"]
            if p["answer"].get("recovery"):
                self.r.emit(
                    "voxel.outcome.step_recovery", decision=p["answer"]["recovery"]
                )
                t["local_loop"] = False
            choice = p["answer"]["action"]["choice"]
            if choice == "REPLAN":
                self.replan_outcome(
                    "the legal next-step judgment found no productive action under this strategy"
                )
                return True
            if choice == "WAIT":
                t["outcome_waits"] = t.get("outcome_waits", 0) + 1
                if t["outcome_waits"] >= 3:
                    self.replan_outcome("repeated waiting without a useful next action")
                return True
            t["outcome_waits"] = 0
            return await self.handle("voxel.task.decision", p)
        if kind == "voxel.task.site":
            self.step_job = None
            if p.get("error"):
                self.r.emit("voxel.site.failed", reason=p["error"])
                return True
            if not self.bridge.task_current():
                return True
            v = p["result"]
            t["site_strategy"] = v
            t["site_region"] = v.get("region")
            t["site_alter"] = v.get("alter", False)
            t["site_checked"] = time.monotonic()
            project = self.mode.building.project
            project.preparation = {"phase": v["method"], "region": v.get("region")}
            self.mode.building.emit()
            self.r.emit("voxel.site.selected", result=v)
            region = v.get("region")
            from .site import working_region

            box = working_region(region) if region and v.get("alter") else None
            if box != t["scope"].get("site_box"):
                t["scope"] = (
                    {**t["scope"], "site_box": box}
                    if box
                    else {k: v for k, v in t["scope"].items() if k != "site_box"}
                )
                t.update(
                    status="READY",
                    source=t["source"] + "-region",
                    duration_ms=max(
                        1000, int((t["deadline"] - time.monotonic()) * 1000)
                    ),
                )
                self.bridge.task_grant = None
                self.begin(self.bridge.origin(t["source"]))
            if v["method"] == "ASK":
                self.stop("site_requirements_conflict")
                self.say("场地选择涉及你指定的位置或保留要求，需要先确认怎样调整。")
            self.emit()
            return True
        if kind == "voxel.task.site_step":
            self.step_job = None
            if p.get("error"):
                self.r.emit("voxel.site.step_failed", reason=p["error"])
                return True
            if p["answer"]["action"]["choice"] == "REVISE":
                if t.get("site_repair"):
                    self.stop("construction_repair_blocked")
                    self.say(
                        "这处施工通路还没有打通，房屋和进度保留在原位；我需要重新检查可到达的施工面。"
                    )
                    self.emit()
                else:
                    t.pop("site_strategy", None)
                return True
            return await self.handle(
                "voxel.task.decision", {**p, "urgent_goal": t.get("urgent_goal")}
            )
        if kind == "voxel.task.summit":
            self.step_job = None
            if p.get("error"):
                return True
            if p["answer"]["arrived"]["noul"] >= 0.85:
                t["summit_confirmed"] = True
                if not t.get("agenda"):
                    await self.complete()
            return True
        if kind == "voxel.task.repair":
            self.step_job = None
            if p.get("error"):
                self.r.emit("voxel.task.repair_failed", reason=p["error"])
                return True
            if not self.bridge.task_current():
                return True
            choice = p["answer"]["action"]["choice"]
            t["navigation"] = {}
            self.r.emit(
                "voxel.task.progress_review",
                answer=p["answer"],
                stalled_s=t.get("stalled_s"),
            )
            if choice == "REPLAN" and t.get("agenda"):
                t["agenda_index"] = len(t["agenda"]["steps"])
                await self.next_agenda(self.bridge.snapshot)
                return True
            if choice in {"ASK", "REPLAN"} and t.get("recovery_attempts", 0) < 3:
                self.recover_goal(self.bridge.snapshot, p["actions"])
                return True
            if choice in {"ASK", "REPLAN"}:
                self.stop("observed_route_blocked")
                self.say(
                    cue=self.cue(
                        "任务没有完成。根据当前可见障碍、工具和尝试，解释具体卡在哪里，给出一个可行的后续选项。"
                    )
                )
                self.emit()
                return True
            await self.handle(
                "voxel.task.decision", {**p, "urgent_goal": t.get("urgent_goal")}
            )
            return True
        if kind == "voxel.task.recovery":
            self.job = None
            if p.get("error"):
                self.r.emit("voxel.task.recovery_failed", reason=p["error"])
                return True
            if not self.bridge.task_current():
                return True
            t["navigation"] = {}
            t["recovery"] = p["result"]
            t["recovery_attempts"] = t.get("recovery_attempts", 0) + 1
            if t.get("progress"):
                t["progress"]["last_effect_at"] = time.monotonic()
            self.r.emit(
                "voxel.task.recovery_plan", plan=p["result"], provider="reasoning"
            )
            choice = p["result"]["choice"]
            if choice == "ASK":
                self.stop("observed_route_blocked")
                self.say(
                    str(
                        p["result"].get("reason")
                        or "眼前这条路线还无法通过，需要换个方向。"
                    )[:180]
                )
                self.emit()
                return True
            if str(choice).isdigit():
                await self.handle(
                    "voxel.task.decision",
                    {
                        "generation": self.generation,
                        "answer": {"action": {"choice": choice}},
                        "actions": p["actions"],
                        "urgent_goal": t.get("urgent_goal"),
                    },
                )
            return True
        if kind == "voxel.task.replanned":
            self.job = None
            if p.get("error"):
                t["replan_errors"] = t.get("replan_errors", 0) + 1
                t["replan_after"] = time.monotonic() + min(
                    8, 2 ** min(t["replan_errors"], 3)
                )
                self.r.emit(
                    "voxel.task.replan_retry",
                    reason=p["error"],
                    attempt=t["replan_errors"],
                )
                if t["replan_errors"] == 1:
                    self.say(
                        "这一阶段完成了，下一阶段计划还在重试，代打时间和目标都保留着。"
                    )
                self.emit()
                return True
            if not self.bridge.task_current():
                return True
            remaining = int((t["deadline"] - time.monotonic()) * 1000)
            if remaining < 1000:
                self.stop("task_deadline")
                self.emit()
                return True
            t.update(
                agenda=p["agenda"],
                agenda_index=0,
                stage_started=time.monotonic(),
                stage_inventory=semantic.context(self.bridge.snapshot)["inventory"],
                stage_moves=t.get("moves", 0),
                stage_position=copy.deepcopy(
                    self.bridge.snapshot["view"]["self"]["position"]
                ),
                stage_cells=len(t.get("progress", {}).get("cells", [])),
                agent_goal=p["agenda"]["steps"][0]["goal"],
                duration_ms=remaining,
                source=t["source"] + "-next",
                status="READY",
                authorized=True,
                generation=self.bridge.generation,
            )
            t["scope"]["recipes"] = p["recipes"]
            t.pop("resource_goal_resolved", None)
            t["wanted_resources"] = []
            t.pop("replan_after", None)
            t.pop("replan_errors", None)
            t["navigation"] = {}
            self.bridge.task_grant = None
            self.say(p["agenda"]["summary"])
            self.r.emit("voxel.agenda", agenda=t["agenda"], task_id=t["id"])
            self.begin(self.bridge.origin(t["source"]))
            self.emit()
            return True
        if kind == "voxel.task.emergency":
            mode = p["answer"]["response"]["choice"]
            if mode != "CONTINUE" and self.bridge.task_current():
                if t["kind"] == "AGENT":
                    was_urgent = t.get("urgent_goal")
                    t["urgent_goal"] = "SURFACE" if mode == "AIR" else "EVADE"
                    choice = p["answer"].get("move", {}).get("choice", "NONE")
                    if choice.isdigit():
                        t["urgent_move"] = p["moves"][int(choice)]
                    if self.step_job:
                        self.step_job.cancel()
                        self.step_job = None
                    if self.pending and (not was_urgent):
                        self.r.spawn(self.bridge.display.release())
                        grant = self.bridge.task_grant
                        self.r.spawn(
                            self.bridge.request(
                                "TASK_CANCEL_STEP",
                                {"task_id": grant["task_id"], "grant_id": grant["id"]},
                                timeout=5,
                            )
                        )
                else:
                    old = copy.deepcopy(t)
                    remaining = max(
                        1000,
                        int(
                            (self.bridge.task_grant["deadline"] - time.monotonic())
                            * 1000
                        ),
                    )
                    self.stop("urgent_survival")
                    self.paused_goal = old
                    t = self.task = {
                        **old,
                        "id": uuid4().hex,
                        "kind": "TRAVEL",
                        "status": "READY",
                        "scope": {"kind": "TRAVEL"},
                        "agent_goal": "SURFACE" if mode == "AIR" else "EVADE",
                        "start_position": copy.deepcopy(
                            self.bridge.snapshot["view"]["self"]["position"]
                        ),
                        "duration_ms": remaining,
                        "authorized": True,
                        "generation": self.bridge.generation,
                        "source": old["source"] + "-rescue",
                    }
                    self.r.spawn(self.begin_after_revoke(t["source"]))
                    self.say("先脱离危险，原来的任务记着。")
                    self.emit()
            return True
        if kind == "voxel.task.resources":
            self.step_job = None
            if p.get("result"):
                t.update(p["result"])
                t["resource_goal_resolved"] = True
                t["resource_checked"] = time.monotonic()
            return True
        if kind == "voxel.task.craft_supply":
            self.step_job = None
            if p.get("result"):
                t["craft_supply"] = p["result"]
                t["craft_supply_for"] = p["item_id"]
                t["craft_supply_signature"] = p["signature"]
            return True
        if kind == "voxel.task.planned":
            self.job = None
            if p.get("error"):
                transient = p["error"] in {
                    "TimeoutError",
                    "planning_timeout",
                    "TypeSafeInternalServerError",
                    "TypeSafeAPIConnectionError",
                    "TypeSafeAPITimeoutError",
                    "TypeSafeRateLimitError",
                    "HTTPStatusError",
                }
                retry = self.retry_plan
                if (
                    transient
                    and retry
                    and t.get("authorized")
                    and (time.monotonic() < t.get("deadline", float("inf")))
                    and (t.get("generation") == self.bridge.generation)
                ):
                    t["plan_retries"] = t.get("plan_retries", 0) + 1
                    self.r.emit(
                        "voxel.task.plan_service_retry",
                        reason=p["error"],
                        attempt=t["plan_retries"],
                    )
                    if t["plan_retries"] == 2:
                        self.say(
                            "规划服务暂时没有返回，我会自动重试，目标和约定时间都保留着。"
                        )

                    async def again():
                        await asyncio.sleep(min(8, 2 ** min(t["plan_retries"] - 1, 3)))
                        await retry()

                    self.job = self.r.spawn(again())
                    return True
                t["status"] = "NEEDS_INPUT"
                t["authorized"] = False
                t["pending_question"] = (
                    "这次计划没生成成功，目标已保留。可以让我重试或换一个目标。"
                )
                self.r.emit("voxel.task.plan_failed", reason=p["error"])
                self.say(t["pending_question"])
                self.emit()
                return True
            result = p["resolved"]
            self.retry_plan = None
            if result.get("question"):
                t["pending_question"] = result["question"]
            elif result.get("design"):
                if self.mode.building:
                    from jarvis.core.schemas import TranscriptUpdate

                    update = TranscriptUpdate(
                        utterance_id=t["source"],
                        revision=1,
                        text=t["request"],
                        is_final=True,
                        audio_end_sample=1,
                    )
                    decision = {
                        "route": {"choice": "PROJECT_NEW", "confidence": 1},
                        "project_authorize": {"noul": 1 if t["authorized"] else 0},
                    }
                    self.stop("placement_layout")
                    self.emit()
                    await self.mode.building.apply_intent(
                        update,
                        decision,
                        self.bridge.origin(t["source"]),
                        self.bridge.snapshot,
                    )
                    return True
                t["pending_question"] = "当前还没有摆放方案，请说明要摆成什么形状。"
            elif result.get("scope"):
                t["scope"] = result["scope"]
                if result.get("kind_override"):
                    t["kind"] = result["kind_override"]
                    t["quantity"] = None
                for key in (
                    "agent_goal",
                    "outcome",
                    "autonomy",
                    "travel_distance",
                    "extent",
                    "navigation_authorized",
                    "completion_request",
                ):
                    if key in result:
                        t[key] = result[key]
                if result.get("outcome"):
                    t["outcome_review_at"] = time.monotonic()
                    t["outcome_spoken_at"] = time.monotonic()
                    target = t["outcome"].get("target")
                    if target and target.get("target_id") in self.entity_memory:
                        t["outcome"]["target"] = copy.deepcopy(
                            self.entity_memory[target["target_id"]]
                        )
                    self.say(result["outcome"]["strategy"])
                if result.get("agenda"):
                    t["agenda"] = result["agenda"]
                    t["agenda_index"] = 0
                    t["stage_started"] = time.monotonic()
                    t["stage_inventory"] = semantic.context(self.bridge.snapshot)[
                        "inventory"
                    ]
                    t["stage_dug"] = 0
                    t["stage_position"] = copy.deepcopy(
                        self.bridge.snapshot["view"]["self"]["position"]
                    )
                    t["stage_cells"] = 0
                    self.r.emit("voxel.agenda", agenda=t["agenda"], task_id=t["id"])
                    self.say(t["agenda"]["summary"])
                if t["scope"].get("corridor"):
                    t["quantity"] = None
                if t["kind"] == "PLACE":
                    t["item_id"] = result["item_id"]
                    t["quantity"] = result["quantity"]
            elif t["kind"] == "CRAFT":
                if result.get("question"):
                    t["pending_question"] = result["question"]
                else:
                    t["quantity"] = result["quantity"]
                    t["scope"] = {"kind": "CRAFT", "recipes": result["recipes"]}
                    t["item_id"] = result["item_id"]
            else:
                selected = result["selected"]
                rows = selected["targets"]
                if selected["ambiguous"] or (not rows and (not selected["hostiles"])):
                    t["pending_question"] = "请指明要操作的方块或生物，或把视线对准它。"
                elif t["kind"] == "DIG":
                    targets = [
                        {"position": r["position"], "item_id": r["item_id"]}
                        for r in rows
                        if r["key"].startswith("N")
                    ]
                    t["scope"] = {"kind": "DIG", "targets": targets[:128]}
                    t["quantity"] = min(t["quantity"] or len(targets), len(targets))
                    if not targets:
                        t["pending_question"] = (
                            "当前没找到符合描述的可见方块，请看向要拆的部分。"
                        )
                else:
                    ids = [r["target_id"] for r in rows if r["key"].startswith("E")]
                    t["scope"] = {
                        "kind": "COMBAT",
                        "entities": ids,
                        "hostiles": selected["hostiles"],
                    }
                    if not t["quantity"] and t["stop_kind"] == "GOAL":
                        t["quantity"] = None
            if t["pending_question"]:
                t["status"] = "NEEDS_INPUT"
                t["authorized"] = False
                self.say(t["pending_question"])
            else:
                t["status"] = "READY"
                if (
                    t["kind"] == "AGENT"
                    and t.get("agent_goal") == "MATERIALS"
                    and self.mode.building
                    and self.mode.building.project
                    and (self.mode.building.project.status != "CANCELLED")
                ):
                    project = self.mode.building.project
                    t["project_id"] = project.project_id
                    t["materials_needed"] = (project.blueprint or {}).get(
                        "materials", {}
                    )
                    if not project.blueprint and (
                        not self.mode.building.job or self.mode.building.job.done()
                    ):
                        project.authorization = {
                            "source": t["source"],
                            "duration_ms": t["duration_ms"],
                            "generation": self.bridge.generation,
                            "supply": True,
                        }
                        self.mode.building.design(t["source"], True)
                if t["authorized"]:
                    self.begin(self.bridge.origin(t["source"]))
                else:
                    self.say("目标已整理好，需要我动手时说开始即可。")
            await self.save(t["source"])
            self.emit()
        elif kind == "voxel.task.armed":
            self.job = None
            armed = p["result"].get("status") == "ARMED"
            t["status"] = (
                "RUNNING"
                if armed
                and (
                    self.bridge.task_current()
                    or self.bridge.continuity(self.bridge.task_grant)
                    in {"wait", "rearm"}
                )
                else "PAUSED"
            )
            if t["status"] == "RUNNING":
                self.failures = 0
                if (
                    not t.get("agenda")
                    and (not t.get("outcome"))
                    and (not t.get("start_announced"))
                ):
                    self.say("开始操作，你随时可以说“我自己来”。")
                    t["start_announced"] = True
            elif (
                p["result"].get("reason")
                in {"native_client_not_ready", "source_expired"}
                and t.get("generation") == self.bridge.generation
                and (time.monotonic() < t.get("deadline", 0))
            ):
                t["status"] = "READY"
            else:
                t["authorized"] = False
                reason = p["result"].get("reason", "unknown")
                self.r.emit(
                    "voxel.task.arm_failed", reason=reason, scope=t.get("scope")
                )
                text = {
                    "passage_origin_changed": "你的位置已离开刚才通道起点，原来的开路范围保留着；重新指向前方让我开路即可。",
                    "unobserved_target": "目标目前没有被观察到，请把视线转向要操作的位置。",
                    "invalid_scope": "任务目标还没明确，暂时不能执行。",
                    "source_changed": "刚才发生了手动接管，任务已保留。",
                }.get(reason, "执行器暂时没有确认开始，目标和进度都已保留。")
                self.say(text)
            self.emit()
        elif kind == "voxel.task.decision":
            self.step_job = None
            if p.get("error"):
                self.failures += 1
                self.r.emit("voxel.task.decision_failed", reason=p["error"])
                t.setdefault("decision_unavailable_since", time.monotonic())
                t["decision_retry_at"] = time.monotonic() + min(
                    5, 0.5 * 2 ** min(self.failures, 4)
                )
                if time.monotonic() - t["decision_unavailable_since"] >= 8 and (
                    not t.get("decision_retry_announced")
                ):
                    t["decision_retry_announced"] = True
                    self.say("判断服务暂时慢了些，我正在重试，会接着完成当前任务。")
                    self.emit()
                return True
            t.pop("decision_unavailable_since", None)
            t.pop("decision_retry_at", None)
            t.pop("decision_retry_announced", None)
            if not self.bridge.task_current() or holds_actions(self.mode):
                return True
            if t["kind"] == "AGENT" and p.get("urgent_goal") != t.get("urgent_goal"):
                return True
            choice = p["answer"]["action"]["choice"]
            if p["answer"].get("navigation") is not None:
                t["navigation"] = p["answer"]["navigation"]
            if choice == "WAIT":
                return True
            if choice == "DONE":
                defeated = set(t.get("defeated", []))
                targets = set(t["scope"].get("entities", []))
                if (
                    t["kind"] == "COMBAT"
                    and targets
                    and (targets <= defeated)
                    and (t["stop_kind"] == "GOAL")
                ):
                    await self.complete()
                return True
            if choice in {"ASK", "BLOCKED"}:
                if t["kind"] in {"AGENT", "TRAVEL"} and t.get("stalled_s", 0) < 15:
                    t.setdefault("navigation", {}).pop("waypoint", None)
                    return True
                self.stop("need_user_decision")
                self.say(cue=self.cue("说明阻碍，只问一个必要的问题。"))
                self.emit()
                return True
            if not choice.isdigit() or int(choice) >= len(p["actions"]):
                return True
            old = p["actions"][int(choice)]
            s = self.bridge.snapshot
            candidates = (s["view"].get("survival", {}).get("task") or {}).get(
                "actions"
            ) or []
            action = next((a for a in candidates if self.same_action(old, a)), None)
            if not action:
                self.r.emit("voxel.task.decision_stale", kind=old["kind"])
                return True
            action = {**action, "emergency": bool(t.get("urgent_goal"))}
            self.pending = True
            gen = self.generation
            task_id = t["id"]

            async def execute():
                try:
                    result = await self.bridge.task_step(action, s)
                except Exception as e:
                    result = {"status": "UNKNOWN", "reason": type(e).__name__}
                self.r.post(
                    "voxel.task.result",
                    {
                        "generation": gen,
                        "task_id": task_id,
                        "action": action,
                        "result": result,
                    },
                )

            self.r.spawn(execute())
        elif kind == "voxel.task.result":
            action = p["action"]
            result = p["result"]
            self.recent.append(
                {
                    "kind": action["kind"],
                    "target": action.get("target"),
                    "item_id": action.get("item_id"),
                    "slot": action.get("slot"),
                    "status": result.get("status"),
                    "reason": result.get("reason"),
                    "completed_after": t["completed"],
                    "position_before": result.get("position_before"),
                    "position_after": result.get("position"),
                }
            )
            t["attempts"] = (t.get("attempts", []) + [dict(self.recent[-1])])[-24:]
            self.r.emit("voxel.task.step", action=action, result=result)
            if result.get("status") == "SUCCEEDED":
                self.failures = 0
                self.last_effect_at = time.monotonic()
                self.apply_receipt(action, result)
                await self.save(result.get("command_id", "step"))
            else:
                if result.get("reason") in {
                    "survival_interrupt",
                    "voice_attention",
                    "user_speech",
                    "camera_alignment_pending",
                    "camera_not_aligned",
                }:
                    self.emit()
                    return True
                self.failures += 1
                recoverable = result.get("status") in {
                    "CANCELLED",
                    "REJECTED",
                } and self.bridge.continuity(self.bridge.task_grant) in {
                    "wait",
                    "rearm",
                }
                if recoverable:
                    return True
                if (
                    result.get("status") in {"UNKNOWN", "CANCELLED"}
                    or not self.bridge.task_current()
                ):
                    self.stop("execution_paused")
                elif t.get("outcome") and result.get("status") == "REJECTED":
                    if action["kind"] in {"MOVE", "JUMP_MOVE", "JUMP_PLACE", "SWIM"}:
                        t.setdefault("navigation", {}).pop("waypoint", None)
                        t["access_progress"] = {
                            "failed_target": action.get("target"),
                            "actual_position": result.get("position"),
                            "reason": result.get("reason"),
                            "repeated_failures": self.failures,
                        }
                    if self.failures >= 12:
                        self.stop("execution_blocked")
                        self.say(
                            "几种操作都没有推进，我先停下保留目标和材料；刚才卡住的位置和原因也已经记下。"
                        )
                    elif self.failures >= 3:
                        self.replan_outcome(
                            "Native rejected the attempted approach without an effect. Inspect actual positions and recent failures; choose different access/tooling, not the same failed movement."
                        )
                elif self.failures >= 3:
                    self.stop("execution_paused")
            self.emit()
        return True

    @staticmethod
    def same_action(old, new):
        keys = (
            "kind",
            "item_id",
            "target_id",
            "target_index",
            "recipe_id",
            "slot",
            "interaction",
            "above",
            "target_item_id",
        )
        if not all((new.get(k) == old.get(k) for k in keys)):
            return False
        if old["kind"] == "ATTACK":
            return True
        if old["kind"] == "LOOK":
            return (
                abs(old.get("yaw", 0) - new.get("yaw", 0)) < 0.01
                and abs(old.get("pitch", 0) - new.get("pitch", 0)) < 0.01
            )
        if old["kind"] in {"SWIM", "SWIM_UP"}:
            a = old.get("target", {})
            b = new.get("target", {})
            return all((old.get(k) == new.get(k) for k in ("dx", "dz"))) and all(
                (abs(a.get(k, 0) - b.get(k, 0)) < 0.25 for k in ("x", "z"))
            )
        return new.get("target") == old.get("target")

    def apply_receipt(self, action, result):
        t = self.task
        command = result.get("command_id")
        if command and command in t.get("receipts", []):
            return
        if command:
            t["receipts"] = (t.get("receipts", []) + [command])[-256:]
        from .outcomes import remember_effect

        remember_effect(t, action, result)
        from .locomotion import effect, cell

        effect(t, action, result, time.monotonic())
        if action["kind"] in {
            "DIG",
            "DIG_DOWN",
            "PLACE",
            "JUMP_PLACE",
            "ATTACK",
            "USE",
        }:
            t["completed"] += 1
        resource_ids = t.get("wanted_resources", []) + (
            t.get("craft_supply") or {}
        ).get("wanted_resources", [])
        if action["kind"] == "DIG" and action.get("item_id") in resource_ids:
            t["resources_dug"] = t.get("resources_dug", 0) + 1
            t.setdefault("collect_positions", []).append(action["target"])
            self.resource_memory.pop(str(action["target"]), None)
            self.removed_resources.add(str(action["target"]))
        if action["kind"] == "ATTACK" and result.get("defeated"):
            t.setdefault("defeated", []).append(action["target_id"])
        if (
            action["kind"] == "PLACE"
            and action.get("item_id") == "mcl_crafting_table:crafting_table"
        ):
            self.facilities[str(action["target"])] = {
                "item_id": action["item_id"],
                "position": action["target"],
                "evidence": "placement_receipt",
                "last_seen": time.time(),
            }
        if action["kind"] in {"DIG", "DIG_DOWN"} and action.get("target"):
            self.facilities.pop(str(action["target"]), None)
            self.observation_memory.pop(str(action["target"]), None)
            self.removed_resources.add(str(action["target"]))
        if action["kind"] == "CRAFT" and (not action.get("dependency_for")):
            t["completed"] += result.get("count", action["count"])
        if action["kind"] in {"DIG", "PLACE"} and "targets" in t["scope"]:
            t["scope"]["targets"] = [
                r for r in t["scope"]["targets"] if r["position"] != action["target"]
            ]
        if action["kind"] in {"MOVE", "JUMP_MOVE", "SWIM"}:
            t["moves"] = t.get("moves", 0) + 1
            p = result.get("position") or action.get("target") or {}
            key = cell(p)
            self.visited[key] = self.visited.get(key, 0) + 1
            if t.get("outcome") and self.visited[key] >= 3:
                t["local_loop"] = True
            if t.get("collect_positions"):
                t["collect_positions"] = [
                    r
                    for r in t["collect_positions"]
                    if (r["x"] - p.get("x", 0)) ** 2 + (r["z"] - p.get("z", 0)) ** 2
                    > 1.1
                ]
        if action["kind"] == "LOOK":
            t["looks"] = t.get("looks", 0) + 1
        if result.get("reached_air"):
            t["reached_air"] = True

    async def agent_snapshot(self, s, native):
        from .agency import select_step

        t = self.task
        game = semantic.context(s)
        p = game["player"]
        goal = t.get("agent_goal")
        start = t["start_position"]
        if t.get("outcome"):
            await self.outcome_snapshot(s, native)
            return
        if self.job and (not self.job.done()):
            return
        if goal == "PREPARE_SITE":
            await self.site_snapshot(s, native)
            return
        if t.get("agenda") and (not t.get("urgent_goal")):
            steps = t["agenda"]["steps"]
            index = t.get("agenda_index", 0)
            if index < len(steps):
                stage = steps[index]
                stage_goal = stage["goal"]
                amount = stage.get("quantity")
                baseline = t.get("stage_inventory", {})
                targets = t.get("wanted_resources", [])
                stock = stage.get("quantity_mode", "stock") == "stock"
                outputs = (
                    [
                        "mcl_core:cobble",
                        "mcl_core:stone",
                        "mcl_core:andesite",
                        "mcl_core:granite",
                        "mcl_core:diorite",
                    ]
                    if stage_goal == "FIND_STONE"
                    else targets
                )
                gained = sum(
                    (
                        max(
                            0,
                            game["inventory"].get(i, 0)
                            - (0 if stock else baseline.get(i, 0)),
                        )
                        for i in outputs
                    )
                )
                done = (
                    stage_goal in {"FIND_WOOD", "FIND_STONE"}
                    and amount
                    and (gained >= amount)
                )
                done = done or (
                    stage_goal == "CRAFT"
                    and game["inventory"].get(stage.get("item_id"), 0)
                    >= (0 if stock else baseline.get(stage.get("item_id"), 0))
                    + (amount or 1)
                )
                done = done or (
                    stage_goal == "PLACE_BENCH" and game.get("workbench") is not None
                )
                stage_start = t.get("stage_position", start)
                stage_distance = math.hypot(
                    p["position"]["x"] - stage_start["x"],
                    p["position"]["z"] - stage_start["z"],
                )
                new_cells = len(t.get("progress", {}).get("cells", [])) - t.get(
                    "stage_cells", 0
                )
                done = done or (
                    stage_goal == "EXPLORE"
                    and stage_distance >= max(6, amount or 10)
                    and (new_cells >= 6)
                )
                done = done or (
                    stage_goal == "ASCEND" and t.get("summit_confirmed", False)
                )
                if done:
                    t["completed_stages"] = (
                        t.get("completed_stages", [])
                        + [
                            {
                                "goal": stage_goal,
                                "label": stage["label"],
                                "inventory": game["inventory"],
                                "position": p["position"],
                            }
                        ]
                    )[-16:]
                    t["agenda_index"] = index + 1
                    t["stage_inventory"] = game["inventory"]
                    t["stage_moves"] = t.get("moves", 0)
                    t["stage_position"] = copy.deepcopy(p["position"])
                    t["stage_cells"] = len(t.get("progress", {}).get("cells", []))
                    t["stage_started"] = time.monotonic()
                    t.pop("resource_goal_resolved", None)
                    t["wanted_resources"] = []
                    t["navigation"] = {}
                    t.pop("summit_confirmed", None)
                    if index + 1 < len(steps):
                        stage = steps[index + 1]
                        self.say("接下来" + stage["label"] + "。")
                    else:
                        await self.next_agenda(s)
                        return
                goal = t["agent_goal"] = stage["goal"]
                t["stage_goal"] = stage
            else:
                await self.next_agenda(s)
                return
        if (
            t.get("urgent_goal") == "SURFACE"
            and p.get("head_medium") == "breathable"
            and (p.get("breath", 0) >= 8)
        ):
            t["urgent_goal"] = "ASHORE"
        on_land = game["grounded"] and game["dry_land"] and (not game["in_water"])
        if on_land:
            t.setdefault("land_since", time.monotonic())
        else:
            t.pop("land_since", None)
        landed = on_land and time.monotonic() - t["land_since"] >= 0.6
        if t.get("urgent_goal") == "ASHORE" and landed:
            t.pop("urgent_goal", None)
        if t.get("urgent_goal") == "EVADE" and (
            not any(
                (
                    e.get("relation") == "hostile_species" and e.get("distance", 99) < 5
                    for e in game["entities"]
                )
            )
        ):
            t.pop("urgent_goal", None)
        if t["kind"] == "TRAVEL":
            distance = (
                (p["position"]["x"] - start["x"]) ** 2
                + (p["position"]["z"] - start["z"]) ** 2
            ) ** 0.5
            if (
                goal == "SURFACE"
                and p.get("head_medium") == "breathable"
                and (p.get("breath", 0) >= 8)
            ):
                if self.paused_goal:
                    t["agent_goal"] = "ASHORE"
                    goal = "ASHORE"
                else:
                    if not t.get("surface_reported"):
                        t["surface_reported"] = True
                        self.say(
                            "已经浮到水面，我先帮你保持浮起；你可以让我上岸，或说我自己来。"
                        )
                        self.emit()
                    return
            if goal == "ASHORE" and on_land and (not landed):
                return
            done = (
                goal == "ASHORE"
                and landed
                or (goal == "FORWARD" and distance >= t.get("travel_distance", 3) - 0.3)
                or (
                    goal == "ASCEND"
                    and t.get("extent") == "ONE"
                    and landed
                    and (p["position"]["y"] > start["y"] + 0.6)
                )
                or (
                    goal == "EVADE"
                    and landed
                    and (
                        distance >= 3.5
                        or (
                            distance >= 1
                            and (
                                not any(
                                    (
                                        e.get("relation") == "hostile_species"
                                        and e.get("distance", 99) < 5
                                        for e in game["entities"]
                                    )
                                )
                            )
                        )
                    )
                )
            )
            if done:
                await self.complete()
                return
        if (
            goal == "EXPLORE"
            and (not t.get("agenda"))
            and (t.get("stop_kind") not in {"TIMED", "ONGOING"})
        ):
            progress = t.get("progress", {})
            if (
                progress.get("max_radius", 0) >= 12
                and len(progress.get("cells", [])) >= 12
                and (time.monotonic() - t.get("arming_started", time.monotonic()) >= 25)
            ):
                await self.complete()
                return
        if (
            goal == "ASCEND"
            and landed
            and (p["position"]["y"] >= start["y"] + 2)
            and (t.get("extent") != "ONE")
        ):
            upward = any(
                (
                    r.get("rise", 0) > 0
                    and (not r.get("protected"))
                    and (not r.get("place_step"))
                    for r in game.get("navigation", [])
                )
            )
            if (
                not upward
                and (not t.get("summit_check"))
                and (time.monotonic() - t.get("summit_checked", 0) > 8)
            ):
                t["summit_checked"] = time.monotonic()
                gen = self.generation

                async def check_summit():
                    try:
                        a = await self.r.controller.judge_typed(
                            "voxel_summit_completion",
                            {
                                "request": t["request"],
                                "start": start,
                                "position": p["position"],
                                "grounded": landed,
                                "visible_nodes": game["nodes"],
                                "navigation": game.get("navigation"),
                                "terrain": s["view"]
                                .get("construction", {})
                                .get("terrain", []),
                            },
                            {
                                "arrived": {
                                    "type": "noul",
                                    "instructions": "Does observed terrain support that the player has reached the requested local hilltop? A jump or one ledge is insufficient. Need a stable raised area, no observed higher continuation nearby. Unknown/occluded continuation is not proof of a summit.",
                                }
                            },
                            deadline_ms=1400,
                        )
                        self.r.post(
                            "voxel.task.summit", {"generation": gen, "answer": a}
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self.r.post(
                            "voxel.task.summit",
                            {"generation": gen, "error": type(e).__name__},
                        )
                    finally:
                        t.pop("summit_check", None)

                t["summit_check"] = True
                self.step_job = self.r.spawn(check_summit())
                return
        actions = native.get("actions") or []
        stage = t.get("stage_goal")
        craft_supply = None
        if stage and stage["goal"] == "CRAFT" and (not t.get("urgent_goal")):
            requirements = [
                r
                for r in native.get("craft_requirements", [])
                if r.get("item_id") == stage["item_id"]
            ]
            exact = {
                r["recipe_id"]
                for r in self.bridge.recipe_catalog.values()
                if r["item_id"] == stage["item_id"]
            }
            ready = any(
                (
                    a["kind"] == "CRAFT"
                    and (
                        a.get("item_id") == stage["item_id"]
                        or a.get("dependency_for") in exact
                    )
                    for a in actions
                )
            )
            place_bench = any(
                (r.get("workbench_required") for r in requirements)
            ) and game["inventory"].get("mcl_crafting_table:crafting_table")
            if requirements and (not ready) and (not place_bench):
                t["craft_deficit"] = {
                    "item_id": stage["item_id"],
                    "requirements": requirements,
                }
                signature = json.dumps(requirements, sort_keys=True)
                if (
                    t.get("craft_supply_for") != stage["item_id"]
                    or t.get("craft_supply_signature") != signature
                ):
                    gen = self.generation
                    item = stage["item_id"]
                    copy_task = copy.deepcopy(t)

                    async def supply():
                        from .resources import choose_resource_goal

                        try:
                            result = await choose_resource_goal(
                                self.r.controller, s, copy_task
                            )
                            self.r.post(
                                "voxel.task.craft_supply",
                                {
                                    "generation": gen,
                                    "result": result,
                                    "item_id": item,
                                    "signature": signature,
                                },
                            )
                        except Exception as e:
                            self.r.post(
                                "voxel.task.craft_supply",
                                {
                                    "generation": gen,
                                    "error": type(e).__name__,
                                    "item_id": item,
                                },
                            )

                    self.step_job = self.r.spawn(supply())
                    return
                craft_supply = t.get("craft_supply")
                if craft_supply:
                    goal = (
                        "FIND_WOOD"
                        if craft_supply["resource_group"] == "LOGS"
                        else "FIND_STONE"
                        if craft_supply["resource_group"] == "STONE"
                        else "RETURN_SITE"
                    )
                    if goal == "RETURN_SITE":
                        sites = [
                            r["position"]
                            for r in self.facilities.values()
                            if r["item_id"] == "mcl_crafting_table:crafting_table"
                        ]
                        if sites:
                            t["return_site"] = min(
                                sites,
                                key=lambda r: sum(
                                    ((r[k] - p["position"][k]) ** 2 for k in ("x", "z"))
                                ),
                            )
                        else:
                            t.pop("craft_supply_for", None)
                            t.pop("craft_supply", None)
                            await self.repair(s, actions)
                            return
            else:
                t.pop("craft_deficit", None)
        targets = (
            craft_supply.get("wanted_resources", [])
            if craft_supply
            else t.get("wanted_resources", [])
        )
        if (
            not targets
            and t.get("retry_after_observation")
            and (time.monotonic() - t.get("resource_checked", 0) > 5)
        ):
            t["resource_goal_resolved"] = False
        visible_drops = [
            r["position"] for r in game.get("drops", []) if r["item_id"] in targets
        ]
        if visible_drops:
            t["collect_positions"] = visible_drops
        if not targets:
            from .resources import choose_resource_goal

            gen = self.generation

            async def prepare():
                try:
                    result = await choose_resource_goal(
                        self.r.controller,
                        s,
                        t,
                        self.mode.building.project if self.mode.building else None,
                    )
                    self.r.post(
                        "voxel.task.resources", {"generation": gen, "result": result}
                    )
                except Exception as e:
                    self.r.post(
                        "voxel.task.resources",
                        {"generation": gen, "error": type(e).__name__},
                    )

            if (
                t["kind"] == "AGENT"
                and goal
                in {"FIND_WOOD", "FIND_STONE", "MATERIALS", "SURVIVE", "GATHER"}
                and (not t.get("resource_goal_resolved"))
            ):
                self.step_job = self.r.spawn(prepare())
                return
        if (
            goal in {"FIND_WOOD", "FIND_STONE"}
            and t.get("quantity")
            and (not t.get("agenda"))
        ):
            outputs = (
                ["mcl_core:cobble", "mcl_core:stone"]
                if goal == "FIND_STONE"
                else targets
            )
            gained = sum(
                (
                    game["inventory"].get(i, 0) - t["baseline_inventory"].get(i, 0)
                    for i in outputs
                )
            )
            if gained >= t["quantity"]:
                await self.complete()
                return
        if self.mode.building and self.mode.building.project and (goal == "MATERIALS"):
            project = self.mode.building.project
            plan = self.mode.building.plan()
            if t.get("project_id") != project.project_id:
                self.stop("construction_changed")
                self.emit()
                return
            if not plan:
                if not self.mode.building.job or self.mode.building.job.done():
                    if not t.get("design_retry"):
                        t["design_retry"] = True
                        project.authorization = {
                            "source": t["source"],
                            "duration_ms": t["duration_ms"],
                            "deadline": t.get("deadline"),
                            "generation": self.bridge.generation,
                            "supply": True,
                        }
                        self.mode.building.design(t["source"], True)
                    else:
                        self.stop("design_unavailable")
                        self.say("布局还没生成成功，采集和设计要求都保留了。")
                        self.emit()
                return
            if plan:
                from .blueprint import material_deficits

                t["materials_needed"] = plan.public()["materials"]
                if not material_deficits(plan, s, project.completed):
                    t["return_site"] = project.site
                    distance = (
                        sum(
                            (
                                (p["position"][k] - project.site[k]) ** 2
                                for k in ("x", "z")
                            )
                        )
                        ** 0.5
                    )
                    if distance > 3:
                        t["urgent_goal"] = "RETURN_SITE"
                    else:
                        t.pop("urgent_goal", None)
                        source = t["source"]
                        remaining = max(
                            1000,
                            int(
                                (self.bridge.task_grant["deadline"] - time.monotonic())
                                * 1000
                            ),
                        )
                        t["status"] = "COMPLETED"
                        t["authorized"] = False
                        self.bridge.task_grant = None
                        project.authorization = {
                            "source": source + "-build",
                            "duration_ms": remaining,
                            "deadline": t.get("deadline"),
                            "generation": self.bridge.generation,
                        }
                        self.mode.building.begin(self.bridge.origin(source + "-build"))
                        self.emit()
                        return
        urgent_move = t.pop("urgent_move", None)
        if urgent_move and t.get("urgent_goal"):
            match = next(
                (
                    i
                    for i, a in enumerate(actions)
                    if a["kind"] == urgent_move["kind"]
                    and a.get("dx") == urgent_move.get("dx")
                    and (a.get("dz") == urgent_move.get("dz"))
                ),
                None,
            )
            if match is not None:
                self.r.post(
                    "voxel.task.decision",
                    {
                        "generation": self.generation,
                        "urgent_goal": t["urgent_goal"],
                        "answer": {"action": {"choice": str(match), "confidence": 1}},
                        "actions": actions,
                    },
                )
                return
        if (
            goal in {"FIND_WOOD", "FIND_STONE"}
            and (not t.get("agenda"))
            and t.get("quantity")
            and (t.get("resources_dug", 0) >= t["quantity"])
        ):
            actions = [a for a in actions if a["kind"] not in {"DIG", "CRAFT"}]
        stage = t.get("stage_goal")
        if (
            stage
            and stage["goal"] in {"FIND_WOOD", "FIND_STONE"}
            and (not t.get("urgent_goal"))
        ):
            actions = [
                a
                for a in actions
                if a["kind"] != "CRAFT"
                and (a["kind"] != "PLACE" or a.get("step_support"))
            ]
        if (
            stage
            and stage["goal"] == "CRAFT"
            and (not t.get("urgent_goal"))
            and (not craft_supply)
        ):
            recipe_ids = {
                r["recipe_id"]
                for r in self.bridge.recipe_catalog.values()
                if r["item_id"] == stage["item_id"]
            }
            crafts = [
                a
                for a in actions
                if a["kind"] == "CRAFT"
                and (
                    a.get("item_id") == stage["item_id"]
                    or a.get("dependency_for") in recipe_ids
                )
            ]
            requirements = [
                r
                for r in native.get("craft_requirements", [])
                if r.get("item_id") == stage["item_id"]
            ]
            need_bench = any((r.get("workbench_required") for r in requirements))
            needed = {k for r in requirements for k in r.get("missing") or {}}
            dependencies = [
                a
                for a in actions
                if a["kind"] in {"SELECT", "PLACE"}
                and a.get("item_id") == "mcl_crafting_table:crafting_table"
                and need_bench
            ]
            if crafts:
                actions = crafts
            elif dependencies:
                actions = dependencies
            else:
                actions = [a for a in actions if a["kind"] != "CRAFT"]
        if craft_supply:
            actions = [
                a
                for a in actions
                if a["kind"] != "CRAFT"
                and (
                    not (
                        a["kind"] == "DIG"
                        and a.get("item_id") == "mcl_crafting_table:crafting_table"
                    )
                )
            ]
        if stage and stage["goal"] == "EXPLORE" and (not t.get("urgent_goal")):
            actions = [a for a in actions if a["kind"] != "CRAFT"]
        if stage and stage["goal"] == "PLACE_BENCH" and (not t.get("urgent_goal")):
            actions = [
                a
                for a in actions
                if a["kind"] in {"PLACE", "SELECT"}
                and a.get("item_id") == "mcl_crafting_table:crafting_table"
            ]
        if goal in {"EXPLORE", "ASCEND"} and (not t.get("urgent_goal")):
            obstacles = [
                b["position"]
                for r in game.get("navigation", [])
                for b in r.get("blockers", [])
            ]
            actions = [
                a
                for a in actions
                if a["kind"] != "CRAFT"
                and (a["kind"] != "DIG" or a.get("target") in obstacles)
                and (a["kind"] != "PLACE" or a.get("step_support"))
            ]
        from .locomotion import filter_actions

        key = f"{round(p['position']['x'] / 3)},{round(p['position']['z'] / 3)}"
        actions = filter_actions(
            actions, t, p["position"], self.search_views.get(key, []), self.recent
        )
        if (
            t.get("stalled_s", 0) >= 15
            and time.monotonic() - t.get("repair_at", 0) > 15
        ):
            await self.repair(s, actions)
            return
        if not actions:
            return
        if time.monotonic() - self.last_step < 0.1:
            return
        self.last_step = time.monotonic()
        gen = self.generation
        snapshot = copy.deepcopy(s)
        task = copy.deepcopy(t)
        task["search_views"] = getattr(self, "search_views", {})
        task["observed_terrain"] = (
            snapshot["view"].get("construction", {}).get("terrain", [])
        )
        if craft_supply:
            task["agent_goal"] = goal
        if task.get("urgent_goal"):
            task["agent_goal"] = task["urgent_goal"]

        async def choose():
            try:
                a = await select_step(
                    self.r.controller,
                    snapshot,
                    task,
                    actions,
                    self.recent,
                    self.visited,
                    targets,
                    list(self.resource_memory.values()),
                )
                self.r.post(
                    "voxel.task.decision",
                    {
                        "generation": gen,
                        "answer": a,
                        "actions": actions,
                        "urgent_goal": task.get("urgent_goal"),
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.r.post(
                    "voxel.task.decision",
                    {"generation": gen, "error": type(e).__name__},
                )

        self.step_job = self.r.spawn(choose())

    async def next_agenda(self, s):
        t = self.task
        if t.get("stop_kind") not in {"TIMED", "ONGOING"}:
            self.replan_outcome(
                "legacy intermediate agenda ended without verifying the requested result"
            )
            return
        if not self.bridge.task_current() or time.monotonic() < t.get(
            "replan_after", 0
        ):
            return
        if t.get("deadline", 0) - time.monotonic() <= 2:
            return
        gen = self.generation
        snapshot = copy.deepcopy(s)
        previous = copy.deepcopy(t)

        async def plan():
            from .agenda import propose

            try:
                agenda = await propose(
                    self.mode,
                    t["request"]
                    + "；上一阶段已完成，请根据新局面安排下一阶段，勿重复已完成的准备。",
                    snapshot,
                    previous,
                )
                products = {
                    r["item_id"] for r in agenda["steps"] if r["goal"] == "CRAFT"
                }
                recipes = [
                    r["recipe_id"]
                    for r in self.bridge.recipe_catalog.values()
                    if r["item_id"] in products
                ][:24]
                self.r.post(
                    "voxel.task.replanned",
                    {"generation": gen, "agenda": agenda, "recipes": recipes},
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.r.post(
                    "voxel.task.replanned",
                    {"generation": gen, "error": type(e).__name__},
                )

        self.job = self.r.spawn(plan())

    def replan_outcome(self, reason):
        if (
            not self.task
            or not self.bridge.task_current()
            or (self.job and (not self.job.done()))
        ):
            return
        t = self.task
        if time.monotonic() < t.get("outcome_replan_after", 0):
            return
        t["outcome_replan_after"] = time.monotonic() + 5
        gen = self.generation
        task = copy.deepcopy(t)
        snapshot = copy.deepcopy(self.bridge.snapshot)
        if t.get("milestone_complete"):
            task["outcome"] = None
            task["baseline_inventory"] = semantic.context(snapshot)["inventory"]

        async def revise():
            from .outcomes import plan

            try:
                result = await plan(self.mode, task, snapshot, reason=reason)
                self.r.post(
                    "voxel.task.outcome_plan", {"generation": gen, "outcome": result}
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.r.post(
                    "voxel.task.outcome_plan",
                    {
                        "generation": gen,
                        "error": str(error)[:180] or type(error).__name__,
                    },
                )

        self.job = self.r.spawn(revise())

    def review_completion(self):
        if self.job and (not self.job.done()):
            self.job.cancel()
        t = self.task
        t["completion_pending"] = True
        gen = self.generation
        task = copy.deepcopy(t)
        snapshot = copy.deepcopy(self.bridge.snapshot)

        async def review():
            from .judgment import choose
            from .outcomes import evidence

            try:
                native = evidence(task, snapshot)
                relevant = {
                    i: r
                    for i, r in native.items()
                    if r["kind"] == "inventory"
                    and r.get("gained", 0) > 0
                    or (
                        r["kind"]
                        in {
                            "dig",
                            "use",
                            "craft",
                            "defeat",
                            "place",
                            "position",
                            "observation",
                        }
                        and (not r.get("summary_of_receipts"))
                    )
                }
                registry = {
                    **self.native_item_memory,
                    **{
                        r["item_id"]: r
                        for r in snapshot["view"]
                        .get("survival", {})
                        .get("item_facts", [])
                    },
                }
                value = await choose(
                    self.mode.reasoning,
                    "Decide whether the real native receipts and current inventory establish the requested result. Some semantic checks may be uncertain; inspect the evidence rather than assuming completion. COMPLETE only if all final conditions are established; MORE for an unfulfilled part. A placement receipt with matching target/item proves placement unless a later dig removed it. Do not invent extra steps or demand a separate pickup click for automatically collected inventory.",
                    {
                        "request": task["request"],
                        "verified_conditions": task["outcome"]["success_checks"],
                        "evidence": relevant,
                        "source_yields": {
                            r.get("item_id"): registry.get(r.get("item_id"), {}).get(
                                "possible_drops"
                            )
                            for r in task.get("effects", [])
                            if r["kind"] == "DIG"
                        },
                    },
                    {
                        "COMPLETE": "Entire requested result is achieved",
                        "MORE": "A stated part remains; explain what",
                    },
                )
                self.r.post(
                    "voxel.task.completion_review", {"generation": gen, "result": value}
                )
            except Exception as error:
                self.r.post(
                    "voxel.task.completion_review",
                    {
                        "generation": gen,
                        "error": str(error)[:140] or type(error).__name__,
                    },
                )

        self.job = self.r.spawn(review())

    async def outcome_snapshot(self, s, native):
        from . import outcomes
        from .locomotion import filter_actions

        t = self.task
        game = semantic.context(s)
        now = time.monotonic()
        from .spatial import observe

        observe(t, s)
        blockers = [
            r
            for r in native.get("craft_requirements", [])
            if r.get("inventory_blocker")
        ]
        if blockers:
            self.stop("craft_inventory_blocked")
            self.say(
                "合成格里已经放着物品，需要先腾空合成格；目标和已经收集的材料都保留着。"
            )
            self.emit()
            return
        if t.get("completion_pending") or now < t.get("completion_wait_until", 0):
            return
        if t.get("milestone_complete"):
            if not self.job or self.job.done():
                self.replan_outcome(
                    "Choose the next milestone for the retained timed/ongoing request."
                )
            return
        if t.pop("completion_retry", False):
            self.review_completion()
            return
        tracked = t["outcome"].get("target")
        if tracked and tracked.get("target_id"):
            fresh = next(
                (
                    r
                    for r in game["entities"] + game["nodes"]
                    if r.get("target_id") == tracked["target_id"]
                ),
                None,
            )
            if fresh:
                t["outcome"]["target"] = {**fresh, "last_seen": now}
            if not tracked.get("relation"):
                operated = any(
                    (
                        r.get("target") == tracked.get("position")
                        and r["kind"] in {"DIG", "PLACE", "USE"}
                        for r in t.get("effects", [])
                    )
                )
                if operated:
                    t["outcome"]["target"] = None
                    t.setdefault("navigation", {}).pop("waypoint", None)
                    if any(
                        (
                            "inventory" in c["evidence"]
                            for c in t["outcome"]["success_checks"]
                        )
                    ):
                        t["outcome_review_at"] = 0
                        t["strategy_invalid"] = True
                        t["harvested_source"] = copy.deepcopy(tracked)
        elif tracked and tracked.get("evidence") == "drop":
            from .locomotion import distance

            drops = [r for r in game["drops"] if r["item_id"] == tracked.get("item_id")]
            if drops:
                nearest = min(
                    drops, key=lambda r: distance(r["position"], tracked["position"])
                )
                t["outcome"]["target"] = {**nearest, "evidence": "drop"}
            else:
                t["outcome"]["target"] = None
                t["outcome_review_at"] = 0
                t["strategy_invalid"] = True
        pending_plan = t.pop("pending_outcome_plan", None)
        if pending_plan:
            if pending_plan["generation"] != self.generation:
                return
            await self.handle("voxel.task.outcome_plan", pending_plan)
            return
        if t.get("urgent_goal"):
            p = game["player"]
            if (
                t["urgent_goal"] == "SURFACE"
                and p.get("head_medium") == "breathable"
                and (p.get("breath", 0) >= 8)
            ):
                t["urgent_goal"] = "ASHORE"
            if t["urgent_goal"] == "ASHORE" and game["dry_land"] and game["grounded"]:
                t.pop("urgent_goal", None)
            if t.get("urgent_goal") == "EVADE" and (
                not any(
                    (
                        e.get("relation") == "hostile_species"
                        and e.get("distance", 99) < 5
                        for e in game["entities"]
                    )
                )
            ):
                t.pop("urgent_goal", None)
            if t.get("urgent_goal"):
                from .agency import select_step

                gen = self.generation
                urgent = copy.deepcopy(t)
                urgent["agent_goal"] = t["urgent_goal"]
                actions = native.get("actions") or []

                async def evade():
                    try:
                        a = await select_step(
                            self.r.controller,
                            s,
                            urgent,
                            actions,
                            self.recent,
                            self.visited,
                        )
                        self.r.post(
                            "voxel.task.decision",
                            {
                                "generation": gen,
                                "answer": a,
                                "actions": actions,
                                "urgent_goal": urgent["agent_goal"],
                            },
                        )
                    except Exception as error:
                        self.r.post(
                            "voxel.task.decision",
                            {"generation": gen, "error": type(error).__name__},
                        )

                self.step_job = self.r.spawn(evade())
                return
        if self.job and (not self.job.done()):
            return
        inventory_changed = game["inventory"] != t.get(
            "outcome_review_inventory", t.get("baseline_inventory", {})
        )
        changed = (
            t.get("effect_version", 0) != t.get("outcome_review_version", 0)
            or inventory_changed
        )
        if changed or now - t.get("outcome_review_at", 0) > 10:
            t["outcome_review_version"] = t.get("effect_version", 0)
            t["outcome_review_at"] = now
            t["outcome_review_inventory"] = dict(game["inventory"])
            gen = self.generation
            task = copy.deepcopy(t)
            snapshot = copy.deepcopy(s)

            async def verify():
                try:
                    result = await outcomes.review(self.mode, task, snapshot)
                    self.r.post(
                        "voxel.task.outcome_review",
                        {"generation": gen, "result": result},
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.r.post(
                        "voxel.task.outcome_review",
                        {
                            "generation": gen,
                            "error": str(error)[:180] or type(error).__name__,
                        },
                    )

            self.step_job = self.r.spawn(verify())
            return
        if t.get("strategy_invalid"):
            source = t.get("harvested_source")
            registry = {
                **self.native_item_memory,
                **{
                    r["item_id"]: r
                    for r in s["view"].get("survival", {}).get("item_facts", [])
                },
            }
            if source and game["drops"]:
                candidates = {
                    str(i): {
                        **r,
                        "registered_source_drops": registry.get(
                            source["item_id"], {}
                        ).get("possible_drops"),
                    }
                    for i, r in enumerate(game["drops"])
                }
                gen = self.generation

                async def select_drop():
                    try:
                        a = await self.r.controller.judge_typed(
                            "voxel_harvested_drop_target",
                            {
                                "request": t["request"],
                                "source": source["item_id"],
                                "inventory": game["inventory"],
                            },
                            {
                                "drop": {
                                    "type": "choice",
                                    "instructions": "The selected source was just harvested. Which currently visible dropped item is the useful result or needed ingredient to pick up next? Select its registered yield, not a different planted block. NONE if none of these drops helps. This is an observation reference, not proof the inventory already contains it.",
                                    "criteria": {
                                        "NONE": "No useful observed drop",
                                        **candidates,
                                    },
                                }
                            },
                            deadline_ms=1500,
                        )
                        key = a["drop"]["choice"]
                        row = (
                            game["drops"][int(key)]
                            if key.isdigit() and int(key) < len(game["drops"])
                            else None
                        )
                        self.r.post(
                            "voxel.task.harvest_target",
                            {
                                "generation": gen,
                                "target": {**row, "evidence": "drop"} if row else None,
                            },
                        )
                    except Exception as error:
                        self.r.post(
                            "voxel.task.harvest_target",
                            {"generation": gen, "error": type(error).__name__},
                        )

                self.step_job = self.r.spawn(select_drop())
                return
            t["outcome"]["target"] = None
            t.pop("harvested_source", None)
            t.pop("strategy_invalid", None)
        actions = native.get("actions") or []
        p = game["player"]["position"]
        k = f"{round(p['x'] / 3)},{round(p['z'] / 3)}"
        actions = filter_actions(
            actions, t, p, self.search_views.get(k, []), self.recent
        )
        if not actions:
            if now - self.last_step > 4:
                self.replan_outcome(
                    "no presently legal actions, inspect requirements and alternative means"
                )
            return
        if now - self.last_step < 0.15:
            return
        self.last_step = now
        gen = self.generation
        task = copy.deepcopy(t)
        snapshot = copy.deepcopy(s)

        async def choose():
            try:
                result = await outcomes.choose(
                    self.mode, task, snapshot, actions, self.recent, self.visited
                )
                self.r.post(
                    "voxel.task.outcome_decision",
                    {
                        "generation": gen,
                        "answer": result,
                        "actions": actions,
                        "urgent_goal": task.get("urgent_goal"),
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.r.post(
                    "voxel.task.outcome_decision",
                    {
                        "generation": gen,
                        "error": str(error)[:180] or type(error).__name__,
                    },
                )

        self.step_job = self.r.spawn(choose())

    async def repair(self, s, actions):
        t = self.task
        t["repair_at"] = time.monotonic()
        gen = self.generation
        game = semantic.context(s)
        game.pop("navigation", None)
        game.pop("tools", None)
        game["nodes"] = game["nodes"][:8]
        state = {
            "request": t["request"],
            "task": {
                k: t.get(k)
                for k in ("agent_goal", "stage_goal", "stalled_s", "constraints")
            },
            "game": game,
            "craft_deficit": t.get("craft_deficit"),
            "recent": list(self.recent)[-5:],
        }
        options = {
            str(i): {
                k: v for k, v in a.items() if k not in {"action_id", "target_index"}
            }
            for i, a in enumerate(actions)
        }

        async def review():
            try:
                a = await self.r.controller.judge_typed(
                    "voxel_progress_repair",
                    state,
                    {
                        "action": {
                            "type": "choice",
                            "instructions": "The current goal has made no observed progress for too long. Choose a concrete DIFFERENT useful legal action: expose route headroom, use a needed tool, move to less-visited ground, collect actual drops. Do not repeat recent ineffective LOOK/JUMP/movement loops. REPLAN if the overall agenda is no longer useful. ASK only when all offered actions cannot serve the goal.",
                            "criteria": {
                                "REPLAN": "Revise overall authorized gameplay agenda",
                                "ASK": "No useful lawful route; explain the specific blocker",
                                **options,
                            },
                        }
                    },
                    deadline_ms=2500,
                )
                self.r.post(
                    "voxel.task.repair",
                    {"generation": gen, "answer": a, "actions": actions},
                )
            except Exception as e:
                self.r.post(
                    "voxel.task.repair", {"generation": gen, "error": type(e).__name__}
                )

        self.step_job = self.r.spawn(review())

    def recover_goal(self, s, actions):
        gen = self.generation
        t = copy.deepcopy(self.task)
        snapshot = copy.deepcopy(s)

        async def review():
            try:
                from .judgment import choose

                result = await choose(
                    self.mode.reasoning,
                    "The retained user goal stalled. Choose a different lawful next step and explain a short continuation strategy in Chinese. Use available local digging/step construction to make progress to a destination. When nearby upward access is not present, move toward visible higher terrain or reveal the relevant obstacle. Do not declare completion after one movement or ask the user to do routine available work. ASK only for an actual unresolved restriction or no reachable option.",
                    {
                        "request": t["request"],
                        "goal": t.get("agent_goal"),
                        "game": semantic.context(snapshot),
                        "recent_attempts": list(self.recent)[-8:],
                        "previous_recovery": t.get("recovery"),
                        "observed_higher_ground": snapshot["view"]
                        .get("construction", {})
                        .get("terrain", []),
                    },
                    {
                        "ASK": "No lawful progress; explain concrete blocker",
                        **{
                            str(i): {k: v for k, v in a.items() if k != "action_id"}
                            for i, a in enumerate(actions)
                        },
                    },
                )
                self.r.post(
                    "voxel.task.recovery",
                    {"generation": gen, "result": result, "actions": actions},
                )
            except Exception as error:
                self.r.post(
                    "voxel.task.recovery",
                    {"generation": gen, "error": type(error).__name__},
                )

        self.job = self.r.spawn(review())
        self.say("刚才这条路线没有推进，我换个办法，原来的目标继续保留。")

    def start_site(self, project, auth, repair=False):
        s = self.bridge.snapshot
        if auth.get("generation") != self.bridge.generation:
            return
        had = bool(self.bridge.task_grant) or repair
        self.stop("prepare_build_site")
        self.task = {
            "id": uuid4().hex,
            "kind": "AGENT",
            "status": "RUNNING" if had else "READY",
            "request": "\n".join((r["text"] for r in project.requests[-6:])),
            "source": auth["source"] + "-site",
            "world_id": s["world_id"],
            "goal": project.title,
            "constraints": list(self.mode.memory.preferences.values()),
            "quantity": None,
            "completed": 0,
            "duration_ms": auth.get("duration_ms", 900000),
            "scope": {"kind": "AGENT", "recipes": []},
            "authorized": True,
            "generation": self.bridge.generation,
            "pending_question": None,
            "history": [],
            "stop_kind": "GOAL",
            "agent_goal": "PREPARE_SITE",
            "project_id": project.project_id,
            "start_position": copy.deepcopy(s["view"]["self"]["position"]),
            "start_yaw": s["view"]["self"].get("yaw", 0),
            "baseline_inventory": semantic.context(s)["inventory"],
            "site_alter": True,
        }
        if auth.get("deadline") is not None:
            self.task["deadline"] = auth["deadline"]
        project.authorization = {**auth, "generation": self.bridge.generation}
        project.status = "PREPARING_SITE"
        if repair:
            plan = self.mode.building.plan()
            region = {
                "origin": plan.origin,
                "width": plan.size[0],
                "depth": plan.size[1],
                "height": plan.size[2],
            }
            self.task.update(
                site_repair=True,
                site_region=region,
                site_strategy={"method": "REPAIR"},
                site_checked=time.monotonic(),
            )
            from .site import working_region

            self.task["scope"]["site_box"] = working_region(region)
            self.task["protected_build_cells"] = [
                {"position": r["target"], "item_id": r["item_id"]}
                for r in project.completed
                if r.get("target") and (not r.get("removed"))
            ]
        if had:
            self.r.spawn(self.begin_after_revoke(self.task["source"]))
        else:
            self.begin(self.bridge.origin(self.task["source"]))
        self.emit()

    async def site_snapshot(self, s, native):
        from . import site
        from .blueprint import supported_sites, parse_blueprint

        t = self.task
        building = self.mode.building
        project = building.project if building else None
        if (
            not project
            or project.project_id != t.get("project_id")
            or project.status == "CANCELLED"
        ):
            self.stop("site_project_changed")
            self.emit()
            return
        plan = building.plan()
        if not plan:
            self.stop("site_design_missing")
            self.emit()
            return
        if t.get("site_repair"):
            obstructions = building.site_obstructions(s)
            if not obstructions and t.get("completed", 0) > 0:
                remaining = max(0, int((t["deadline"] - time.monotonic()) * 1000))
                source = t["source"]
                t.update(status="COMPLETED", authorized=False)
                self.bridge.task_grant = None
                project.authorization = {
                    "source": source + "-build",
                    "duration_ms": remaining,
                    "deadline": t["deadline"],
                    "generation": self.bridge.generation,
                    "supply": True,
                }
                project.preparation = {"phase": "READY", "site": project.site}
                project.status = "READY"
                building.begin(self.bridge.origin(source + "-build"))
                self.emit()
                return
        sites = supported_sites(plan, s)
        current = t.get("site_region")
        site_ready = next(
            (
                r
                for r in sites
                if (not current or r == current["origin"])
                and site.ready(
                    {
                        "origin": r,
                        "width": plan.size[0],
                        "depth": plan.size[1],
                        "height": plan.size[2],
                    },
                    s,
                )
            ),
            None,
        )
        if site_ready and (not t.get("site_repair")):
            project.site = site_ready
            project.blueprint = parse_blueprint(
                plan.public(), building.materials(), site_ready
            ).public()
            project.preparation = {"phase": "READY", "site": site_ready}
            project.question = None
            remaining = max(0, int((t["deadline"] - time.monotonic()) * 1000))
            source = t["source"]
            t["status"] = "COMPLETED"
            t["authorized"] = False
            self.bridge.task_grant = None
            project.authorization = {
                "source": source + "-build",
                "duration_ms": remaining,
                "deadline": t["deadline"],
                "generation": self.bridge.generation,
                "supply": True,
            }
            project.status = "READY"
            await building.persist(source)
            self.say("场地已经准备好，接着备齐材料并按刚才的方案建。")
            building.begin(self.bridge.origin(source + "-build"))
            self.emit()
            return
        gen = self.generation
        task = copy.deepcopy(t)
        snapshot = copy.deepcopy(s)
        if not t.get("site_strategy") or (
            not t.get("site_repair")
            and t.get("stalled_s", 0) > 15
            and (time.monotonic() - t.get("site_checked", 0) > 18)
        ):
            t["site_checked"] = time.monotonic()

            async def choose_site():
                try:
                    result = await site.strategy(
                        self.mode, snapshot, project, plan, task.get("site_region")
                    )
                    self.r.post(
                        "voxel.task.site", {"generation": gen, "result": result}
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.r.post(
                        "voxel.task.site",
                        {"generation": gen, "error": type(error).__name__},
                    )

            self.step_job = self.r.spawn(choose_site())
            return
        actions = native.get("actions") or []
        from .locomotion import filter_actions

        p = s["view"]["self"]["position"]
        k = f"{round(p['x'] / 3)},{round(p['z'] / 3)}"
        actions = filter_actions(
            actions, t, p, self.search_views.get(k, []), self.recent
        )
        if not actions:
            return

        async def choose_step():
            try:
                result = await site.choose_step(
                    self.mode, snapshot, task, actions, self.recent, self.visited
                )
                self.r.post(
                    "voxel.task.site_step",
                    {"generation": gen, "answer": result, "actions": actions},
                )
            except Exception as error:
                self.r.post(
                    "voxel.task.site_step",
                    {"generation": gen, "error": type(error).__name__},
                )

        self.step_job = self.r.spawn(choose_step())

    def start_supplies(self, project, auth):
        s = self.bridge.snapshot
        if auth.get("generation") != self.bridge.generation:
            return
        if (
            self.task
            and self.task.get("project_id") == project.project_id
            and (self.task.get("agent_goal") == "MATERIALS")
            and (self.task.get("status") in {"READY", "RUNNING", "ARMING"})
            and self.task.get("authorized")
        ):
            self.task["materials_needed"] = (project.blueprint or {}).get(
                "materials", {}
            )
            return
        had_grant = bool(self.bridge.task_grant)
        self.stop("prepare_build_resources")
        self.task = {
            "id": uuid4().hex,
            "kind": "AGENT",
            "status": "READY",
            "request": "为当前房屋寻找并加工缺少的建材，准备好后继续建造。",
            "source": auth["source"] + "-supplies",
            "world_id": s["world_id"],
            "goal": project.title,
            "constraints": [],
            "quantity": None,
            "completed": 0,
            "duration_ms": auth.get("duration_ms", 900000),
            "scope": {"kind": "AGENT", "recipes": []},
            "authorized": True,
            "generation": self.bridge.generation,
            "pending_question": None,
            "history": [],
            "stop_kind": "GOAL",
            "agent_goal": "MATERIALS",
            "autonomy": True,
            "start_position": copy.deepcopy(s["view"]["self"]["position"]),
            "start_yaw": s["view"]["self"].get("yaw", 0),
            "baseline_inventory": semantic.context(s)["inventory"],
        }
        self.task["materials_needed"] = (project.blueprint or {}).get("materials", {})
        self.task["scope"]["recipes"] = [
            r["recipe_id"]
            for r in self.bridge.recipe_catalog.values()
            if r["item_id"] in self.task["materials_needed"]
        ][:24]
        self.task["project_id"] = project.project_id
        if auth.get("deadline") is not None:
            self.task["deadline"] = auth["deadline"]
        if had_grant:
            self.r.spawn(self.begin_after_revoke(self.task["source"]))
        else:
            self.begin(self.bridge.origin(self.task["source"]))
        self.emit()
