
local P={}
local function integer(x) return type(x)=="number" and x==x and x>=0 and x<9007199254740991 and x%1==0 end
local function id(x) return type(x)=="string" and #x>0 and #x<=100 end
local function pos(p)
    if type(p)~="table" then return false end
    for _,k in ipairs({"x","y","z"}) do
        local v=p[k]
        if type(v)~="number" or v~=v or math.abs(v)>30000 or v%1~=0 then return false end
    end
    return true
end
local function anchor(a)
    return type(a)=="table" and id(a.game_instance_id) and id(a.player_life_id) and id(a.bridge_epoch) and id(a.candidate_set_id) and integer(a.snapshot_seq) and integer(a.control_epoch)
end
function P.command(m)
    if type(m)~="table" or not id(m.request_id) or not integer(m.seq) or type(m.session_id)~="string" or #m.session_id>100 then return false end
    if m.kind=="REVOKE" then return true end
    if m.kind=="QUERY" then return id(m.command_id) end
    if m.kind=='TASK_CATALOG' then return true end
    if m.kind=='TASK_ARM' then
        if not (id(m.task_id) and id(m.grant_id) and id(m.source_event_id) and anchor(m.anchor) and integer(m.duration_ms) and m.duration_ms>=1000 and m.duration_ms<=900000 and type(m.scope)=='table' and integer(m.max_effects) and m.max_effects>=1 and m.max_effects<=2048) then return false end
        local s=m.scope
        if s.kind=='DIG' or s.kind=='PLACE' then
            if type(s.targets)~='table' or #s.targets>128 or (#s.targets<1 and not s.corridor) then return false end
            for _,v in ipairs(s.targets) do if type(v)~='table' or not pos(v.position) or not id(v.item_id) then return false end end
            if s.corridor then
                local c=s.corridor
                if s.kind~='DIG' or type(c)~='table' or not pos(c.origin) or type(c.dx)~='number' or type(c.dz)~='number' or
                    not ((math.abs(c.dx)==1 and c.dz==0) or (math.abs(c.dz)==1 and c.dx==0)) or
                    not integer(c.width) or c.width<1 or c.width>4 or not integer(c.height) or c.height<2 or c.height>4 or
                    not integer(c.length) or c.length<1 or c.length>8 or type(c.until_open)~='boolean' then return false end
            end
        elseif s.kind=='AGENT' or s.kind=='TRAVEL' then
            if s.navigation~=nil and type(s.navigation)~='boolean' then return false end
            for _,k in ipairs({'alter','creatures','consume','model_outcome'}) do if s[k]~=nil and type(s[k])~='boolean' then return false end end
            if s.focus_items~=nil then
                if type(s.focus_items)~='table' or #s.focus_items>12 then return false end
                for _,v in ipairs(s.focus_items) do if not id(v) then return false end end
            end
            if s.site_box~=nil then
                local b=s.site_box
                if s.kind~='AGENT' or type(b)~='table' or not pos(b.origin) or not integer(b.width) or b.width<1 or b.width>12 or
                    not integer(b.depth) or b.depth<1 or b.depth>12 or not integer(b.height) or b.height<1 or b.height>12 then return false end
            end
            if s.recipes~=nil then
                if type(s.recipes)~='table' or #s.recipes>24 then return false end
                for _,v in ipairs(s.recipes) do if not id(v) then return false end end
            end
        elseif s.kind=='CRAFT' then
            if type(s.recipes)~='table' or #s.recipes<1 or #s.recipes>16 then return false end
            for _,v in ipairs(s.recipes) do if not id(v) then return false end end
        elseif s.kind=='COMBAT' then
            if type(s.entities)~='table' or #s.entities>12 or type(s.hostiles)~='boolean' then return false end
            for _,v in ipairs(s.entities) do if not id(v) then return false end end
            if #s.entities==0 and not s.hostiles then return false end
        else return false end
        return true
    end
    if m.kind=='TASK_AIM' then return id(m.task_id) and id(m.grant_id) and id(m.action_id) and anchor(m.anchor) end
    if m.kind=='TASK_STEP' or m.kind=='TASK_INPUT_DONE' then
        return id(m.task_id) and id(m.grant_id) and id(m.command_id) and (m.kind=='TASK_INPUT_DONE' or id(m.action_id) and anchor(m.anchor))
    end
    if m.kind=='TASK_CANCEL_STEP' then return id(m.task_id) and id(m.grant_id) end
    if m.kind=='PROJECT_CANCEL_STEP' then return id(m.project_id) and id(m.grant_id) end
    if m.kind=="PROJECT_ARM" then
        if not (id(m.project_id) and id(m.grant_id) and id(m.source_event_id) and integer(m.revision) and anchor(m.anchor) and pos(m.origin) and
            integer(m.duration_ms) and m.duration_ms>=1000 and m.duration_ms<=900000 and type(m.cells)=="table" and #m.cells>=1 and #m.cells<=512) then return false end
        for _,cell in ipairs(m.cells) do if not pos(cell) or not id(cell.item_id) then return false end end
        if m.receipts then
            if type(m.receipts)~="table" or #m.receipts>256 then return false end
            for _,v in ipairs(m.receipts) do if not id(v) then return false end end
        end
        return true
    end
    if m.kind=='PROJECT_AIM' then return id(m.project_id) and id(m.grant_id) and id(m.action_id) and anchor(m.anchor) end
    if m.kind=="PROJECT_STEP" or m.kind=="PROJECT_INPUT_DONE" then
        return id(m.project_id) and id(m.grant_id) and id(m.command_id) and
            (m.kind=="PROJECT_INPUT_DONE" or id(m.action_id) and anchor(m.anchor))
    end
    if m.kind~="ARM" and m.kind~="SUBMIT" and m.kind~="COMMIT" then return false end
    local a=m.anchor
    if type(a)~="table" or not id(a.game_instance_id) or not id(a.player_life_id) or not id(a.bridge_epoch) or not id(a.candidate_set_id) or
        not integer(a.snapshot_seq) or not integer(a.control_epoch) or not id(m.source_event_id) or not id(m.grant_id) then return false end
    if m.kind~="ARM" and (not id(m.command_id) or not id(m.action_id)) then return false end
    return true
end
function P.reply(data)
    if type(data)~="table" or not id(data.game_instance_id) or not id(data.bridge_epoch) or type(data.session_id)~="string" or #data.session_id>100 then return false end
    if not integer(data.ack_events) or type(data.ack_results)~="table" or #data.ack_results>32 or type(data.commands)~="table" or #data.commands>8 then return false end
    for _,v in ipairs(data.ack_results) do if not id(v) then return false end end
    for _,m in ipairs(data.commands) do if not P.command(m) then return false end end
    local input=data.input
    if type(input)~="table" or not integer(input.seq) or type(input.alive)~="boolean" then return false end
    if input.mode~="look" and input.mode~="menu" and input.mode~="watch" and input.mode~="released" then return false end
    return true
end
return P
