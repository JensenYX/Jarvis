return function(B)
B.client_menu=true;B.client_focused=false;B.client_seen=0;B.client_seq=0
core.mod_channel_join("jev_bridge:client")
core.register_on_modchannel_message(function(channel,sender,message)
    if channel~="jev_bridge:client" or sender~=B.player_name or #message>100 then return end
    local seq,menu,focused,frame=message:match("^(%d+):([01]):([01]):([%d%.]+)$")
    seq=tonumber(seq);frame=tonumber(frame)
    if not seq or seq<=B.client_seq or not frame or frame>10000 then return end
    menu=menu=="1";focused=focused=="1"
    if menu~=B.client_menu or focused~=B.client_focused then B.revoke("native_menu_or_focus") end
    B.client_seq=seq;B.client_menu=menu;B.client_focused=focused;B.client_seen=B.now();B.frame_ms=frame
end)
local function bound(player) return player and player:get_player_name()==B.player_name end
local last_inventory=nil
local inventory_timer=0;local previous_step=B.now()
core.register_on_joinplayer(function(player)
    if not bound(player) then return end
    B.life_id=B.id("life");B.rebind(B.session_id)
    B.client_seq=0;B.client_seen=0;B.client_menu=true
    B.event("joined",{},"engine","native_hook")
end)
core.register_on_leaveplayer(function(player)
    if bound(player) then B.event("left",{},"engine","native_hook");B.rebind("") end
end)
core.register_on_dieplayer(function(player)
    if bound(player) then B.revoke("death");B.life_id=B.id("life");B.event("died",{},"engine","native_hook") end
end)
core.register_on_respawnplayer(function(player)
    if bound(player) then B.revoke("respawn");B.life_id=B.id("life");B.event("respawned",{},"engine","native_hook") end
end)
core.register_on_player_hpchange(function(player,change,reason)
    if bound(player) then
        B.event("hp_change",{change=change,cause=reason.type},"engine","native_hook")
        if change<0 and B.task then B.task.knockback_until=B.now()+900 end
    end
end,false)
core.register_on_placenode(function(pos,node,placer)
    if not bound(placer) then return end
    local active=B.active_command
    local own=active and active.target and vector.equals(pos,active.target) and (node.name==active.item_id or active.kind=='USE')
    if own then active.hook=true else B.revoke("native_place") end
    B.event("placed",{position=pos,item_id=node.name,command_id=own and active.command_id or nil},own and "jev" or "human","native_hook")
end)
core.register_on_dignode(function(pos,node,digger)
    if bound(digger) then
        local active=B.active_command;local own=active and active.kind=='DIG' and vector.equals(active.target,pos)
        if own then active.hook=true else B.revoke('native_dig') end
        B.event('dug',{position=pos,item_id=node.name,command_id=own and active.command_id or nil},own and 'jev' or 'human','native_hook')
    end
end)
core.register_on_player_inventory_action(function(player,action)
    if bound(player) then
        local own=B.active_command and B.active_command.kind=='EQUIP'
        if not own then B.revoke("inventory_action") end
        B.event("inventory_action",{action=action},own and 'jev' or 'human',"native_hook")
    end
end)
core.register_globalstep(function(dtime)

    local now=B.now()
    if now-previous_step>700 then B.stall_recheck_until=now+700 end
    previous_step=now
    B.check_manual()
    local player=B.player()
    inventory_timer=inventory_timer+dtime
    if player and inventory_timer>=.25 then
        inventory_timer=0
        local counts={}
        for _,stack in ipairs(player:get_inventory():get_list("main") or {}) do
            if not stack:is_empty() then counts[stack:get_name()]=(counts[stack:get_name()] or 0)+stack:get_count() end
        end
        local fingerprint=core.write_json(counts)
        if last_inventory and last_inventory~=fingerprint then

            B.event("inventory_changed",{},"unknown","state_diff")
        end
        last_inventory=fingerprint
    elseif not player then last_inventory=nil end
    if B.grant and B.now()>B.grant.expires then B.revoke("grant_expired") end
    if (B.browser_mode=="look" or B.browser_mode=="watch") and B.now()-B.browser_seen>1200 and B.now()>(B.stall_recheck_until or 0) then
        B.browser_mode="released";B.revoke("input_lease_expired")
    end
end)
end
