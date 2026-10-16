
local http = core.request_http_api()
local storage = core.get_mod_storage()
local path = core.get_modpath(core.get_current_modname())
local B = {
    player_name = core.settings:get("jev_bridge_player") or "CompanionPlayer",
    world_id = core.settings:get("jev_bridge_world") or "unconfigured",
    session_id = "", control_epoch = 0, snapshot_seq = 0, event_seq = 0,
    input_seq = 0, browser_mode = "released", browser_seen = 0,
    events = {}, ledger = {}, ledger_order = {}, sources = {}, snapshots = {},
    candidates = {}, command_seq = 0, uncertain = false, overflow = false,
    grant = nil, active_command = nil, previous = nil, last_success = 0,
}
B.protocol=dofile(path.."/protocol.lua")
B.command_ledger=dofile(path.."/ledger.lua")
local ledger_text=storage:get_string("command_ledger")
local saved=ledger_text~="" and core.parse_json(ledger_text) or nil
B.ledger,B.ledger_order,B.uncertain=B.command_ledger.restore(saved,storage:get_int("unresolved_callback")==1)

local journal_path=core.get_worldpath().."/jev_bridge_transaction.json"
local journal_command=nil
local journal=io.open(journal_path,"rb")
if journal then
    local raw=journal:read(4097);journal:close()
    local row=#raw<=4096 and core.parse_json(raw) or nil
    if type(row)=="table" and type(row.command_id)=="string" then
        journal_command=row.command_id
        if not B.ledger[row.command_id] then B.ledger_order[#B.ledger_order+1]=row.command_id end
        B.ledger[row.command_id]=row
        B.ledger,B.ledger_order,B.uncertain=B.command_ledger.restore({records=B.ledger,order=B.ledger_order},B.uncertain)
    else B.uncertain=true end
end
function B.persist_ledger(latest)
    if latest and (latest.status=="RUNNING" or latest.command_id==journal_command) then
        local encoded=core.write_json(latest)
        assert(encoded and #encoded<=4096 and core.safe_file_write(journal_path,encoded),"Transaction journal unavailable")
        journal_command=latest.command_id
    end
    storage:set_string("command_ledger",core.write_json({records=B.ledger,order=B.ledger_order}))
    storage:set_int("unresolved_callback",B.uncertain and 1 or 0)
end
local serial = 0
function B.id(prefix)
    serial = serial + 1
    return prefix .. "-" .. tostring(core.get_us_time()) .. "-" .. serial
end
function B.now() return core.get_us_time() / 1000 end
function B.player() return core.get_player_by_name(B.player_name) end
B.instance_id = B.id("instance")
B.bridge_epoch = B.id("bridge")
B.life_id = B.id("life")
function B.revoke(reason)
    if B.stop_presentation then B.stop_presentation() end
    B.control_epoch = B.control_epoch + 1
    B.last_revocation={reason=reason,control_epoch=B.control_epoch,input_seq=B.input_seq,at=B.now()}
    if B.grant then B.event("grant_cancelled", {reason=reason}, "engine", "own_command") end
    B.grant = nil
    local input=B.project and B.project.input
    local pending=input and B.ledger[input.command_id]
    if pending and pending.status=="INPUT_ARMED" then

        B.command_ledger.remember(B,input.command_id,{status="CANCELLED",command_id=input.command_id,
            kind=input.kind,reason=reason,at_game_mono_ms=B.now()})
    end
    if B.project then B.event("project_stopped",{project_id=B.project.id,reason=reason},"engine","own_command") end
    B.project=nil;B.build_contexts={}
    if B.task then
        local p=B.task.pending
        if p and B.ledger[p.command_id] and (B.ledger[p.command_id].status=='RUNNING' or B.ledger[p.command_id].status=='INPUT_ARMED') then
            local receipt=p.consumed and table.copy(p.consumed) or {kind=p.kind,reason=reason}
            receipt.status=p.consumed and 'SUCCEEDED' or 'CANCELLED';receipt.command_id=p.command_id
            B.command_ledger.remember(B,p.command_id,receipt)
        end
        B.event('task_stopped',{task_id=B.task.id,reason=reason},'engine','own_command')
    end
    B.task=nil;B.task_contexts={}
end
function B.rebind(session)
    B.revoke("binding_changed")
    B.session_id = session or ""
    B.bridge_epoch = B.id("bridge")
    B.snapshots = {}
    B.previous = nil
    B.input_seq = 0
    B.command_seq = 0
    B.sources = {}
    B.browser_mode = "released"
end
function B.event(kind, payload, actor, evidence)
    B.event_seq = B.event_seq + 1
    if #B.events >= 64 then
        B.overflow = true
        B.grant = nil
        return
    end
    B.events[#B.events + 1] = {event_id=B.id("event"), event_seq=B.event_seq,
        actor=actor or "unknown", evidence=evidence or "state_diff", kind=kind,
        player_life_id=B.life_id, at_game_mono_ms=B.now(), payload=payload or {}}
end
for _,file in ipairs({"game_rules", "pickup", "weather", "observe", "events", "control", "construction", "crafting", "travel", "terrain", "presentation", "interactions", "survival"}) do
    dofile(path .. "/" .. file .. ".lua")(B)
end
dofile(path .. "/transport.lua")(B, http, storage)
