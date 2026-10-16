import base64
import hashlib
import io
from PIL import Image
from .decision.base import DecisionInput, Question
from .short_visual import INSTRUCTIONS as VISUAL_INSTRUCTIONS

INSTRUCTIONS = (
    VISUAL_INSTRUCTIONS
    + "\n当前没有用户提问。所有图片都是按时间排列的完整游戏帧，第二张不是HUD裁剪。\n比较实际画面中的有意义新事件，用一句自然的队友口吻回应。表扬必须有可见成果且归属正确；\n受伤后的建议须以可见变化为依据，不推测隐藏敌人。普通走路、换武器或已经回应的事件保持安静。\n每次只提最明确的一次变化。不要附加击杀数量、伤害数字、未经确认的路线、塔的级别或阵营。\n先核对底部控制角色的生命状态；灰屏和自己的生命归零不能被其他人的击杀提示误读为自己获胜。\n只夸有证据的成果，别为了热闹编造击杀或把小兵、塔和大型野怪混为一谈。\n只输出PAST加制表符和一句明确过去措辞的回复，或者SILENT。用户要求安静时遵守。\nrecently_delivered是已经实际播出的对话，只用于避免重复，不能证明画面事实。"
)
FACT_INSTRUCTIONS = "你是Jev视觉观察器。图片是按时间排列的实际完整游戏帧。\n只描述画面确实支持的一次新事件，不生成对用户的回答、建议、表扬或安慰。\n角色归属必须由底部HUD和实际画面确认；看不清就保持安静。\n不能把别人击杀、普通特效、小兵、塔或野怪互相混淆，不能猜隐藏敌人。\n普通走路、换武器、无明确变化、重复事件和用户要求安静时输出SILENT。\n有明确事件则只输出PAST、一个制表符和一句以“刚才”开头的可见事实。\n不要附加无证据的数量、精确血量、路线、塔的级别或阵营。\nrecently_delivered只是已经实际播出的对话，用于避免重复，不是视觉证据。"


def request_for(*, game, images, observation_ids=(), preferences=(), delivered=()):
    history = [
        {"text": item.get("utterance", item.get("text", "")), "actually_played": True}
        for item in delivered
        if item.get("utterance", item.get("text", ""))
        and item.get("actually_played", True)
    ]
    return DecisionInput(
        state={
            "game": game,
            "user_request": None,
            "player_identity_binding": {
                "source": "explicit_replay_role",
                "scope": "displayed_HUD_controlled_avatar",
                "champion": None,
                "screen_position": None,
            },
            "image_layout_note": "图片按先后排列，都是实际捕获的完整帧，不是HUD放大图。",
            "explicit_user_preferences": list(preferences),
            "recently_delivered": history,
        },
        images=tuple(images),
        observation_ids=tuple(observation_ids),
        questions=(
            Question(
                id="extract",
                primitive="noul",
                instructions="Is the proposed fact visible?",
            ),
        ),
    )


def image_url(binary):
    picture = Image.open(io.BytesIO(binary)).convert("RGB")
    picture.thumbnail((960, 540))
    buffer = io.BytesIO()
    picture.save(buffer, format="JPEG", quality=95)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def causal_request(frames, *, game, preferences, delivered):
    if len(frames) < 2:
        raise ValueError("A visual reaction needs multiple observed frames")
    stamps = [f.observation.stamp for f in frames]
    if len({(s.game_session_id, s.scene_epoch) for s in stamps}) != 1 or any(
        (b.capture_ts_ms <= a.capture_ts_ms for a, b in zip(stamps, stamps[1:]))
    ):
        raise ValueError(
            "A reaction must compare distinct ordered frames from one scene"
        )
    if any(
        (
            hashlib.sha256(f.payload).hexdigest() != f.observation.content_sha256
            for f in frames
        )
    ):
        raise ValueError("Reaction pixels differ from their observations")
    return request_for(
        game=game,
        images=[image_url(f.payload) for f in frames],
        observation_ids=[f.observation.observation_id for f in frames],
        preferences=preferences,
        delivered=delivered,
    )


def spaced_frames(frames, count):
    if count < 2:
        raise ValueError("Temporal sampling needs at least two slots")
    if len(frames) <= count:
        return frames
    first, last = (
        frames[0].observation.stamp.capture_ts_ms,
        frames[-1].observation.stamp.capture_ts_ms,
    )
    indices = sorted(
        {
            min(
                range(len(frames)),
                key=lambda j: abs(
                    frames[j].observation.stamp.capture_ts_ms
                    - (first + (last - first) * i / (count - 1))
                ),
            )
            for i in range(count)
        }
    )
    return tuple((frames[i] for i in indices))
