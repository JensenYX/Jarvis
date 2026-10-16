return function(B,http,storage)
local url=core.settings:get("jev_bridge_url") or ""
local secret=core.settings:get("jev_bridge_secret") or ""
if not http or not url:match("^http://127%.0%.0%.1:%d+/internal/voxel/exchange$") or #secret<32 then
    core.log("warning","[jev_bridge] loopback HTTP capability is unavailable; observation only offline")
    B.control_enabled=false
    return
end
local inflight,next_at,backoff=false,0,250
local results={}
local function array(t) return #t==0 and core.parse_json("[]") or t end
core.register_globalstep(function()
    local now=B.now()
    if inflight or now<next_at then return end
    inflight=true
    local snapshot=B.observe()

    snapshot.action_candidates=array(snapshot.action_candidates)
    local payload={snapshot=snapshot,events=array(B.events),results=array(results),command_seq=B.command_seq}
    local instance,bridge=B.instance_id,B.bridge_epoch
    http.fetch({url=url,method="POST",extra_headers={"Authorization: Bearer "..secret,"Content-Type: application/json"},
        data=core.write_json(payload),timeout=2},function(response)
        inflight=false
        next_at=B.now()+backoff
        if not response.succeeded or response.code~=200 or #response.data>65536 then
            backoff=math.min(backoff*2,2000)
            if B.now()-B.last_success>2500 then

                B.revoke("exchange_unavailable")
            end
            return
        end
        local data=core.parse_json(response.data)
        if not B.protocol.reply(data) then B.revoke("invalid_exchange_reply");return end
        if data.game_instance_id~=instance or data.bridge_epoch~=bridge or bridge~=B.bridge_epoch then return end

        B.last_success=B.now();backoff=250
        next_at=B.now()+((B.task or B.project or B.browser_mode=='look') and 80 or 250)
        if type(data.ack_events)=="number" then
            local remaining={}
            for _,e in ipairs(B.events) do if e.event_seq>data.ack_events then remaining[#remaining+1]=e end end
            B.events=remaining
        end
        local acknowledged={}
        for _,id in ipairs(data.ack_results or {}) do acknowledged[id]=true end
        local pending={}
        for _,r in ipairs(results) do if not acknowledged[r.request_id] then pending[#pending+1]=r end end
        results=pending
        if data.session_id~=B.session_id then
            B.rebind(type(data.session_id)=="string" and data.session_id or "")
            return
        end
        local input=data.input
        if type(input)=="table" and type(input.seq)=="number" then
            if input.seq>B.input_seq then B.revoke("browser_input_observed");B.input_seq=input.seq end
            B.browser_mode=(input.mode=="look" or input.mode=="menu" or input.mode=="watch") and input.mode or "released"
            if input.alive then B.browser_seen=B.now() end
        end

        for i,m in ipairs(data.commands or {}) do
            if i>8 or #results>=32 then B.overflow=true;B.revoke("result_queue_full");break end
            local r
            if m.kind:sub(1,8)=='PROJECT_' then r=B.build_command(m)
            elseif m.kind:sub(1,5)=='TASK_' then r=B.survival_command(m)
            else r=B.command(m) end
            if r then results[#results+1]=r end
        end
    end)
end)
end
