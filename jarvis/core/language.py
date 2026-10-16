import re

ENGLISH = {
    "好。": "Okay.",
    "已保存。": "Saved.",
    "这个任务还在处理。": "I'm still working on it. You can keep talking.",
    "我来处理一下。": "I'll work on that. You can keep talking.",
    "你指的是哪一个任务？": "Which task do you mean?",
    "这个任务已经取消。": "That task has been cancelled.",
    "好的，按最新要求处理。": "Okay, I'll use your updated requirements.",
    "这个任务正在等待处理。": "That task is queued.",
    "这个任务还需要你补充信息。": "I need one more detail to finish that task.",
    "结果已经准备好了。": "The result is ready.",
    "结果准备好了。": "The result is ready.",
    "这个任务的结果已经告诉你了。": "I've already given you that result.",
    "这个任务已经结束了。": "That task has finished.",
    "现在暂时无法处理，请稍后再试。": "I couldn't process that request just now. Please try again.",
    "你最在意的条件是什么？": "Which requirement matters most to you?",
    "你想让我帮你完成什么？": "What would you like me to do?",
    "能再具体说一下你的要求吗？": "Could you be a little more specific?",
    "还需要哪些条件？": "What other requirements should I use?",
    "还有需要补充的信息吗？": "Is there anything else you'd like to add?",
    "我还没有找到这方面的记录。你能再告诉我一下吗？": "I couldn't find a record of that. Could you tell me again?",
}


def response_language(text, detected="", preference="auto"):
    if preference in {"en", "zh"}:
        return preference
    if detected.lower() in {"en", "english"}:
        return "en"
    if detected.lower() in {"zh", "chinese"}:
        return "zh"
    return "zh" if re.search("[\\u3400-\\u9fff]", text) else "en"
