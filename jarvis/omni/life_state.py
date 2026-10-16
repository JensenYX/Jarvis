import asyncio
import base64
import hashlib
import io
import re
from PIL import Image
from .decision.base import DecisionInput, Question, Option

STATES = ("dead", "low", "alive", "unknown")
QUESTION = "根据实际截图判断底部HUD控制角色的生命状态。第二图是同一帧下方HUD放大。死亡看角色头像复活倒计时、灰色世界和死亡界面，不能把技能冷却或回城进度当死亡。已死亡时生命数字可能残留少量非零。存活时只看自己的绿色生命条占比，不看资源、金币或其他英雄。角色选择、加载、桌面以及看不清自己的HUD均为unknown。"


def request_for(binary, *, observation_ids=()):
    im = Image.open(io.BytesIO(binary)).convert("RGB")
    w, h = im.size
    crop = im.crop((int(w * 0.18), int(h * 0.64), int(w * 0.78), h))
    crop = crop.resize((crop.width * 2, crop.height * 2))
    images = []
    for picture in (im, crop):
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG")
        images.append(
            "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
        )
    return DecisionInput(
        state={"game": "lol", "image_layout": "完整帧，及同帧HUD放大"},
        observation_ids=tuple(observation_ids),
        images=tuple(images),
        questions=(
            Question(
                id="life_state",
                primitive="choice",
                instructions=QUESTION,
                options=tuple(
                    (
                        Option(id=k, text=v)
                        for k, v in zip(
                            STATES,
                            (
                                "自己已死亡，正在等待复活。",
                                "自己存活，绿色生命条只剩一半或更少。",
                                "自己存活，绿色生命条超过一半。",
                                "画面不足，不能确认自己的生命状态。",
                            ),
                        )
                    )
                ),
            ),
        ),
    )


class LifeStateReader:
    def __init__(self, settings):
        from .decision.jev_omni import JevOmniBackend

        self.backend = JevOmniBackend(settings["endpoint"])
        self.adapter_sha256 = settings["adapter_sha256"]
        self.threshold = settings.get("threshold", 0.8)
        self.cache = {}

    async def read(self, binary, observation_id, *, timeout_s=1.5):
        digest = hashlib.sha256(binary).hexdigest()
        key = (observation_id, digest)
        if key in self.cache:
            return self.cache[key]
        request = await asyncio.to_thread(
            request_for, binary, observation_ids=(observation_id,)
        )
        result = await self.backend.decide(request, timeout_s=timeout_s)
        if (
            result.input_hash != request.input_hash()
            or result.raw.get("adapter_sha256") != self.adapter_sha256
            or len(result.answers) != 1
        ):
            raise ValueError("Unbound life-state checkpoint or input")
        answer = result.answers[0]
        if (
            answer.question_id != "life_state"
            or answer.primitive != "choice"
            or answer.option_ids != STATES
            or (answer.choice not in STATES)
        ):
            raise ValueError("Invalid life-state classification")
        confidence = answer.raw_probs[STATES.index(answer.choice)]
        record = {
            "source_observation_id": observation_id,
            "image_sha256": digest,
            "input_hash": result.input_hash,
            "adapter_sha256": self.adapter_sha256,
            "state": answer.choice,
            "raw_probabilities": dict(zip(STATES, answer.raw_probs)),
            "admitted": confidence >= self.threshold and answer.choice != "unknown",
            "confidence_kind": "uncalibrated_model_probability",
            "scope": "own HUD life state only; no identity, kill, team, ability or location evidence",
        }
        if len(self.cache) >= 128:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = record
        return record

    async def close(self):
        await self.backend.close()


def pure_life_event(text):
    text = re.sub("\\s+", "", text).strip("。！!，,")
    text = re.sub("^(?:哎呀|哎|唉|啊)[，,！!]*", "", text)
    for _ in range(2):
        text = re.sub("^(?:你|我们|咱们|我方角色|角色|刚才|刚刚|这波|那波)", "", text)
    if re.fullmatch("(?:阵亡|倒下|倒|被击杀)了(?:啊|呀)?", text):
        return "dead"
    if re.fullmatch("复活了(?:啊|呀)?", text):
        return "alive"
    if re.fullmatch(
        "(?:掉血了|(?:血量|生命值|血)(?:(?:掉|降)(?:下来了|下去了|到(?:了)?(?:一半|半血)以下(?:了)?)|(?:下降|降低|变低|减少)了))(?:啊|呀)?",
        text,
    ):
        return "low"
    return None


def certified_transition(
    proposal, evidence, *, adapter_sha256, threshold, delivered, preferences
):
    from .controls import STYLE_TEXT

    state = evidence.state
    if state.get("user_request") is not None or not preferences:
        return None
    preference = preferences[-1]
    if (
        preference.get("source") != "user_interface"
        or preference.get("source_id") != "style_selector"
        or preference.get("text") != STYLE_TEXT["active"]
    ):
        return None
    binding = state.get("player_identity_binding") or {}
    if (
        binding.get("source") != "explicit_replay_role"
        or binding.get("scope") != "displayed_HUD_controlled_avatar"
    ):
        return None
    target = pure_life_event(proposal.candidate_reply)
    if (
        target is None
        or proposal.temporal_mode != "historical"
        or len(proposal.facts) != 1
    ):
        return None
    fact = proposal.facts[0]
    if (
        fact.text != proposal.candidate_reply
        or fact.kind != "past_event"
        or fact.status != "observed"
    ):
        return None
    readings = state.get("pixel_life_states") or []
    if len(readings) != 2:
        return None
    records = {r["id"]: r for r in state["observations"]}
    for reading in readings:
        source = records.get(reading.get("source_observation_id"))
        if (
            source is None
            or reading.get("adapter_sha256") != adapter_sha256
            or reading.get("image_sha256") != source.get("content_sha256")
            or (reading["source_observation_id"] not in fact.evidence_refs)
            or (not reading.get("admitted"))
            or (
                reading.get("raw_probabilities", {}).get(reading.get("state"), 0)
                < threshold
            )
        ):
            return None
    first, last = readings
    if first["source_observation_id"] == last["source_observation_id"]:
        return None
    if not 0 <= last.get("capture_ts_ms", -1) - first.get("capture_ts_ms", 0) <= 10000:
        return None
    if last["state"] != target or first["state"] == target:
        return None
    if (
        target == "dead"
        and first["state"] not in {"low", "alive"}
        or (target == "alive" and first["state"] != "dead")
        or (target == "low" and first["state"] != "alive")
    ):
        return None
    for prior in delivered:
        if (
            pure_life_event(prior.get("utterance", prior.get("text", ""))) == target
            and prior.get("completed_at_ms", float("inf")) >= first["capture_ts_ms"]
        ):
            return None
    return {
        "scope": "one_model_generated_pure_life_transition",
        "target_state": target,
        "readings": readings,
        "visual_evidence_provider": "trained_Jev_life_state",
        "wording_check": "complete_single_clause_no_extra_claims",
        "utility_policy": "latest_explicit_active_UI_style_requests_salient_event_acknowledgments",
        "model_probabilities_unchanged": True,
    }


REACTION_INSTRUCTIONS = "你是一起玩游戏的中文队友，目前没有用户提问。\n图片按时间排列，都是实际游戏帧。任务只关注底部HUD角色死亡或复活的新变化。\npixel_life_states是独立Jev视觉分类器对这些原帧的读数，不是用户提供的事实；admitted=false表示不能采用。\nstate含义：dead=已死亡；low=存活且生命条不超过一半；alive=存活且生命条超过一半；unknown=无法确认。\n比较最早和最新的有效读数，并检查图片。如果出现死亡或复活的新变化，可以用一句简短自然的话反应。\nlow与alive的血条分类尚不可靠，不用于主动掉血播报；只在这两种存活状态之间变化时输出SILENT。\n不添加英雄名、击杀、技能、路线或团队胜负；倒下不是拿到击杀。没有明确新变化或已说过则安静。\n可以表达关心，但不夸大成果。遵守用户的安静偏好。\n像身边的队友一样自然说话，最多十八个汉字；只回应这次变化，不附加战术，不朗读状态标签。\n不要教用户“死了等复活”这种常识，也不要让已经死亡的角色小心被偷袭。\n生命分类仅区分超过半血与不超过半血，不提供精确损失或速度，不要宣称血量暴跌、见底或完全满血。\nPAST后的句子必须以“刚才”开头，明确说已发生的变化。\n严格只输出PAST、一个制表符和一句明确过去措辞的短中文，或者只输出SILENT。"
