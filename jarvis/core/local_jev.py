import json
from types import SimpleNamespace

import httpx


class Answer:
    def __init__(self, values):
        self.values = values
        self.__dict__.update(values)

    def model_dump(self, **kwargs):
        return dict(self.values)


class Response:
    def __init__(self, answers, model):
        self.answers = answers
        self.choices = {
            key: value for key, value in answers.items() if value.type == "choice"
        }
        self.model = model

    def model_dump(self, **kwargs):
        return {
            "model": self.model,
            "answers": {key: value.model_dump() for key, value in self.answers.items()},
        }


class LocalJevClient:
    def __init__(self, endpoint):
        self.client = httpx.AsyncClient(base_url=endpoint, timeout=30, trust_env=False)
        self.models = SimpleNamespace(list=self.health)

    async def health(self):
        response = await self.client.get("/health")
        response.raise_for_status()
        if not response.json().get("ready"):
            raise RuntimeError("JEV model is loading")
        return response.json()

    async def system_one(self, *, state, questions, model=None):
        from jarvis.omni.decision.base import (
            DecisionInput,
            DecisionResult,
            Option,
            Question,
        )

        converted, fixed, definitions = [], {}, {}
        for name, question in questions.items():
            wire = (
                question.model_dump(mode="json")
                if hasattr(question, "model_dump")
                else question
            )
            primitive = wire.get("type", "choice")
            instructions = wire["instructions"]
            if not isinstance(instructions, str):
                instructions = json.dumps(instructions, ensure_ascii=False)
            criteria = wire.get("criteria") or {}
            if primitive == "choice":
                options = tuple(
                    Option(id=str(key), text=str(value or key))
                    for key, value in criteria.items()
                )
            elif primitive == "score":
                options = tuple(
                    Option(id=str(index), text=str(value))
                    for index, value in enumerate(criteria)
                )
            else:
                options = ()
            definitions[name] = primitive
            if len(options) == 1:
                key = options[0].id
                fixed[name] = Answer(
                    {
                        "type": primitive,
                        "choice": key,
                        "confidence": 1.0,
                        "distribution": {key: 1.0},
                        "probabilities": {key: 1.0},
                        "score": 0.0,
                    }
                )
            else:
                converted.append(
                    Question(
                        id=name,
                        primitive=primitive,
                        instructions=instructions,
                        options=options,
                    )
                )
        answers = dict(fixed)
        if converted:
            request = DecisionInput(state=state, questions=tuple(converted))
            response = await self.client.post(
                "/decide", json=request.model_dump(mode="json")
            )
            response.raise_for_status()
            result = DecisionResult.model_validate(response.json())
            if result.input_hash != request.input_hash() or [
                answer.question_id for answer in result.answers
            ] != [question.id for question in converted]:
                raise ValueError("JEV response is bound to different questions")
            for question, answer in zip(converted, result.answers):
                if (
                    answer.primitive != question.primitive
                    or answer.option_ids != question.option_ids()
                ):
                    raise ValueError("JEV response changed the available options")
                distribution = dict(zip(answer.option_ids, answer.raw_probs))
                values = {
                    "type": question.primitive,
                    "distribution": distribution,
                    "probabilities": distribution,
                }
                if question.primitive == "choice":
                    values.update(
                        choice=answer.choice, confidence=distribution[answer.choice]
                    )
                elif question.primitive == "noul":
                    values["noul"] = distribution["true"]
                else:
                    values["score"] = sum(
                        index * probability
                        for index, probability in enumerate(answer.raw_probs)
                    )
                answers[question.id] = Answer(values)
        return Response(answers, "jev-omni")

    async def aclose(self):
        await self.client.aclose()
