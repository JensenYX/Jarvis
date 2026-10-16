RULES = {
    "pvz_web": {
        "source": "Plants-vs-Zombies-MODDED/game@277c1bf97813c355f59e974c5a3a7ffd46df5b1d",
        "economy": "向日葵产生阳光；种植消耗卡片显示的阳光并启动该卡冷却。代打时可以选择正常点击收阳光，动画完成后到账。",
        "planting": "种植需要卡片可用、阳光足够且该植物能种在目标格。支持白天草坪和已观察到的局部草坪新手关卡。",
        "information": "远近区域是当前可见位置的摘要，不知道未来刷怪；未展示精确血量、冷却秒数或必然伤害。",
        "control": "明确的一步请求只操作一步；持续代打直到玩家收回，计时代打到期或本关结束会停止。每步由Jev根据当前合法动作选择，包含种植、正常收阳光或等待。人工输入、收回、暂停、失焦和断线撤销授权。只有已授权的持续代打才会在等待后继续判断。",
    }
}


def lookup(game, topic):
    if game == "voxel":
        from jarvis.games.voxel.rules import lookup as voxel_rule

        return voxel_rule(topic)
    rules = RULES.get(game, {})
    return (
        {"source": rules.get("source"), "topic": topic, "text": rules[topic]}
        if topic in rules and topic != "source"
        else {
            "error": "Unknown rule topic",
            "available": [k for k in rules if k != "source"],
        }
    )
