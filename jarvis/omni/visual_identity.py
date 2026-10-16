import base64
import hashlib
import io
from PIL import Image


def identity_input(request, frame, game):
    if hashlib.sha256(frame.payload).hexdigest() != frame.observation.content_sha256:
        raise ValueError("Identity image differs from the captured observation")
    picture = Image.open(io.BytesIO(frame.payload)).convert("RGB")
    pictures = [picture]
    if game == "lol":
        w, h = picture.size
        crop = picture.crop((int(w * 0.18), int(h * 0.78), int(w * 0.78), h))
        pictures.append(crop.resize((crop.width * 2, crop.height * 2)))
    images, derivatives = ([], [])
    for i, picture in enumerate(pictures):
        stream = io.BytesIO()
        picture.save(stream, format="PNG")
        binary = stream.getvalue()
        images.append("data:image/png;base64," + base64.b64encode(binary).decode())
        derivatives.append(
            {
                "source_observation_id": frame.observation.observation_id,
                "source_sha256": frame.observation.content_sha256,
                "derived_sha256": hashlib.sha256(binary).hexdigest(),
                "normalized_crop": [0.18, 0.78, 0.78, 1] if i else None,
                "size": list(picture.size),
                "new_capture_time": False,
            }
        )
    state = {
        "game": game,
        "user_request": request.state["user_request"],
        "player_identity_binding": {
            k: request.state["player_identity_binding"][k]
            for k in ("source", "scope", "champion", "screen_position")
        },
        "image_layout_note": "最后一张若为HUD放大图，来自同一帧，不是新增时刻。",
    }
    return (
        request.model_copy(
            update={
                "state": state,
                "images": tuple(images),
                "observation_ids": (frame.observation.observation_id,),
            }
        ),
        derivatives,
    )
