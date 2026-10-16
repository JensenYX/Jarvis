return function(B)
local surroundings, last_surroundings = {}, -10000
local function loaded_segment(a,b)
    local delta=vector.subtract(b,a)
    local steps=math.ceil(vector.length(delta)*2)
    for i=0,steps do
        if not core.get_node_or_nil(vector.add(a,vector.multiply(delta,i/math.max(1,steps)))) then return false end
    end
    return true
end
local function sensor(player)
    local eye, dir=B.eye(player),player:get_look_dir()
    local nodes, objects, seen={}, {}, {}

    local right={x=math.cos(player:get_look_horizontal()),y=0,z=math.sin(player:get_look_horizontal())}
    for _,h in ipairs({-.45,0,.45}) do
        for _,v in ipairs({-.3,0,.3}) do
            local aim=vector.normalize(vector.add(vector.add(dir,vector.multiply(right,h)),{x=0,y=v,z=0}))
            for hit in core.raycast(eye,vector.add(eye,vector.multiply(aim,8)),false,false) do
                if hit.type=="node" then
                    local p=hit.under
                    local key=core.pos_to_string(p)
                    local node=core.get_node_or_nil(p)
                    if not seen[key] and node and loaded_segment(eye,hit.intersection_point or p) then
                        seen[key]=true
                        nodes[#nodes+1]={item_id=node.name,name=B.name(node.name),position=p}
                    end
                    break
                end
            end
        end
    end
    for scanned,obj in ipairs(core.get_objects_inside_radius(eye,8)) do
        if scanned>32 then break end
        if obj~=player and #objects<12 then
            local pos=obj:get_pos()
            local delta=vector.subtract(pos,eye)
            if vector.length(delta)>.1 and vector.dot(vector.normalize(delta),dir)>.6 then
                local visible=false
                for hit in core.raycast(eye,pos,true,false) do
                    if hit.type=="node" then break end
                    if hit.type=="object" and hit.ref~=player then visible=hit.ref==obj;break end
                end
                if visible and loaded_segment(eye,pos) then
                    local entity=obj:get_luaentity()

                    local name=entity and entity.name or "player"
                    objects[#objects+1]={kind=name:sub(1,100),position=pos}
                end
            end
        end
    end
    return {visible_nodes=nodes,visible_objects=objects,radius_blocks=8,
        quality="bounded_first_hit_rays_and_object_line_of_sight"}
end
function B.observe()
    B.check_manual()
    B.snapshot_seq=B.snapshot_seq+1
    local now,player=B.now(),B.player()
    local s={protocol_version=1,route="luanti_voxelibre",game_build="VoxeLibre-0.92.3-a523240f/Luanti-5.17.0",
        world_id=B.world_id,game_instance_id=B.instance_id,player_life_id=B.life_id,
        bridge_epoch=B.bridge_epoch,session_id=B.session_id,snapshot_seq=B.snapshot_seq,
        control_epoch=B.control_epoch,candidate_set_id=B.id("candidates"),
        sampled_at_game_mono_ms=now,observation_mode="declared_spatial_sensor",
        phase=player and (player:get_hp()>0 and "playing" or "dead") or "disconnected",
        view={game="voxel",unknown_fields={"unloaded_space","hidden_objects","closed_containers"}},
        action_candidates={},capabilities={observe=true,guarded_control=B.control_enabled,
            place_held_once=B.control_enabled,movement=false,dig_once=false,eat_once=false,combat=false,continuous_autoplay=false},
        control={mode=B.grant and "ARMED" or "MANUAL",uncertain=B.uncertain or B.overflow,
            input_mode=B.browser_mode,input_seq=B.input_seq,client_menu=B.client_menu,
            client_focused=B.client_focused,frame_ms=B.frame_ms,last_revocation=B.last_revocation}}
    local private=nil
    if player then
        local inv={}
        for _,listname in ipairs({"main","craft"}) do
            for _,stack in ipairs(player:get_inventory():get_list(listname) or {}) do
                if not stack:is_empty() and #inv<40 then inv[#inv+1]=B.item(stack) end
            end
        end
        local hunger={quality="unavailable"}
        if mcl_hunger and mcl_hunger.get_active() and mcl_hunger.get_hunger_enabled(player) then
            hunger={quality="game_api",value=mcl_hunger.get_hunger(player),max=20}
        elseif mcl_hunger then hunger={quality="inactive"} end
        s.view.self={position=player:get_pos(),eye_position=B.eye(player),hp=player:get_hp(),hp_max=player:get_properties().hp_max,
            breath=player:get_breath(),breath_max=player:get_properties().breath_max,
            hunger=hunger,wielded=B.item(player:get_wielded_item()),inventory_summary=inv,
            wield_index=player:get_wield_index(),yaw=player:get_look_horizontal(),pitch=player:get_look_vertical()}
        local head=core.get_node_or_nil(B.eye(player))
        local headdef=head and core.registered_nodes[head.name]
        s.view.self.head_medium=not head and "unknown" or (headdef and headdef.drowning and headdef.drowning>0 and "drowning_medium" or "breathable")
        if now-last_surroundings>=1000 then surroundings=sensor(player);last_surroundings=now end
        s.view.environment=table.copy(surroundings)
        s.view.environment.observed_at_game_mono_ms=last_surroundings
        local day=core.get_timeofday()
        s.view.environment.time_of_day=day
        s.view.environment.always_day=core.settings and core.settings:get_bool('jev_always_day',false) or false
        s.view.environment.clear_weather=core.settings and core.settings:get_bool('jev_clear_weather',false) or false
        s.view.environment.day_phase=day<.2 and "night" or day<.25 and "dawn" or day<.75 and "day" or day<.8 and "dusk" or "night"
        local focus=B.focus(player,4)
        if focus then
            local node=core.get_node_or_nil(focus.under)
            s.view.focus={kind="block_face",position=focus.under,place_position=focus.above,
                item_id=node and node.name or "unknown",name=node and B.name(node.name) or "unknown"}
        end
        local reason
        private,reason=B.eligible(player,true)
        s.control.blocked_reason=reason
        if private and B.control_enabled then
            local id=B.id("place")
            s.action_candidates={{action_id=id,kind="PLACE_HELD_ON_FOCUS_ONCE",label="在当前瞄准面放置一块手持的"..B.name(private.item_id),
                item_id=private.item_id,count=1,target=private.focus.above}}
            private.action_id=id
        end
    end
    s.view.recent_events={}
    for i=math.max(1,#B.events-15),#B.events do s.view.recent_events[#s.view.recent_events+1]=B.events[i] end
    if B.build_observe then B.build_observe(s,player) end
    if B.survival_observe then B.survival_observe(s,player) end
    B.snapshots[s.snapshot_seq]={public=s,private=private}
    B.snapshots[s.snapshot_seq-32]=nil
    return s
end
end
