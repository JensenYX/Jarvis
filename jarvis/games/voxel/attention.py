def holds_actions(mode):
    return bool(mode.r.user_speaking or mode.r.route_pending)
