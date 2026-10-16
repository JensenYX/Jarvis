import base64
import hashlib
import io
import json
from pathlib import Path
from PIL import Image
from jarvis.config import ROOT
from .decision.base import DecisionInput, Option, Question
from .decision.jev_omni import JevOmniBackend


class VisualFactReader:
    def __init__(self, settings):
        path = Path(settings["protocol"])
        if not path.is_absolute():
            path = ROOT / path
        binary = path.read_bytes()
        if hashlib.sha256(binary).hexdigest() != settings["protocol_sha256"]:
            raise ValueError("Visual fact protocol changed")
        self.protocol = json.loads(binary)
        self.backend = JevOmniBackend(settings["endpoint"])
        self.adapter_sha256 = settings["adapter_sha256"]

    async def read(self, frame, *, game, timeout_s):
        if (
            hashlib.sha256(frame.payload).hexdigest()
            != frame.observation.content_sha256
        ):
            raise ValueError("Fact reader input differs from observation")
        image = Image.open(io.BytesIO(frame.payload)).convert("RGB")
        images = [image]
        tasks = ("champion", "location", "health") if game == "lol" else ("weapon",)
        if game == "lol":
            w, h = image.size
            a, b, c, d = self.protocol["hud_crop"]
            crop = image.crop((int(w * a), int(h * b), int(w * c), int(h * d)))
            scale = self.protocol["hud_scale"]
            images.append(crop.resize((crop.width * scale, crop.height * scale)))
        urls = []
        derivatives = []
        for index, picture in enumerate(images):
            stream = io.BytesIO()
            picture.save(stream, format="PNG")
            urls.append(
                "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()
            )
            derivatives.append(
                {
                    "index": index,
                    "source_observation_id": frame.observation.observation_id,
                    "source_sha256": frame.observation.content_sha256,
                    "derived_sha256": hashlib.sha256(stream.getvalue()).hexdigest(),
                    "transform": "RGB PNG re-encoding"
                    if index == 0
                    else "HUD crop and PIL default RGB bicubic resize",
                    "normalized_crop": None
                    if index == 0
                    else self.protocol["hud_crop"],
                    "size": list(picture.size),
                    "new_capture_time": False,
                }
            )
        questions = []
        for task in tasks:
            spec = self.protocol["questions"][task]
            questions.append(
                Question(
                    id=task,
                    primitive="choice",
                    instructions=spec["instructions"],
                    options=tuple((Option(**row) for row in spec["options"])),
                )
            )
        request = DecisionInput(
            state={"game": game, "image_layout": self.protocol["image_layout"]},
            questions=tuple(questions),
            images=tuple(urls),
            observation_ids=(frame.observation.observation_id,),
        )
        result = await self.backend.decide(request, timeout_s=timeout_s)
        if result.raw.get("adapter_sha256") != self.adapter_sha256:
            raise ValueError("Unexpected visual fact checkpoint")
        estimates = []
        for answer in result.answers:
            if answer.native_confidence < self.protocol["threshold"]:
                continue
            value = next(
                (
                    o.text
                    for q in questions
                    if q.id == answer.question_id
                    for o in q.options
                    if o.id == answer.choice
                )
            )
            estimates.append(
                {
                    "field": answer.question_id,
                    "value": value,
                    "choice": answer.choice,
                    "confidence": answer.native_confidence,
                    "observation_id": frame.observation.observation_id,
                    "status": "fallible model estimate; verify against pixels, not independent ground truth",
                }
            )
        raw = result.model_dump(mode="json")
        raw["visual_derivatives"] = derivatives
        return (estimates, raw, urls)

    async def close(self):
        await self.backend.close()
