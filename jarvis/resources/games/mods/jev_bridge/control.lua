return function(B)
local function result(message,status,reason,extra)
    local row=extra or {}
    row.request_id=message.request_id;row.command_id=message.command_id
    row.status=status;row.reason=reason;row.at_game_mono_ms=B.now()
    if message.command_id then
        if not B.command_ledger.remember(B,message.command_id,row) then
            row.status="UNKNOWN";row.reason="ledger_write_failed"
        end
    end
    return row
end
local function source(message)
    local a=message.anchor
    if type(a)~="table" or a.game_instance_id~=B.instance_id or a.player_life_id~=B.life_id or
        a.bridge_epoch~=B.bridge_epoch or a.control_epoch~=B.control_epoch then return nil,"stale_anchor" end
    local row=B.snapshots[a.snapshot_seq]
    if not row or row.public.candidate_set_id~=a.candidate_set_id or not row.private or
        B.now()-row.public.sampled_at_game_mono_ms>1500 then return nil,"snapshot_expired" end
    local current,reason=B.eligible(B.player(),true)
    if not current then return nil,reason end
    local original=row.private
    if not B.same_key(original.state,current.state) or not vector.equals(original.focus.under,current.focus.under) or
        not vector.equals(original.focus.above,current.focus.above) or original.under_name~=current.under_name then return nil,"target_or_item_changed" end
    return row
end
function B.command(message)
    if B.protocol and not B.protocol.command(message) then B.revoke("invalid_command");return nil end
    if type(message)~="table" or type(message.request_id)~="string" or #message.request_id>80 then return nil end
    if message.kind=="QUERY" then
        local old=B.ledger[message.command_id]
        if old then local copy=table.copy(old);copy.request_id=message.request_id;return copy end
        return result(message,"UNKNOWN","no_ledger_record")
    end
    if message.kind=="REVOKE" then B.revoke("explicit_revoke");return result(message,"CANCELLED","manual") end
    if message.command_id and B.ledger[message.command_id] then
        local copy=table.copy(B.ledger[message.command_id]);copy.request_id=message.request_id;return copy
    end
    if not B.control_enabled or message.session_id~=B.session_id or B.session_id=="" then
        return result(message,"REJECTED","not_bound_or_verified")
    end
    if type(message.seq)~="number" or message.seq<=B.command_seq then return result(message,"REJECTED","old_command") end
    B.command_seq=message.seq
    B.check_manual()
    local row,reason=source(message)
    if not row then B.grant=nil;return result(message,"REJECTED",reason) end
    if message.kind=="ARM" or message.kind=="SUBMIT" then
        if type(message.source_event_id)~="string" or type(message.grant_id)~="string" or B.sources[message.source_event_id] then
            return result(message,"REJECTED","source_already_used")
        end
        B.sources[message.source_event_id]=B.now()
        local total=0
        for _ in pairs(B.sources) do total=total+1 end
        if total>512 then B.overflow=true;return result(message,"REJECTED","source_ledger_full") end
        B.grant={id=message.grant_id,anchor=table.copy(message.anchor),source=message.source_event_id,
            action_id=row.private.action_id,expires=B.now()+5000}
        B.event("grant_armed",{grant_id=message.grant_id,source_event_id=message.source_event_id},"engine","own_command")
        if B.overflow then B.grant=nil;return result(message,"REJECTED","critical_event_queue_full") end
        if message.kind=="ARM" then return result(message,"ARMED",nil,{grant_id=message.grant_id}) end
    end
    if message.kind~="COMMIT" and message.kind~="SUBMIT" then return result(message,"REJECTED","unsupported") end
    local grant=B.grant
    B.grant=nil
    if not grant or grant.id~=message.grant_id or grant.expires<B.now() or grant.action_id~=message.action_id or
        grant.source~=message.source_event_id or grant.anchor.snapshot_seq~=message.anchor.snapshot_seq then
        return result(message,"REJECTED","grant_invalid")
    end
    if type(message.command_id)~="string" or #message.command_id>80 then return result(message,"REJECTED","command_id_invalid") end
    local player=B.player()
    local stack=player:get_wielded_item()
    local before=stack:get_count()
    local item=row.private.item_id
    local target=row.private.focus.above
    local active={command_id=message.command_id,item_id=item,target=target,hook=false}
    if not B.command_ledger.remember(B,message.command_id,{status="RUNNING",command_id=message.command_id,
        game_instance_id=B.instance_id,player_life_id=B.life_id,at_game_mono_ms=B.now()}) then
        return result(message,"UNKNOWN","ledger_write_failed")
    end
    B.active_command=active
    local ok,returned=pcall(stack:get_definition().on_place,stack,player,table.copy(row.private.focus))
    B.active_command=nil

    if ok and returned~=nil then
        ok=pcall(function() player:set_wielded_item(returned) end)
    end
    local after=player:get_wielded_item()
    local node=core.get_node_or_nil(target)
    B.previous=B.state_key(player)
    local count=after:get_count()
    if ok and node and node.name==item and active.hook and count==before-1 and
        (count==0 or after:get_name()==item) then
        B.event("command_succeeded",{command_id=message.command_id,position=target,item_id=item,count_before=before,count_after=count},"jev","own_command")
        return result(message,"SUCCEEDED",nil,{count_before=before,count_after=count,target=target,item_id=item})
    end
    B.uncertain=true
    B.event("command_unknown",{command_id=message.command_id},"jev","own_command")
    return result(message,"UNKNOWN","callback_or_effect_uncertain")
end
end
