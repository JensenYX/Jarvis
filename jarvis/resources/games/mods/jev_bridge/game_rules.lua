return function(B)

B.allowlist = { ["mcl_core:dirt"]=true }
B.control_enabled = core.settings:get_bool("jev_bridge_place_verified", false)
function B.name(item)
    local def = core.registered_items[item]
    local text = def and (def.short_description or def.description) or item
    return core.strip_colors(core.get_translated_string("en", text)):sub(1, 100)
end
function B.item(stack)
    return {item_id=stack:get_name(), name=B.name(stack:get_name()), count=stack:get_count()}
end
function B.eye(player)
    local p = player:get_pos()
    local props = player:get_properties()
    local offset = player:get_eye_offset()
    local yaw = player:get_look_horizontal()

    local x,z = offset.x/10,offset.z/10
    return vector.add(p, {x=x*math.cos(yaw)-z*math.sin(yaw),
        y=(props.eye_height or 1.625)+offset.y/10,
        z=x*math.sin(yaw)+z*math.cos(yaw)})
end
function B.focus(player, reach)
    local eye = B.eye(player)
    local ray = core.raycast(eye, vector.add(eye, vector.multiply(player:get_look_dir(), reach or 4)), true, false)
    for hit in ray do
        if hit.type == "object" then
            if hit.ref ~= player then return nil end
        elseif hit.type == "node" then
            return {type="node", under=hit.under, above=hit.above,
                intersection_point=hit.intersection_point}
        end
    end
end
function B.state_key(player)
    local p = player:get_pos()
    return {position=p, yaw=player:get_look_horizontal(), pitch=player:get_look_vertical(),
        slot=player:get_wield_index(), stack=player:get_wielded_item():to_string(),
        controls=player:get_player_control_bits()}
end
function B.same_key(a,b)
    return a and b and a.slot==b.slot and a.stack==b.stack and
        vector.distance(a.position,b.position)<0.02 and math.abs(a.yaw-b.yaw)<0.001 and
        math.abs(a.pitch-b.pitch)<0.001 and a.controls==b.controls
end
function B.check_manual()
    local player=B.player()
    if not player then return end
    local key=B.state_key(player)
    local old=B.previous

    if B.pickup_stack_change and B.pickup_stack_change(old,key) then
        old=table.copy(old);old.stack=key.stack
    end
    local camera_only=not B.grant and old and key.slot==old.slot and key.stack==old.stack and
        key.controls==0 and old.controls==0 and vector.distance(key.position,old.position)<.02
    local passive_water=not B.grant and not B.project and B.passive_water_change and B.passive_water_change(player,key,old)
    if old and not camera_only and not passive_water and not B.same_key(key,old) and not (B.presentation_change and B.presentation_change(key)) and not (B.build_allows_native_change and B.build_allows_native_change(key)) and not (B.task_allows_native_change and B.task_allows_native_change(key)) then B.revoke("native_player_change") end
    B.previous=key
end
local function overlaps(pos, obj)
    local at=obj:get_pos()
    local props=obj:get_properties()
    if not at or not props then return true end
    if not obj:is_player() and not props.physical then return false end
    local box=props.collisionbox or {-0.3,0,-0.3,0.3,1.8,0.3}
    return at.x+box[1]<pos.x+.5 and at.x+box[4]>pos.x-.5 and
        at.y+box[2]<pos.y+.5 and at.y+box[5]>pos.y-.5 and
        at.z+box[3]<pos.z+.5 and at.z+box[6]>pos.z-.5
end
function B.eligible(player, require_control)
    if not player or player:get_hp()<=0 then return nil,"not_alive" end
    if B.uncertain or B.overflow then return nil,"unreconciled" end
    if require_control and (B.client_menu or not B.client_focused or B.now()-B.client_seen>400) then return nil,"native_client_not_ready" end
    if require_control and (B.browser_mode~="look" or B.now()-B.browser_seen>1200) then return nil,"input_not_ready" end
    if player:get_player_control_bits()~=0 or vector.length(player:get_velocity())>0.1 then return nil,"player_moving" end
    if not core.check_player_privs(player,{interact=true}) or core.is_creative_enabled(B.player_name) then return nil,"survival_required" end
    local stack=player:get_wielded_item()
    local name=stack:get_name()
    local def=core.registered_items[name]
    if not B.allowlist[name] or stack:is_empty() or stack:get_wear()~=0 or not def or
        def.type~="node" or def.drawtype~="normal" or def.on_place~=core.nodedef_default.on_place then return nil,"held_item_not_verified" end
    if next(stack:get_meta():to_table().fields or {}) then return nil,"custom_item" end
    local focus=B.focus(player,math.min(def.range or 4,4))
    if not focus then return nil,"no_block_face" end
    local under,above=core.get_node_or_nil(focus.under),core.get_node_or_nil(focus.above)
    local udef=under and core.registered_nodes[under.name]
    if not under or not above then return nil,"unloaded" end
    if above.name~="air" or not udef or not udef.walkable or udef.buildable_to or udef.on_rightclick then return nil,"occupied_or_interactive" end
    if core.is_protected(focus.above,B.player_name) then return nil,"protected" end
    for _,obj in ipairs(core.get_objects_inside_radius(focus.above,2.5)) do
        if overlaps(focus.above,obj) then return nil,"collision" end
    end
    return {focus=focus,under_name=under.name,above_name=above.name,item_id=name,
        state=B.state_key(player), count=stack:get_count()}
end
end
