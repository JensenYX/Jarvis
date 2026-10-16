from __future__ import annotations
import asyncio
import copy
import json
import time
import httpx
from collections import deque
from uuid import uuid4
from .projects import Project, ProjectStore
from .blueprint import (
    parse_blueprint,
    material_deficits,
    site_from_snapshot,
    supported_sites,
)
from .construction import propose, brief, intake
from .activity import Activity, judge_activity
from .coach import decision_game
from .adapter import IDENTITY
from .attention import holds_actions


class Building:
    def __init__(self, mode):
        self.mode = mode
        self.r = mode.r
        self.project = None
        self.store = ProjectStore(self.r.memory, self.r.user_id)
        self.world = None
        self.identity = None
        self.job = None
        self.action_job = None
        self.activity = Activity()
        self.activity_job = None
        self.generation = 0
        self.last_step = 0.0
        self.last_action = None
        self.rejections = 0
        self.moves = deque(maxlen=12)
        self.pending_voice = None
        self.last_saved = 0.0
        self.counters = {}
        self.blocked_since = None
        self.pending_action = False
        self.unreachable = {}
        self.project_history = {}
        self.retry_after = 0.0
        self.service_errors = 0
        self.design_retries = 0

    @property
    def bridge(self):
        return self.mode.bridge

    async def bind_world(self, s):
        identity = tuple((s[k] for k in IDENTITY))
        if self.identity == identity:
            return
        self.stop("world_or_life_changed")
        self.identity = identity
        if self.world != s["world_id"]:
            self.world = s["world_id"]
            self.project = await self.store.load(self.world)
            self.activity = Activity()
            self.project_history = {
                p.project_id: p for p in await self.store.history(self.world)
            }
        self.emit()

    def emit(self):
        self.r.emit(
            "voxel.project",
            project=self.project.context() if self.project else None,
            authorization_active=self.bridge.project_current()
            if self.bridge
            else False,
            generation=self.generation,
        )

    async def persist(self, source):
        if self.project:
            await self.store.save(self.project, source)
            self.last_saved = time.monotonic()
            self.project_history[self.project.project_id] = copy.deepcopy(self.project)

    def observe_event(self, event):
        self.activity.observe(event)
        if self.project and event.get("kind") == "dug":
            position = (event.get("payload") or {}).get("position")
            for row in self.project.completed:
                if row.get("target") == position:
                    row["removed"] = True
            self.emit()

    def stop(self, reason, speak=False):
        arming = bool(self.project and self.project.status == "ARMING")
        self.generation += 1
        self.pending_voice = None
        if reason in {"new_project", "design_changed", "world_or_life_changed"}:
            self.unreachable = {}
            self.design_retries = 0
        current = asyncio.current_task()
        for name in ("job", "activity_job"):
            task = getattr(self, name)
            if task and task is not current:
                task.cancel()
            setattr(self, name, None)
        if self.project:
            self.project.authorization = None
            if reason not in {"design_changed", "hold_and_revise"}:
                self.project.execution_intent = False
            if self.project.status not in {"COMPLETED", "CANCELLED", "DISCUSSING"}:
                self.project.status = "PAUSED"
        if self.bridge and (self.bridge.project_grant or arming):
            self.bridge.revoke(reason)
            self.r.spawn(self.bridge.display.release())
        self.emit()
        if speak:
            self.say("已暂停建造，设计和进度保留，后面由你操作。")

    def say(self, text=None, cue=None, *, activity=False):
        binding = self.mode.binding("project_feedback" if activity else "game")
        if binding:
            binding["project_generation"] = self.generation
        if self.r.muted:
            return
        self.mode.say(text, "game_project", binding, cue=cue)

    def status_text(self, project=None):
        p = project if project is not None else self.project
        if not p:
            return "当前没有建造项目。"
        count = p.completed_count()
        total = (p.blueprint or {}).get("count", 0)
        label = p.title if p.title != "一起建造" else "房子"
        if p.status == "CANCELLED":
            return label + "已取消，已经放好的方块还在。"
        if total:
            text = f"{label}已放好{count}/{total}块。"
            if p.status == "COMPLETED":
                return text + "这份方案已建完。"
            if p.status == "PAUSED":
                return text + "目前暂停，剩余部分和原方案都保留着。"
            if p.status == "NEEDS_MATERIALS":
                return text + "正在准备缺少的建材，之后继续原来的房子。"
            if p.status == "PREPARING_SITE":
                return text + "正在处理场地和施工通路。"
            if p.last_progress and time.monotonic() - p.last_progress > 12:
                return text + "刚才施工没有推进，正在检查挡住施工面的东西和其他站位。"
            return text + "正在继续剩下的部分。"
        return (
            label
            + "还在设计。"
            + (
                p.question.get("question", "")
                if p.question
                else "会按已经说好的要求继续。"
            )
        )

    async def flush(self):
        if self.pending_voice and (
            not (
                self.r.user_speaking
                or self.r.route_pending
                or self.r.response_id
                or self.r.muted
            )
        ):
            item = self.pending_voice
            self.pending_voice = None
            if (
                item["generation"] == self.generation
                and time.monotonic() - item["at"] < 10
                and self.mode.valid(item["binding"])
            ):
                self.mode.say(
                    item["text"], "game_project", item["binding"], cue=item["cue"]
                )

    async def apply_intent(self, update, decision, origin, snapshot):
        await self.bind_world(snapshot)
        route = decision["route"]["choice"]
        confidence = decision["route"]["confidence"]
        self.r.emit(
            "voxel.project.intent",
            action=route,
            confidence=confidence,
            authorize=decision.get("project_authorize", {}).get("noul", 0),
        )
        if route == "PROJECT_STATUS":
            self.say(cue=self.cue(update.text))
            return
        project_mass = sum(
            (
                v
                for k, v in decision["route"].get("probabilities", {}).items()
                if k in {"PROJECT_NEW", "PROJECT_CHANGE", "PROJECT_START"}
            )
        )
        if (
            confidence < 0.65
            and project_mass < 0.85
            and (decision.get("project_authorize", {}).get("noul", 0) < 0.85)
        ):
            self.say("你是想让我动手建造，还是先讨论设计？")
            return
        if self.project and decision.get("project_update", {}).get("noul", 0) >= 0.8:
            route = "PROJECT_CHANGE"
        authorized = decision.get("project_authorize", {}).get("noul", 0) >= 0.85
        preserved = bool(
            self.project
            and self.project.authorization
            and (decision.get("keep_original_request", {}).get("noul", 0) >= 0.85)
        )
        previous_auth = (
            copy.deepcopy(self.project.authorization) if self.project else None
        )
        if decision.get("project_hold", {}).get("noul", 0) >= 0.8:
            authorized = False
            preserved = False
        delegate = decision.get("delegate_design", {}).get("noul", 0) >= 0.8
        duration = decision.get("duration_ms")
        if duration and (not 1000 <= duration <= 900000):
            self.say(
                "单次建造时长支持十五分钟以内，请给一个较短时长；方案和进度可以继续保留。"
            )
            return
        if route == "PROJECT_NEW" or self.project is None:
            if self.project:
                await self.persist(update.utterance_id)
            self.stop("new_project")
            self.project = Project(
                world_id=snapshot["world_id"], site=site_from_snapshot(snapshot)
            )
        elif route == "PROJECT_CHANGE":
            self.stop("design_changed")
        self.project.execution_intent = (
            authorized or preserved or self.project.execution_intent
        )
        if decision.get("project_hold", {}).get("noul", 0) >= 0.8:
            self.project.execution_intent = False
        self.project.design_delegated = delegate or self.project.design_delegated
        delegate = self.project.design_delegated
        if route in {"PROJECT_NEW", "PROJECT_CHANGE"} or not self.project.blueprint:
            self.project.apply_request(update.text, update.utterance_id)
            self.project.preparation = None
            if authorized or preserved:
                self.project.authorization = {
                    "source": update.utterance_id,
                    "duration_ms": duration
                    or (previous_auth or {}).get("duration_ms", 900000),
                    "generation": self.bridge.generation,
                    "supply": decision.get("supply", {}).get("noul", 0) >= 0.7
                    or (previous_auth or {}).get("supply", False),
                }
                if (
                    preserved
                    and (not duration)
                    and ((previous_auth or {}).get("deadline") is not None)
                ):
                    self.project.authorization["deadline"] = previous_auth["deadline"]
            if not self.project.site:
                self.project.site = site_from_snapshot(snapshot)
            if self.project.authorization:
                a = self.project.authorization
                a.setdefault(
                    "deadline", time.monotonic() + a.get("duration_ms", 900000) / 1000
                )
            self.design(update.utterance_id, delegate)
            self.r.spawn(self.persist(update.utterance_id))
        elif route == "PROJECT_START":
            if not authorized:
                self.say("是否让我按当前方案继续动手建造？")
                return
            self.project.authorization = {
                "source": update.utterance_id,
                "duration_ms": duration or 900000,
                "generation": self.bridge.generation,
                "supply": decision.get("supply", {}).get("noul", 0) >= 0.7,
            }
            if not self.project.site:
                self.project.site = site_from_snapshot(snapshot)
            self.begin(origin)

    def design(self, source, delegate=False):
        if self.job and self.job is not asyncio.current_task():
            self.job.cancel()
        self.pending_geometry = None
        project = self.project
        generation = self.generation
        revision = project.revision
        frozen = copy.deepcopy(project)
        snapshot = copy.deepcopy(self.bridge.snapshot)
        materials = self.materials()
        project.status = "PLANNING"
        self.emit()
        if not self.design_retries:
            self.say("我按你刚才的要求整理方案。")

        async def work():
            try:
                async with asyncio.timeout(60):
                    question = await intake(self.r.controller, frozen, delegate)
                    if question:
                        result = {
                            "title": frozen.title,
                            "requirements": frozen.requirements,
                            "question": question,
                            "plan": None,
                            "usable": False,
                            "review": {},
                        }
                    else:
                        self.r.post(
                            "voxel.project.progress",
                            {
                                "generation": generation,
                                "revision": revision,
                                "stage": "正在设计方块布局并检查入口和材料",
                            },
                        )
                        async with self.r.worker_slots:
                            result = await propose(
                                self.mode.reasoning,
                                self.r.controller,
                                frozen,
                                snapshot,
                                materials,
                                self.r.history,
                                delegate,
                            )
                        plan = result.get("plan")
                        if plan:
                            self.r.post(
                                "voxel.project.geometry_ready",
                                {
                                    "generation": generation,
                                    "revision": revision,
                                    "result": result,
                                },
                            )
                        if plan and (not frozen.completed):
                            sites = supported_sites(plan, snapshot)
                            chosen = await self.r.controller.judge_typed(
                                "voxel_construction_site",
                                {
                                    "requests": [
                                        r["text"] for r in frozen.requests[-5:]
                                    ],
                                    "size": list(plan.size),
                                    "initial_suggested_origin": frozen.site,
                                    "player": snapshot["view"]["self"]["position"],
                                },
                                {
                                    "site": {
                                        "type": "choice",
                                        "instructions": "Choose a supported clear nearby site for this structure. Each coordinate is a VERIFIED whole footprint, not just one block. Generic 建个房子/你来定 permits choosing nearby open ground. If the user demands an exact location absent from the candidates choose ASK; never substitute a different explicit site. Prefer a nearby footprint leaving the player outside so construction can start.",
                                        "criteria": {
                                            "ASK": "No suitable observed site or an exact requested location cannot be used",
                                            **{str(i): p for i, p in enumerate(sites)},
                                        },
                                    }
                                },
                                deadline_ms=1800,
                            )
                            index = chosen["site"]["choice"]
                            if sites and index == "ASK":
                                permission = await self.r.controller.judge_typed(
                                    "voxel_site_choice_scope",
                                    {
                                        "requests": [
                                            r["text"] for r in frozen.requests[-5:]
                                        ]
                                    },
                                    {
                                        "restricted": {
                                            "type": "noul",
                                            "instructions": "Did the player explicitly require a fixed exact building location or forbid choosing another nearby site? Generic build a shelter, find a place yourself, prepare land are NO.",
                                        }
                                    },
                                    deadline_ms=1800,
                                )
                                if permission["restricted"]["noul"] < 0.2:
                                    retry = await self.r.controller.judge_typed(
                                        "voxel_select_available_site",
                                        {
                                            "player": snapshot["view"]["self"][
                                                "position"
                                            ],
                                            "size": list(plan.size),
                                        },
                                        {
                                            "site": {
                                                "type": "choice",
                                                "instructions": "Choose a convenient nearby VERIFIED supported footprint for this authorized structure. Several equally good sites are fine. Every option has complete support and clearance for the whole structure.",
                                                "criteria": {
                                                    str(i): p
                                                    for i, p in enumerate(sites)
                                                },
                                            }
                                        },
                                        deadline_ms=1800,
                                    )
                                    index = retry["site"]["choice"]
                            if sites and index == "ASK":
                                async with self.r.worker_slots:
                                    review = await self.mode.reasoning.complete(
                                        [
                                            {
                                                "role": "system",
                                                "content": "Choose one build site. Return JSON {site:string,question:string|null}. All listed coordinates are VERIFIED clear supported WHOLE footprints. For generic build-a-house/you-decide, select any convenient listed site; do not ask for an exact location. Only an explicit incompatible human site requires site=ASK and a question. Never invent coordinates.",
                                            },
                                            {
                                                "role": "user",
                                                "content": json.dumps(
                                                    {
                                                        "requests": [
                                                            r["text"]
                                                            for r in frozen.requests[
                                                                -5:
                                                            ]
                                                        ],
                                                        "sites": {
                                                            str(i): p
                                                            for i, p in enumerate(sites)
                                                        },
                                                        "player": snapshot["view"][
                                                            "self"
                                                        ]["position"],
                                                    },
                                                    ensure_ascii=False,
                                                ),
                                            },
                                        ],
                                        json_mode=True,
                                        max_tokens=250,
                                        reasoning_effort="none",
                                    )
                                c = review["choices"][0]
                                v = json.loads(c["message"]["content"])
                                if c.get("finish_reason") == "stop" and isinstance(
                                    v.get("site"), str
                                ):
                                    index = v["site"]
                            if index.isdigit() and int(index) < len(sites):
                                plan = parse_blueprint(
                                    plan.public(), self.materials(), sites[int(index)]
                                )
                                result["plan"] = plan
                                result["site"] = plan.origin
                            else:
                                result["site_needed"] = True
                                result["question"] = None
                self.r.post(
                    "voxel.project.designed",
                    {
                        "result": result,
                        "generation": generation,
                        "revision": revision,
                        "source": source,
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.r.post(
                    "voxel.project.designed",
                    {
                        "error": str(error)[:150]
                        if isinstance(error, ValueError)
                        else type(error).__name__,
                        "service_failure": isinstance(
                            error,
                            (httpx.TransportError, httpx.HTTPStatusError, TimeoutError),
                        ),
                        "generation": generation,
                        "revision": revision,
                        "source": source,
                    },
                )

        self.job = self.r.spawn(work())

    def materials(self):
        c = (self.bridge.snapshot or {}).get("view", {}).get("construction", {})
        return {row["item_id"]: row["name"] for row in c.get("materials") or []}

    def plan(self):
        p = self.project
        return (
            parse_blueprint(p.blueprint, self.materials(), p.site)
            if p and p.blueprint
            else None
        )

    def preview_text(self, plan):
        dims = "×".join(map(str, plan.size))
        deficits = material_deficits(plan, self.bridge.snapshot, self.project.completed)
        needs = (
            "；现在还缺" + str(sum(deficits.values())) + "块材料"
            if deficits
            else "，现有材料够用"
        )
        return f"方案是{plan.title}，范围{dims}，共{len(plan.cells)}块{needs}。"

    def cue(self, question, evidence=None):
        return (
            "<|im_start|>user\n用简短自然的中文回答当前问题。仅依据提供的VoxeLibre事实，区分计划、用户要求和实际完成；不要重复问已知信息。\n"
            + json.dumps(
                {
                    "question": question,
                    "project": brief(self.project),
                    "game": decision_game(self.bridge.snapshot),
                    "activity": self.activity.context(),
                    "evidence": evidence,
                },
                ensure_ascii=False,
            )
            + "<|im_end|>"
        )

    def begin(self, origin):
        if self.project and self.project.status == "CANCELLED":
            return
        if self.job and (not self.job.done()):
            return
        plan = self.plan()
        if not plan:
            return
        if not self.bridge.snapshot["capabilities"].get("cooperative_build"):
            self.say("建造执行器尚未就绪，方案和要求已经保留。")
            return
        if not origin or not self.bridge.current(origin):
            self.say("请先进入游戏或开启旁观建造，再让我开始。")
            return
        if plan.origin is None:
            self.say("请先看向希望开始建造的地面，再说在这里开始。")
            return
        operations = getattr(self.mode, "operations", None)
        if (
            operations
            and operations.task
            and (operations.task.get("agent_goal") == "MATERIALS")
            and (operations.task.get("project_id") == self.project.project_id)
            and (operations.task.get("status") in {"RUNNING", "ARMING", "READY"})
        ):
            operations.task["materials_needed"] = plan.public()["materials"]
            self.project.status = "NEEDS_MATERIALS"
            self.emit()
            return
        if (
            self.project.status == "PREPARING_SITE"
            or (self.project.preparation or {}).get("phase") == "REQUIRED"
        ):
            if self.project.authorization and operations:
                operations.start_site(
                    self.project, copy.deepcopy(self.project.authorization)
                )
            return
        if material_deficits(plan, self.bridge.snapshot, self.project.completed):
            self.project.status = "NEEDS_MATERIALS"
            if (
                self.project.authorization
                and self.project.authorization.get("supply")
                and self.mode.operations
            ):
                auth = copy.deepcopy(self.project.authorization)
                self.say("建材还不够，我先找材料，准备好后继续按这个方案建。")
                self.mode.operations.start_supplies(self.project, auth)
            else:
                self.say(
                    self.preview_text(plan) + "可以让我找材料，也可以换材料或调整方案。"
                )
            self.emit()
            return
        if self.bridge.input_mode not in {"look", "watch"}:
            self.project.status = "READY"
            self.say("方案已准备好，点击“开始建造”即可旁观，也可以进入游戏后让我开始。")
            self.emit()
            return
        if not self.project.authorization:
            return
        if (
            self.project.authorization.get("generation", self.bridge.generation)
            != self.bridge.generation
        ):
            self.project.authorization = None
            self.project.status = "READY"
            self.say("操作状态已变化，方案已保留。需要继续时告诉我开始。")
            self.emit()
            return
        generation = self.generation
        project = self.project
        duration = project.authorization.get("duration_ms", 900000)
        project.authorization.setdefault("deadline", time.monotonic() + duration / 1000)
        duration = int((project.authorization["deadline"] - time.monotonic()) * 1000)
        if duration < 1000:
            self.stop("project_deadline")
            self.say("约定时间到了，方案和进度已保留。")
            return
        project.status = "ARMING"
        self.emit()

        async def arm():
            try:
                reply = await self.bridge.arm_project(project, plan, origin, duration)
                self.r.post(
                    "voxel.project.armed", {"result": reply, "generation": generation}
                )
            except Exception as error:
                self.r.post(
                    "voxel.project.armed",
                    {"error": type(error).__name__, "generation": generation},
                )

        self.job = self.r.spawn(arm())

    async def on_snapshot(self, s):
        await self.bind_world(s)
        p = self.project
        state = s.get("view", {}).get("construction", {}).get("project")
        if (
            p
            and p.authorization
            and (time.monotonic() >= p.authorization.get("deadline", float("inf")))
        ):
            self.stop("project_deadline")
            self.say("约定时间到了，方案和进度已保留。")
            await self.persist("project_deadline")
            return
        if (
            p
            and p.status == "RUNNING"
            and self.bridge.project_grant
            and (not self.bridge.fresh())
        ):
            return
        if (
            p
            and p.authorization
            and (
                p.authorization.get("generation", self.bridge.generation)
                != self.bridge.generation
            )
            and (p.status != "RUNNING")
        ):
            p.authorization = None
        if (
            p
            and p.status == "READY"
            and p.authorization
            and (not self.job or self.job.done())
            and (not holds_actions(self.mode))
        ):
            self.begin(self.bridge.origin(p.authorization["source"]))
        if p and p.status == "RUNNING":
            if not self.bridge.project_current():
                continuity = self.bridge.continuity(self.bridge.project_grant)
                if continuity == "wait":
                    return
                if continuity == "rearm":
                    if self.pending_action:
                        return
                    if self.action_job:
                        self.action_job.cancel()
                        self.action_job = None
                    self.generation += 1
                    p.authorization["source"] = (
                        p.authorization["source"][:55].split("-sync-")[0]
                        + "-sync-"
                        + uuid4().hex[:8]
                    )
                    p.status = "READY"
                    self.bridge.project_grant = None
                    self.r.emit(
                        "voxel.project.resuming",
                        project_id=p.project_id,
                        reason="native_transport_restored",
                    )
                    if not holds_actions(self.mode):
                        self.begin(self.bridge.origin(p.authorization["source"]))
                    return
                self.stop("control_revoked")
                self.emit()
                await self.persist("control_revoked")
            elif state and state.get("grant_id") == self.bridge.project_grant["id"]:
                if (
                    state.get("completed", 0) >= state.get("total", 1)
                    and (not self.action_job)
                    and (not self.pending_action)
                ):
                    p.status = "COMPLETED"
                    p.authorization = None
                    self.bridge.revoke("project_completed")
                    self.say(
                        "这份方案的方块已经确认放好，你可以检查效果，再告诉我哪里想调整。"
                    )
                    self.emit()
                    await self.persist("project_completed")
                elif (
                    not self.action_job
                    and (not self.pending_action)
                    and (time.monotonic() - self.last_step > 0.2)
                    and (time.monotonic() >= self.retry_after)
                    and (not holds_actions(self.mode))
                ):
                    actions = state.get("actions") or []
                    stalled = (
                        p.last_progress and time.monotonic() - p.last_progress > 18
                    )
                    if (
                        stalled
                        and (
                            not any(
                                (
                                    a["kind"]
                                    in {
                                        "PROJECT_PLACE",
                                        "PROJECT_JUMP_PLACE",
                                        "PROJECT_SELECT",
                                        "PROJECT_EQUIP",
                                    }
                                    for a in actions
                                )
                            )
                        )
                        and self.site_obstructions(s)
                    ):
                        await self.repair_site(s)
                        return
                    if actions:
                        self.blocked_since = None
                        self.action_job = self.r.spawn(
                            self.step(copy.deepcopy(s), actions)
                        )
                    elif state.get("status") != "INPUT":
                        self.blocked_since = self.blocked_since or time.monotonic()
                        if time.monotonic() - self.blocked_since > 3:
                            self.stop("no_legal_build_step")
                            self.say(
                                cue=self.cue(
                                    "说明当前站位、材料或可达范围造成的阻碍，提出一个有用的下一步。",
                                    state.get("blocked_reason"),
                                )
                            )
                            self.emit()
        if (
            self.activity.due()
            and (not self.activity_job)
            and (not self.job)
            and (not self.r.user_speaking)
            and (not self.r.route_pending)
            and (not self.r.response_id)
            and (
                not getattr(self.mode, "reply_job", None) or self.mode.reply_job.done()
            )
            and (not self.bridge.task_current())
            and (not (p and p.status in {"RUNNING", "ARMING"}))
        ):
            self.activity_job = self.r.spawn(self.review_activity(copy.deepcopy(s)))
        await self.flush()

    def site_obstructions(self, s):
        plan = self.plan()
        if not plan or not plan.origin:
            return []
        from .site import contains, working_region

        region = working_region(
            {
                "origin": plan.origin,
                "width": plan.size[0],
                "depth": plan.size[1],
                "height": plan.size[2],
            }
        )
        approved = {(c["x"], c["y"], c["z"]): c["item_id"] for c in plan.absolute()}
        nodes = list(s["view"].get("survival", {}).get("nodes", []))
        nodes += [
            r["obstacle"]
            for r in s["view"].get("construction", {}).get("survey", [])
            if r.get("obstacle")
        ]
        return [
            r
            for r in nodes
            if contains(region, r["position"])
            and approved.get(tuple((r["position"][k] for k in ("x", "y", "z"))))
            != r["item_id"]
        ]

    async def repair_site(self, s):
        p = self.project
        auth = copy.deepcopy(p.authorization)
        plan = self.plan()
        if not auth or not self.mode.operations:
            return
        self.r.emit(
            "voxel.project.site_repair",
            project_id=p.project_id,
            completed=p.completed_count(),
            obstructions=self.site_obstructions(s),
        )
        self.stop("repair_site")
        auth["generation"] = self.bridge.generation
        p.authorization = auth
        p.execution_intent = True
        p.preparation = {"phase": "REPAIR"}
        self.mode.operations.start_site(p, auth, repair=True)
        self.say("施工面有东西挡住了，我先处理这些障碍，再接着建原来的房子。")
        await self.persist(auth["source"])

    async def step(self, s, actions):
        self.last_step = time.monotonic()
        generation = self.generation
        project = self.project
        try:
            pos = s["view"]["self"]["position"]
            stance = tuple((round(pos[k] * 2) / 2 for k in ("x", "y", "z")))
            actions = [
                a
                for a in actions
                if not (
                    a["kind"] in {"PROJECT_PLACE", "PROJECT_JUMP_PLACE"}
                    and (stance, a.get("cell"), a["kind"]) in self.unreachable
                )
            ]
            if not actions:
                self.r.post(
                    "voxel.project.step_done", {"generation": generation, "pause": True}
                )
                return
            choices = {str(i): a for i, a in enumerate(actions)}
            types = {
                "PLACE": {"PROJECT_PLACE", "PROJECT_JUMP_PLACE"},
                "EQUIP": {"PROJECT_SELECT", "PROJECT_EQUIP"},
                "MOVE": {"PROJECT_MOVE"},
            }
            phase = await self.r.controller.judge_typed(
                "voxel_build_phase",
                {
                    "goal": "Finish the already approved building with real placements",
                    "can_place_now": sum(
                        (a["kind"] in types["PLACE"] for a in actions)
                    ),
                    "can_equip_material_now": any(
                        (a["kind"] in types["EQUIP"] for a in actions)
                    ),
                    "equip_choices": [
                        {k: v for k, v in a.items() if k in {"item_id", "slot"}}
                        for a in actions
                        if a["kind"] in types["EQUIP"]
                    ],
                    "equip_meaning": "These carried materials enable currently reachable remaining plan cells immediately; no new movement is needed to reach those cells.",
                    "current_material": s["view"]["self"].get("wielded"),
                    "completed": project.completed_count(),
                    "last_actions": [
                        {
                            k: v
                            for k, v in (self.last_action or {}).items()
                            if k in {"kind", "status"}
                        }
                    ],
                },
                {
                    "phase": {
                        "type": "choice",
                        "instructions": "Which next action most directly enables real placement of an approved remaining house block? PLACE builds now. EQUIP changes held material to one already in inventory and immediately enables reachable placements at the current stance. MOVE only changes stance; its future opportunities still need the right material. Reposition when neither placement nor a suitable equip is currently available.",
                        "criteria": {
                            key: description
                            for key, description in {
                                "PLACE": "Place a currently reachable approved block",
                                "EQUIP": "Select the material required for reachable cells",
                                "MOVE": "Move to reach unfinished parts",
                            }.items()
                            if any((a["kind"] in types[key] for a in actions))
                        },
                    }
                },
                deadline_ms=1200,
            )
            selected_phase = phase["phase"]["choice"]
            if selected_phase in types and phase["phase"]["confidence"] >= 0.6:
                choices = {
                    k: a
                    for k, a in choices.items()
                    if a["kind"] in types[selected_phase]
                }
            if not choices:
                return
            plan = self.plan()
            pos = s["view"]["self"]["position"]
            origin = plan.origin
            exits = [
                {"x": x + origin["x"], "z": z + origin["z"]}
                for x, z, _ in plan.geometry_facts()["openings_at_perimeter"]
            ]
            current_count = project.completed_count()
            from .locomotion import cell

            unproductive = [
                dict(zip(("x", "y", "z"), map(float, k.split(","))))
                for k, n in project.work_visits.items()
                if n
            ]
            from .navigation import exterior_distances

            completed = {
                tuple((r["target"][k] for k in ("x", "y", "z")))
                for r in project.completed
                if r.get("target") and (not r.get("removed"))
            }
            remaining = [
                {
                    "position": {k: r[k] for k in ("x", "y", "z")},
                    "item_id": r["item_id"],
                }
                for r in plan.absolute()
                if tuple((r[k] for k in ("x", "y", "z"))) not in completed
            ]
            distances, stations = exterior_distances(
                plan, project.completed, remaining, actions, unproductive
            )
            move_facts = {}
            for key, a in choices.items():
                if a["kind"] != "PROJECT_MOVE":
                    continue
                target = a["target"]
                inside = (
                    origin["x"] <= target["x"] < origin["x"] + plan.size[0]
                    and origin["z"] <= target["z"] < origin["z"] + plan.size[1]
                )
                move_facts[key] = {
                    "inside_plan_footprint": inside,
                    "visits_without_building_progress": project.work_visits.get(
                        cell(target), 0
                    ),
                    "steps_around_built_walls_to_unfinished_exterior": distances.get(
                        key
                    ),
                    "distance_to_doorway": min(
                        (
                            abs(target["x"] - p["x"]) + abs(target["z"] - p["z"])
                            for p in exits
                        ),
                        default=None,
                    ),
                }
            option_facts = {
                key: {
                    **{k: v for k, v in a.items() if k not in {"action_id", "label"}},
                    **move_facts.get(key, {}),
                }
                for key, a in choices.items()
            }
            state = {
                "project": {
                    "title": project.title,
                    "requirements": {
                        k: v["value"]
                        for k, v in project.requirements.items()
                        if isinstance(v, dict) and v.get("source") != "model_default"
                    },
                    "site": project.site,
                },
                "self": {
                    k: s["view"]["self"].get(k) for k in ("position", "wielded", "hp")
                },
                "remaining": remaining[:24],
                "movement_facts": move_facts,
                "doorways": exits,
                "current_inside_footprint": origin["x"]
                <= pos["x"]
                < origin["x"] + plan.size[0]
                and origin["z"] <= pos["z"] < origin["z"] + plan.size[1],
                "recent_moves": list(self.moves)[-5:],
                "last_action": {
                    k: v
                    for k, v in (self.last_action or {}).items()
                    if k in ("kind", "target", "status")
                },
                "recent_completed": [
                    r["target"] for r in project.completed[-12:] if not r.get("removed")
                ],
            }
            answer = await self.r.controller.judge_typed(
                "voxel_build_step",
                state,
                {
                    "action": {
                        "type": "choice",
                        "instructions": "Choose the next step to finish the approved plan. Prefer PROJECT_PLACE/PROJECT_JUMP_PLACE that completes a cell now. PROJECT_SELECT when material is not held. For movement, useful_placements counts opportunities AFTER moving. When all moves have zero placement opportunities INSIDE the building, go THROUGH THE DOORWAY TO OUTSIDE to reach exterior roof/walls. Use doorway distances and inside_plan_footprint to continue OUT, not bounce back inside. Avoid options with visits_without_building_progress when unvisited moves exist. Moving farther from nearest unfinished cell can be necessary to go around a wall. WAIT only when all approaches failed. Select numeric key.",
                        "criteria": {"WAIT": "No useful safe progress", **option_facts},
                    },
                    "need_question": {
                        "type": "noul",
                        "instructions": "Is a NEW user design decision essential before continuing (contradiction/material tradeoff), rather than a routine execution choice already delegated?",
                    },
                },
                deadline_ms=2000,
            )
            if (
                generation != self.generation
                or not self.bridge.project_current()
                or holds_actions(self.mode)
            ):
                return
            selected = answer["action"]["choice"]
            navigation_resolved = False
            if (
                all((a["kind"] == "PROJECT_MOVE" for a in choices.values()))
                and stations
            ):
                immediate = {
                    k: v
                    for k, v in option_facts.items()
                    if v.get("useful_placements", 0) > 0
                }
                navigation_options = immediate or option_facts
                navigation = await self.r.controller.judge_typed(
                    "voxel_build_navigation",
                    {
                        "objective": "Reach the unfinished exterior via legal adjacent movement. Shorter path distance goes around the walls; direct Euclidean distance can point through a wall.",
                        "exterior_work_positions_from_our_plan": stations,
                    },
                    {
                        "action": {
                            "type": "choice",
                            "instructions": "Select a legal next movement advancing toward unfinished exterior work. Prefer fewer steps_around_built_walls_to_unfinished_exterior; this accounts for walls and doorway detours. Zero means reach a work position. Avoid repeated pointless movement. It is fine to revisit a corridor on the way out. WAIT only if all estimates unavailable.",
                            "criteria": {
                                "WAIT": "No useful route",
                                **navigation_options,
                            },
                        }
                    },
                    deadline_ms=1800,
                )
                selected = navigation["action"]["choice"]
                navigation_resolved = navigation["action"]["confidence"] >= 0.65
            selected_fact = move_facts.get(selected, {})
            repeated_move = selected_fact.get(
                "visits_without_building_progress", 0
            ) >= 2 and any(
                (
                    row["visits_without_building_progress"] == 0
                    for row in move_facts.values()
                )
            )
            if repeated_move or (
                not navigation_resolved
                and selected == "WAIT"
                and (answer["action"]["confidence"] < 0.65)
            ):
                async with self.r.worker_slots, asyncio.timeout(20):
                    response = await self.mode.reasoning.complete(
                        [
                            {
                                "role": "system",
                                "content": "Select one next step for this already-authorized construction from options only. Return JSON {choice:string,reason:string}. If the player occupies an unfinished cell, move off it; approach another side for blocked faces. Prefer legal placement now over pointless movement. WAIT only for a real blocker. You cannot authorize or invent actions.",
                            },
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "state": state,
                                        "options": option_facts,
                                        "repeated_movement_without_progress": repeated_move,
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        ],
                        json_mode=True,
                        max_tokens=1200,
                        reasoning_effort="none",
                    )
                choice = response["choices"][0]
                if choice.get("finish_reason") != "stop":
                    raise ValueError("Incomplete step review")
                review = json.loads(choice["message"]["content"])
                selected = review.get("choice")
                if selected not in {"WAIT", *choices}:
                    raise ValueError("Unknown reviewed action")
                self.r.emit(
                    "voxel.project.step_reviewed",
                    choice=selected,
                    reason=str(review.get("reason", ""))[:300],
                )
                if (
                    generation != self.generation
                    or not self.bridge.project_current()
                    or holds_actions(self.mode)
                ):
                    return
            if selected == "WAIT" or answer["need_question"]["noul"] > 0.85:
                self.r.post(
                    "voxel.project.step_done", {"generation": generation, "pause": True}
                )
                return
            action = choices.get(selected)
            if action is None:
                return
            fresh = (
                (self.bridge.snapshot or {})
                .get("view", {})
                .get("construction", {})
                .get("project")
                or {}
            ).get("actions", [])
            identity = ("kind", "item_id", "target", "slot", "cell")
            current = next(
                (
                    a
                    for a in fresh
                    if all((a.get(k) == action.get(k) for k in identity))
                ),
                None,
            )
            if current is None:
                return
            action = current
            s = copy.deepcopy(self.bridge.snapshot)
            self.pending_action = True
            reply = await self.bridge.project_step(action, s)
            self.r.post(
                "voxel.project.step_done",
                {
                    "generation": generation,
                    "project_id": project.project_id,
                    "action": action,
                    "result": reply,
                    "stance": stance,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.r.post(
                "voxel.project.step_done",
                {
                    "generation": generation,
                    "error": str(error)[:150]
                    if isinstance(error, ValueError)
                    else type(error).__name__,
                },
            )
        finally:
            if self.action_job is asyncio.current_task():
                self.action_job = None

    async def review_activity(self, s):
        a = self.activity
        a.last_request = time.monotonic()
        version = a.version
        generation = self.generation
        facts = a.context()
        try:
            decision = await judge_activity(
                self.r.controller,
                s,
                a,
                self.project,
                self.r.history,
                self.mode.memory.landmarks,
            )
            self.r.post(
                "voxel.project.activity_done",
                {
                    "decision": decision,
                    "version": version,
                    "generation": generation,
                    "facts": facts,
                    "snapshot": s,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            a.last_judged = version
            self.r.emit("voxel.activity.failed", reason=type(error).__name__)
        finally:
            if self.activity_job is asyncio.current_task():
                self.activity_job = None

    async def handle(self, kind, p):
        if not kind.startswith("voxel.project."):
            return False
        if kind == "voxel.project.step_done":
            self.pending_action = False
            if (
                p.get("generation") != self.generation
                and self.project
                and (p.get("project_id") == self.project.project_id)
            ):
                action = p.get("action", {})
                reply = p.get("result", {})
                if reply.get("status") == "SUCCEEDED" and action.get("kind") in {
                    "PROJECT_PLACE",
                    "PROJECT_JUMP_PLACE",
                }:
                    self.project.completed.append(
                        {
                            "cell": action["cell"],
                            "target": action["target"],
                            "item_id": action["item_id"],
                            "command_id": reply.get("command_id"),
                        }
                    )
                    await self.persist(reply.get("command_id", "receipt"))
                    self.emit()
        if p.get("generation") != self.generation:
            return True
        if kind == "voxel.project.geometry_ready":
            if self.project and p["revision"] == self.project.revision:
                self.pending_geometry = p["result"]
            return True
        if kind == "voxel.project.progress":
            if self.project and self.project.revision == p["revision"]:
                self.r.emit(
                    "voxel.project.progress",
                    project_id=self.project.project_id,
                    stage=p["stage"],
                )
        elif kind == "voxel.project.designed":
            self.job = None
            if not self.project or p["revision"] != self.project.revision:
                return True
            if p.get("error"):
                if p.get("service_failure") and getattr(self, "pending_geometry", None):
                    result = self.pending_geometry
                    self.pending_geometry = None
                    result = {**result, "site_needed": True, "question": None}
                    return await self.handle(
                        kind, {**p, "error": None, "result": result}
                    )
                auth = self.project.authorization
                if (
                    p.get("service_failure")
                    and auth
                    and (auth.get("generation") == self.bridge.generation)
                ):
                    auth.setdefault(
                        "deadline",
                        time.monotonic() + auth.get("duration_ms", 900000) / 1000,
                    )
                    if time.monotonic() >= auth["deadline"]:
                        self.stop("project_deadline")
                        self.say("约定时间到了，设计要求和已有进度已保留。")
                        return True
                    self.design_retries += 1
                    generation = self.generation
                    self.r.emit(
                        "voxel.project.design_retry",
                        reason=p["error"],
                        attempt=self.design_retries,
                    )
                    if self.design_retries == 1:
                        self.say(
                            "设计服务刚才没有及时返回，我会自动重试，要求和进度都保留着。"
                        )

                    async def retry():
                        await asyncio.sleep(min(8, 2 ** min(self.design_retries, 3)))
                        if (
                            generation == self.generation
                            and self.project.authorization is auth
                        ):
                            self.job = None
                            self.design(p["source"], self.project.design_delegated)

                    self.job = self.r.spawn(retry())
                    return True
                self.project.status = "DISCUSSING"
                self.r.emit("voxel.project.plan_failed", reason=p["error"])
                message = (
                    "设计服务暂时不可用，尺寸和材料要求都已保留。你可以说继续，我会重新生成。"
                    if p.get("service_failure")
                    else "方案没有通过尺寸或可进入性检查，要求已保留。你可以让我重新生成。"
                )
                self.project.question = {
                    "key": "plan_retry",
                    "question": message,
                    "reason": p["error"],
                }
                self.say(message)
                self.emit()
                return True
            self.design_retries = 0
            result = p["result"]
            project = self.project
            plan = result["plan"]
            if result.get("site"):
                project.site = result["site"]
            project.title = result["title"]
            project.requirements = result["requirements"]
            project.question = result["question"]
            project.blueprint = plan.public() if plan else None
            if result.get("needs_materials"):
                project.status = "NEEDS_MATERIALS"
                project.question = {
                    "key": "materials",
                    "question": "当前缺少"
                    + "、".join(result["needs_materials"])
                    + "。设计要求已记住，可以先备材料、换材料，或让我先出方案。",
                    "reason": "requested_material_unavailable",
                }
                self.say(
                    cue=self.cue(
                        "直接说明当前缺少所选建材；用简短中文给出准备材料、换材料或先看方案的选择。",
                        {"missing": result["needs_materials"]},
                    )
                )
            elif project.question:
                project.status = "DISCUSSING"
                self.say(project.question["question"])
            elif plan and result.get("site_needed"):
                project.status = "PREPARING_SITE"
                project.preparation = {"phase": "REQUIRED"}
                if project.authorization and self.mode.operations:
                    self.say(
                        "房子方案记住了，我先寻找并整理能放下它的场地，准备好后继续建。"
                    )
                    self.mode.operations.start_site(
                        project, copy.deepcopy(project.authorization)
                    )
                else:
                    project.status = "READY"
                    self.say(
                        "房子方案已经记住，还需要找一块能放下它的场地。可以让我连同找地一起完成。"
                    )
            elif plan and result["usable"]:
                project.status = "READY"
                self.say(self.preview_text(plan))
                self.emit()
                if (
                    project.authorization
                    and (not self.r.user_speaking)
                    and (not self.r.route_pending)
                ):
                    self.begin(self.bridge.origin(project.authorization["source"]))
            else:
                project.status = "DISCUSSING"
                self.say("方案还不足以满足你的要求，请补充最在意的效果，或让我调整。")
            await self.persist(p["source"])
            self.emit()
        elif kind == "voxel.project.armed":
            self.job = None
            if p.get("result", {}).get("status") == "ARMED" and (
                self.bridge.project_current()
                or self.bridge.continuity(self.bridge.project_grant)
                in {"wait", "rearm"}
            ):
                self.project.status = "RUNNING"
                self.last_step = 0
                self.moves.clear()
                self.rejections = 0
                self.blocked_since = None
                self.project.last_progress = time.monotonic()
                if not self.project.completed_count():
                    self.say("开始建造，你随时可以改要求或说“我自己来”。")
            elif (
                p.get("result", {}).get("reason")
                in {"native_client_not_ready", "source_expired"}
                and self.project.authorization
                and (
                    self.project.authorization.get("generation")
                    == self.bridge.generation
                )
            ):
                self.project.status = "READY"
            else:
                self.project.status = "PAUSED"
                self.say("当前站位或控制状态变了，请停稳后让我继续。")
            self.emit()
        elif kind == "voxel.project.step_done":
            if p.get("pause"):
                self.stop("jev_build_pause")
                self.say(cue=self.cue("说明当前建造的阻碍，只问一个必要的问题。"))
                self.emit()
                return True
            if p.get("error"):
                self.service_errors += 1
                self.retry_after = time.monotonic() + min(
                    5, 0.5 * 2 ** min(self.service_errors, 4)
                )
                self.r.emit("voxel.project.step_failed", reason=p["error"], retry=True)
                if self.service_errors == 3:
                    self.say(
                        "施工判断暂时没有返回，我会自动重试，已经建好的部分和要求都保留着。"
                    )
                return True
            else:
                self.service_errors = 0
                self.retry_after = 0.0
                action = p["action"]
                reply = p["result"]
                self.last_action = {**action, "status": reply.get("status")}
                self.r.emit(
                    "voxel.project.step", action=action, result=reply, provider="jev"
                )
                if reply.get("status") == "SUCCEEDED":
                    self.rejections = 0
                    if action["kind"] in {"PROJECT_PLACE", "PROJECT_JUMP_PLACE"}:
                        self.project.completed.append(
                            {
                                "cell": action["cell"],
                                "target": action["target"],
                                "item_id": action["item_id"],
                                "command_id": reply.get("command_id"),
                            }
                        )
                        self.project.last_progress = time.monotonic()
                        self.project.work_visits.clear()
                    if action["kind"] == "PROJECT_MOVE":
                        self.moves.append(
                            {
                                "target": action["target"],
                                "completed_after": self.project.completed_count(),
                            }
                        )
                        from .locomotion import cell

                        key = cell(action["target"])
                        self.project.work_visits[key] = (
                            self.project.work_visits.get(key, 0) + 1
                        )
                    if time.monotonic() - self.last_saved > 5:
                        await self.persist(reply.get("command_id", "step"))
                elif reply.get("status") == "REJECTED" and reply.get("reason") in {
                    "camera_alignment_pending",
                    "camera_not_aligned",
                    "jump_did_not_reach_face",
                    "no_visible_support",
                    "out_of_reach",
                    "input_target_not_reached",
                    "candidate_expired",
                    "native_state_changed",
                    "move_no_longer_legal",
                    "player_moving",
                }:
                    if action["kind"] in {
                        "PROJECT_PLACE",
                        "PROJECT_JUMP_PLACE",
                    } and p.get("stance"):
                        self.unreachable[
                            p["stance"], action.get("cell"), action["kind"]
                        ] = reply["reason"]
                    self.rejections = 0
                elif reply.get("status") in {
                    "REJECTED",
                    "CANCELLED",
                } and self.bridge.continuity(self.bridge.project_grant) in {
                    "wait",
                    "rearm",
                }:
                    self.rejections = 0
                elif (
                    reply.get("status") == "CANCELLED"
                    and reply.get("reason") == "voice_attention"
                ):
                    self.rejections = 0
                elif reply.get("status") in {"UNKNOWN", "CANCELLED"}:
                    self.rejections = 3
                else:
                    self.rejections += 1
            if self.rejections >= 3:
                self.stop("project_step_failed")
                self.say("连续步骤没有确认成功，已暂停并保留进度。")
            self.emit()
        elif kind == "voxel.project.activity_done":
            a = self.activity
            a.last_judged = p["version"]
            decision = p["decision"]
            facts = p["facts"]
            if p["version"] != a.version:
                return True
            self.r.emit(
                "voxel.activity.judged", judgments=decision, version=p["version"]
            )
            meaningful = (
                decision["importance"]["score"] >= 2
                and decision["grounded"]["noul"] >= 0.8
            )
            if meaningful:
                observed = {
                    "meaning": decision["meaning"]["choice"],
                    "bounds": facts["bounds"],
                    "count": facts["count"],
                    "events": facts["events"][-4:],
                }
                if self.project:
                    self.project.observations = (
                        self.project.observations + [observed]
                    )[-8:]
                    await self.persist("observed_build_" + str(p["version"]))
                else:
                    await self.mode.memory.save(
                        "landmark",
                        "玩家建造的已观察进展："
                        + json.dumps(
                            {k: observed[k] for k in ("meaning", "bounds", "count")},
                            ensure_ascii=False,
                        ),
                        "observed_build_" + str(p["version"]),
                        p["snapshot"]["view"]["self"]["position"],
                        provenance="native_observation",
                        evidence_ids=[e["event_id"] for e in facts["events"]],
                    )
            speak = decision["speak"]["choice"]
            if (
                self.r.proactive_enabled
                and speak != "SILENT"
                and (decision["grounded"]["noul"] >= 0.8)
                and (
                    time.monotonic() - max(a.last_spoken, self.mode.last_spoken_at)
                    >= 60
                )
            ):
                a.last_spoken = time.monotonic()
                a.last_feedback = {"kind": speak, "version": p["version"]}
                generation = self.generation
                version = a.version

                async def respond_activity():
                    try:
                        reply = await self.mode.reasoning.complete(
                            [
                                {
                                    "role": "system",
                                    "content": "Be a natural Chinese game companion. Comment on the HUMAN construction in one short complete sentence. Mention a concrete visible shape/material/progress and, if useful, one next idea. Do not recite coordinates, records, APIs, evidence checks or disclaimers. Do not claim a completed house from a partial wall. This is friendly observation, not a formal audit.",
                                },
                                {
                                    "role": "user",
                                    "content": json.dumps(
                                        {
                                            "response_kind": speak,
                                            "observed_human_work": facts,
                                            "player_preferences": self.mode.memory.context(),
                                        },
                                        ensure_ascii=False,
                                    ),
                                },
                            ],
                            max_tokens=250,
                            reasoning_effort="none",
                            latency_sensitive=True,
                        )
                        text = reply["choices"][0]["message"].get("content", "").strip()
                        if (
                            reply["choices"][0].get("finish_reason") == "stop"
                            and text
                            and (generation == self.generation)
                            and (version == a.version)
                        ):
                            self.say(text, activity=True)
                        elif generation == self.generation:
                            a.last_spoken = 0.0
                            a.last_judged = min(a.last_judged, version)
                    except Exception as error:
                        self.r.emit(
                            "voxel.activity.response_failed",
                            reason=type(error).__name__,
                        )

                self.r.spawn(respond_activity())
        return True
