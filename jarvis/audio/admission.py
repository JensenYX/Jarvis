NEUTRAL_PROMPT = "You are a task admission controller shared by two voice-agent runtimes.\nOnly the following observed speech and public capabilities are available.\nReturn JSON with action (NONE, START, UPDATE, CANCEL, STATUS) and goal (string).\nUse NONE for greetings, backchannels, speech directed elsewhere, or incomplete\nrequests. START admits a request that requires backend tools, memory, or domain\nreasoning. Preserve the user's exact facts and constraints in a self-contained\nnatural-language goal. UPDATE, CANCEL and STATUS express the user's task control\nintent. Do not answer the user, choose tool names/arguments, invent facts, or\napply any harness-specific routing policy. Empty goal is required for NONE."


class NeutralController:
    def __init__(self, model, capabilities):
        self.model, self.capabilities = (model, capabilities)
        self.history = []

    async def decide(self, transcript):
        import json

        message = {
            "role": "user",
            "content": json.dumps(
                {
                    "capabilities": self.capabilities,
                    "prior_observed_speech": self.history[-8:],
                    "observed_speech": transcript,
                },
                ensure_ascii=False,
            ),
        }
        from jarvis.core.structured import complete

        value = await complete(
            self.model,
            [{"role": "system", "content": NEUTRAL_PROMPT}, message],
            limit=1024,
        )
        if value.get("action") not in {"NONE", "START", "UPDATE", "CANCEL", "STATUS"}:
            raise ValueError("Invalid neutral admission action")
        if not isinstance(value.get("goal"), str):
            raise ValueError("Invalid neutral admission goal")
        self.history.append(transcript)
        return value
