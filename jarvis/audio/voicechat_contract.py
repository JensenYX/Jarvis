import json

ADMISSION = "native_public_function_intent_to_harness"


def native_tool_goal(name, arguments):
    if not isinstance(name, str) or not name or (not isinstance(arguments, dict)):
        raise ValueError(
            "Native function intent requires a name and JSON object arguments"
        )
    return (
        "The native audio model requested function "
        + json.dumps(name, ensure_ascii=False)
        + " with arguments "
        + json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        + ". Enforce the public policy and verify the request before execution. Use the actual tool result to answer, or request necessary clarification."
    )


DELEGATE = {
    "type": "function",
    "function": {
        "name": "delegate",
        "description": "Ask the background agent to perform requests requiring tools, memory, or domain reasoning. Preserve corrections and task-control intent.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "Complete self-contained user request including exact supplied facts and constraints",
                }
            },
            "required": ["goal"],
            "additionalProperties": False,
        },
    },
}
RESULT_POLICY = "Use the available native functions when the user request needs their capability. A background agent validates and performs each requested operation. Its returned content contains the confirmed result or the necessary clarification. Do not invent completed actions. Continue ordinary conversation while the function runs."
