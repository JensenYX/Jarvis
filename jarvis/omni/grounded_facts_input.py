import base64
import hashlib
import io
from PIL import Image
from .visual_reaction import image_url


def gameplay_facts_input(request, frames, game):
    binding = request.state.get("player_identity_binding")
    compact_binding = (
        {key: binding[key] for key in ("source", "scope")} if binding else None
    )
    state = {
        "game": game,
        "user_request": request.state["user_request"],
        "player_identity_binding": compact_binding,
        "recent_user_context": request.state.get("recent_user_context", {}),
        "explicit_user_preferences": request.state.get("explicit_user_preferences", []),
        "recently_delivered": request.state.get("recently_delivered", []),
        "image_layout_note": "图片按先后排列，都是实际捕获的完整帧，不是HUD放大图。",
    }
    urls, derivatives = ([], [])
    for frame in frames:
        source_hash = hashlib.sha256(frame.payload).hexdigest()
        if source_hash != frame.observation.content_sha256:
            raise ValueError(
                "Gameplay facts pixels differ from the captured observation"
            )
        url = image_url(frame.payload)
        binary = base64.b64decode(url.split(",", 1)[1], validate=True)
        with Image.open(io.BytesIO(binary)) as picture:
            size = list(picture.size)
        urls.append(url)
        derivatives.append(
            {
                "source_observation_id": frame.observation.observation_id,
                "source_sha256": source_hash,
                "derived_sha256": hashlib.sha256(binary).hexdigest(),
                "normalized_crop": None,
                "size": size,
                "new_capture_time": False,
                "image_input_family": "causal_native_gameplay",
            }
        )
    return (
        request.model_copy(
            update={
                "state": state,
                "images": tuple(urls),
                "observation_ids": tuple(
                    (frame.observation.observation_id for frame in frames)
                ),
            }
        ),
        derivatives,
    )


def facts_input(request, frame, game, *, image_input_family):
    if image_input_family not in {"native_single", "full_hud"}:
        raise ValueError("Unknown grounded facts image family")
    source_hash = hashlib.sha256(frame.payload).hexdigest()
    if source_hash != frame.observation.content_sha256:
        raise ValueError("Grounded facts pixels differ from the captured observation")
    binding = request.state.get("player_identity_binding")
    compact_binding = (
        {key: binding[key] for key in ("source", "scope")} if binding else None
    )
    state = {
        "game": game,
        "user_request": request.state["user_request"],
        "player_identity_binding": compact_binding,
    }
    if image_input_family == "native_single":
        urls = [image_url(frame.payload)]
        crops = [None]
    else:
        with Image.open(io.BytesIO(frame.payload)) as original:
            pictures = [original.convert("RGB")]
        crops = [None]
        if game == "lol":
            width, height = pictures[0].size
            crop = pictures[0].crop(
                (int(width * 0.18), int(height * 0.78), int(width * 0.78), height)
            )
            pictures.append(crop.resize((crop.width * 2, crop.height * 2)))
            crops.append([0.18, 0.78, 0.78, 1])
        urls = []
        for picture in pictures:
            stream = io.BytesIO()
            picture.save(stream, format="PNG")
            urls.append(
                "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()
            )
    derivatives = []
    for url, crop in zip(urls, crops):
        binary = base64.b64decode(url.split(",", 1)[1], validate=True)
        with Image.open(io.BytesIO(binary)) as picture:
            size = list(picture.size)
        derivatives.append(
            {
                "source_observation_id": frame.observation.observation_id,
                "source_sha256": source_hash,
                "derived_sha256": hashlib.sha256(binary).hexdigest(),
                "normalized_crop": crop,
                "size": size,
                "new_capture_time": False,
                "image_input_family": image_input_family,
            }
        )
    return (
        request.model_copy(
            update={
                "state": state,
                "images": tuple(urls),
                "observation_ids": (frame.observation.observation_id,),
            }
        ),
        derivatives,
    )
