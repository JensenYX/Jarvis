import asyncio
import base64
from .decision.base import DecisionInput, Option, Question


def questions(game):
    fields = (
        {
            "support": (
                "根据自己的底部HUD及游戏世界位置，判断附近己方英雄能否直接支援。镜头中心可能不是自己。看不到自己的位置或队友距离就unknown。",
                [
                    ("near", "自己附近有可见己方英雄"),
                    ("far", "可见自己与己方英雄相距明显较远"),
                    ("unknown", "无法可靠判断自己与队友距离"),
                ],
            ),
            "engagement": (
                "只判断自己正在参与的可见交战，不将别处特效或小兵当成英雄交战。",
                [
                    ("fighting", "自己正在与可见敌方英雄交战"),
                    ("disengaged", "自己当前未在与敌方英雄交战"),
                    ("unknown", "无法可靠判断"),
                ],
            ),
        }
        if game == "lol"
        else {
            "fire": (
                "第一人称前方可通行的地面是否有正在燃烧的燃烧弹火区？装饰、枪口闪光和烟雾不是火区。",
                [
                    ("present", "前方地面有可见的燃烧火区"),
                    ("absent", "前方地面没有可见燃烧火区"),
                    ("unknown", "无法可靠辨认"),
                ],
            ),
            "smoke": (
                "前方视线是否被可见的浓烟幕遮住？不要把火焰、阴影或普通雾气当成烟幕。",
                [
                    ("blocked", "前方视线被可见烟幕遮挡"),
                    ("clear", "前方视线没有被烟幕遮挡"),
                    ("unknown", "无法可靠辨认"),
                ],
            ),
        }
    )
    return tuple(
        (
            Question(
                id=key,
                primitive="choice",
                instructions=instruction,
                options=tuple((Option(id=name, text=text) for name, text in options)),
            )
            for key, (instruction, options) in fields.items()
        )
    )


async def propose(runtime, frames, update=None, *, backend=None):
    from .life_state import request_for as life_request
    from .perception import VisualProposal, ProposedFact

    frame = frames[-1]
    observation = frame.observation
    images = (
        (await asyncio.to_thread(life_request, frame.payload)).images
        if runtime.game == "lol"
        else (
            "data:"
            + observation.mime_type
            + ";base64,"
            + base64.b64encode(frame.payload).decode(),
        )
    )
    request = DecisionInput(
        state={
            "game": runtime.game,
            "user_request": None,
            "player_identity_binding": {
                "source": "explicit_replay_role",
                "scope": "displayed_HUD_controlled_avatar",
            },
            "image_layout_note": "完整最新帧；第二图如有，是同一帧底部HUD放大，不是第二个时刻。",
        },
        images=images,
        observation_ids=(observation.observation_id,),
        questions=questions(runtime.game),
    )
    decision_task = (backend or runtime.policy.backend).decide(request, timeout_s=1.8)
    reader = runtime.extractor.life_reader
    if runtime.game == "lol" and reader:
        result, life = await asyncio.gather(
            decision_task,
            reader.read(frame.payload, observation.observation_id, timeout_s=1.8),
        )
    else:
        result, life = (await decision_task, None)
    facts = []
    unknowns = []
    field_facts = {}
    threshold = runtime.policy.threshold
    for question, answer in zip(request.questions, result.answers):
        confidence = answer.raw_probs[answer.option_ids.index(answer.choice)]
        if answer.choice == "unknown" or confidence < threshold:
            unknowns.append(question.id)
            continue
        text = next((o.text for o in question.options if o.id == answer.choice))
        facts.append(
            ProposedFact(
                text=text,
                subject="user",
                status="observed",
                kind="scene",
                evidence_refs=(observation.observation_id,),
                valid_for_ms=3000,
            )
        )
        field_facts[question.id] = facts[-1].model_dump(mode="json")
    if life and life["admitted"]:
        text = {
            "dead": "自己已死亡",
            "low": "自己的生命条不超过一半",
            "alive": "自己的生命条超过一半",
        }[life["state"]]
        facts.insert(
            0,
            ProposedFact(
                text=text,
                subject="user",
                status="observed",
                kind="hud",
                evidence_refs=(observation.observation_id,),
                valid_for_ms=3000,
            ),
        )
        field_facts["life"] = facts[0].model_dump(mode="json")
    elif runtime.game == "lol":
        unknowns.append("own_life_state")
    proposal = VisualProposal(
        facts=tuple(facts),
        unknowns=tuple(unknowns),
        candidate_reply="；".join((f.text for f in facts)),
        intent="ANSWER",
        event_description="requested_visual_state",
        temporal_mode="current",
        max_spoken_duration_ms=6000,
    )
    return (
        proposal,
        request,
        {
            "decision": result.model_dump(mode="json"),
            "life": life,
            "field_facts": field_facts,
            "observation_records": [observation.model_dump(mode="json")],
            "model": "Jev-Omni/unified",
            "scope": "typed_visual_evidence_not_authored_dialogue",
        },
    )


class StateObserver:
    def __init__(self, runtime):
        self.runtime = runtime
        self.settings = (
            runtime.config["screen"].get("jev_venus", {}).get("state_observer", {})
        )
        self.task = None
        self.latest = None
        self.last_started = float("-inf")
        self.backend = None
        from .life_events import LifeEvents

        self.life_events = LifeEvents(runtime)
        if self.settings.get("enabled"):
            from .decision.jev_omni import JevOmniBackend

            self.backend = JevOmniBackend(
                self.settings["endpoint"], priority="background"
            )

    def invalidate(self):
        self.latest = None
        self.life_events.invalidate()
        if self.task and (not self.task.done()):
            self.task.cancel()

    def start(self, frame):
        from .capture import now_ms

        rt = self.runtime
        if (
            self.backend is None
            or frame is None
            or rt.capture.paused
            or rt.muted
            or (self.task and (not self.task.done()))
            or (now_ms() - self.last_started < self.settings.get("interval_ms", 1000))
        ):
            return
        self.last_started = now_ms()
        self.task = rt.spawn(self.observe(frame))

    async def observe(self, frame):
        from .capture import now_ms

        rt = self.runtime
        binding, epoch, control = (
            rt.binding(),
            rt.screen_session_epoch,
            rt.playback_control_epoch,
        )
        try:
            result = await propose(rt, (frame,), backend=self.backend)
        except Exception as error:
            rt.emit("screen.state_observer.failed", error=type(error).__name__)
            return
        if (
            binding != rt.binding()
            or epoch != rt.screen_session_epoch
            or control != rt.playback_control_epoch
            or rt.capture.paused
        ):
            return
        self.latest = {
            "prepared": result,
            "frames": (frame,),
            "binding": binding,
            "epoch": epoch,
            "control": control,
        }
        rt.emit(
            "screen.state_observer.observed",
            observation_id=frame.observation.observation_id,
            capture_age_ms=now_ms() - frame.observation.stamp.capture_ts_ms,
            proposal=result[0].model_dump(mode="json"),
            raw=result[2],
        )
        self.life_events.observe(frame, result[2].get("life"))

    def take(self):
        from .capture import now_ms

        rt = self.runtime
        item = self.latest
        if not item:
            return None
        age = now_ms() - item["frames"][-1].observation.stamp.capture_ts_ms
        if (
            item["binding"] != rt.binding()
            or item["epoch"] != rt.screen_session_epoch
            or item["control"] != rt.playback_control_epoch
            or rt.capture.paused
            or (not 0 <= age <= self.settings.get("max_age_ms", 1800))
        ):
            return None
        return item

    async def close(self):
        self.invalidate()
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)
        if self.backend:
            await self.backend.close()
