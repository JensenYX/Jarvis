from jarvis.games.protocol import PvZView


def interface_help(snapshot, *, execution=False):
    if snapshot is None:
        return "游戏还没连上。先开启游戏，连接好后我就能看局面。"
    view = snapshot.view
    if not isinstance(view, PvZView):
        if snapshot.phase == "menu":
            return "先在尖塔窗口开始游戏，我会根据当前画面里的局面帮你分析。"
        if snapshot.paused:
            return "游戏已暂停，恢复后才能帮你操作。"
        if not snapshot.awaiting_action:
            return "当前还不能操作，等游戏进入可选择的界面再交给我。"
        return None
    stage = view.ui_stage if view.ui_stage != "unknown" else snapshot.phase
    if stage in {"loading", "preparing"}:
        return "草坪还在准备，等它展开后我就能给种植建议。"
    if stage == "title":
        return "先点游戏画面中央进入菜单，再点 Adventure 开始新手关卡。"
    if stage == "menu":
        return "先点游戏里的 Adventure 开始，或者点上方“新手关卡”。"
    if stage == "select_cards":
        selected = [card for card in view.card_choices if card.selected]
        if selected:
            return "植物已经选好了，点选卡区的“Start”进入草坪。"
        return "现在在选卡。先点植物卡加入左侧，再点“Start”开始。"
    if stage == "ended":
        return "这局已经结束，可以点上方的关卡按钮再开一局。"
    if snapshot.paused or stage == "paused":
        return "游戏暂停了，先点“Back To Game”恢复，我再帮你看下一步。"
    if view.auto_collect_sun or not view.normal_rules:
        return (
            "当前开了辅助选项，暂时不能代种。关掉自动收阳光并恢复正常速度后就可以。"
            if execution
            else "当前开了辅助选项，我可以陪你分析；恢复正常速度并关掉自动收阳光后才能代种。"
        )
    if stage == "unsupported" or snapshot.phase == "unsupported":
        return (
            "这关暂时只支持陪聊和看状态。想让我代种，可以点上方“新手关卡”或“五路草坪”。"
        )
    if view.input_state == "plant_selected" or view.selected_seed:
        selected = next(
            (s.name for s in view.seed_bank if s.seed_id == view.selected_seed), "植物"
        )
        return f"你正拿着{selected}，先点草坪种下或右键取消，再交给我。"
    if view.input_state == "shovel_selected":
        return "你正拿着铲子，先完成铲除或右键取消，再交给我。"
    if view.input_state == "held":
        return "先松开游戏里的鼠标或按键，我再接手这一株。"
    if not snapshot.focused:
        return "先回到游戏窗口，我就能继续帮你操作和提建议。"
    if not snapshot.awaiting_action and (not snapshot.legal_actions):
        return "游戏还没有开放操作，稍等一下再交给我。"
    if snapshot.awaiting_action and snapshot.legal_actions:
        return None
    if not any((a.kind == "PLANT" for a in snapshot.legal_actions)):
        affordable = [
            c
            for c in view.seed_bank
            if c.cost is not None and view.sun is not None and (c.cost <= view.sun)
        ]
        if view.seed_bank and (not affordable) and (view.sun is not None):
            costs = [c.cost for c in view.seed_bank if c.cost is not None]
            if costs:
                return f"现在有{view.sun}点阳光，最便宜的植物要{min(costs)}点，先点落下的阳光收集。"
            return "当前卡片的费用还没读取到，先按游戏画面的提示操作。"
        if affordable and all((c.cooldown_ready is False for c in affordable)):
            return "买得起的植物都还在冷却，先收阳光，等卡片亮起再种。"
        if view.seed_bank and (not any((c.ready for c in view.seed_bank))):
            return "现在卡片还没准备好，先收阳光，等它亮起再种。"
        if not view.seed_bank:
            return "还没有可用的植物卡，先完成游戏里的选卡或提示步骤。"
        return "当前没有合适的空格可种，我先陪你观察已有防线。"
    return None


def failed_help(snapshot, reason):
    observed = interface_help(snapshot)
    if observed:
        return observed
    return {
        "referenced_advice_expired": "刚才那条建议已经过时了，先让我看当前局面再选。",
        "decision_busy": "我还在看局面，请等这条建议出来，再把这一株交给我。",
        "one_action_inflight": "正在处理你刚才交给我的这一株，完成后就交还给你。",
        "wait": "现在可以先观察防线、继续收阳光，不用急着补种。",
        "abstain": "这一步暂时没有把握，我先不乱建议；可以问我哪张卡有什么用。",
        "stale_button_context": "刚才的操作时机变了，这一株没种；可以重新交给我。",
        "rejected_or_unavailable": "刚才的局面变了，这一株没有执行，你可以继续手动操作。",
        "fresh_state_unavailable": "暂时没收到新的游戏状态，稍等连接恢复后我再帮你。",
    }.get(reason, "这次建议没能算出来，你可以继续操作，也可以再问我一次。")
