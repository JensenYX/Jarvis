

return function(B)
local material_ids={"mcl_core:dirt","mcl_core:wood","mcl_core:birchwood","mcl_core:sprucewood", "mcl_core:darkwood",
    "mcl_core:acaciawood","mcl_core:junglewood","mcl_core:cobble","mcl_core:stone","mcl_core:stonebrick","mcl_core:brick_block"}
B.build_materials={}
core.register_on_mods_loaded(function()
    for _,name in ipairs(material_ids) do
        local def=core.registered_items[name]
        if def and def.type=="node" and def.drawtype=="normal" and def.on_place==core.nodedef_default.on_place and
            not def.on_construct and not def.on_rightclick and not def.after_place_node and (not def.paramtype2 or def.paramtype2=="none") then B.build_materials[name]=true end
    end
    local table_def=core.registered_items['mcl_crafting_table:crafting_table']
    if table_def and table_def.on_place==core.nodedef_default.on_place and not table_def.on_construct and not table_def.after_place_node then
        B.build_materials['mcl_crafting_table:crafting_table']=true
    end
end)
local faces={{x=1,y=0,z=0},{x=-1,y=0,z=0},{x=0,y=-1,z=0},{x=0,y=1,z=0},{x=0,y=0,z=1},{x=0,y=0,z=-1}}
local function same(a,b)return a and b and vector.equals(a,b)end
local function collision(pos,obj,at_override)
    local at=at_override or obj:get_pos();local props=obj:get_properties()
    if not at or not props then return true end
    if not obj:is_player() and not props.physical then return false end
    local box=props.collisionbox or {-.3,0,-.3,.3,1.8,.3}
    return at.x+box[1]<pos.x+.5 and at.x+box[4]>pos.x-.5 and at.y+box[2]<pos.y+.5 and
        at.y+box[5]>pos.y-.5 and at.z+box[3]<pos.z+.5 and at.z+box[6]>pos.z-.5
end
local function plain(name)
    local def=core.registered_nodes[name]
    return def and def.walkable and not def.buildable_to and not def.on_rightclick
end
local function valid_stack(stack,name)
    local def=stack:get_definition()
    return B.build_materials[name] and stack:get_name()==name and not stack:is_empty() and stack:get_wear()==0 and
        not next(stack:get_meta():to_table().fields or {}) and def and def.type=="node" and def.drawtype=="normal" and
        def.on_place==core.nodedef_default.on_place
end
local function ready(player)
    if not player or player:get_hp()<=0 then return false,"not_alive" end
    if B.uncertain or B.overflow then return false,"unreconciled" end
    if B.client_menu or not B.client_focused or B.now()-B.client_seen>500 then return false,"native_client_not_ready" end
    if (B.browser_mode~="look" and B.browser_mode~="watch") or B.now()-B.browser_seen>1200 then return false,"browser_not_ready" end
    if player:get_player_control_bits()~=0 or vector.length(player:get_velocity())>.15 then return false,"player_moving" end
    if core.is_creative_enabled(B.player_name) or not core.check_player_privs(player,{interact=true}) then return false,"survival_required" end
    return true
end
local function jump_clearance(player)
    local p=player:get_pos();local box=player:get_properties().collisionbox or {-.3,0,-.3,.3,1.8,.3}

    for x=math.floor(p.x+box[1]+.5),math.floor(p.x+box[4]+.5) do
        for z=math.floor(p.z+box[3]+.5),math.floor(p.z+box[6]+.5) do
            for y=math.floor(p.y+box[5]+.5),math.floor(p.y+box[5]+1.3+.5) do
                local n=core.get_node_or_nil({x=x,y=y,z=z});local d=n and core.registered_nodes[n.name]
                if not d or d.walkable then return false end
            end
        end
    end
    return true
end
local function can_place(player,cell,eye_override)
    if not B.build_materials[cell.item_id] then return nil,"material_not_allowed" end
    local pos={x=cell.x,y=cell.y,z=cell.z}
    local node=core.get_node_or_nil(pos)
    if not node then return nil,"unloaded" end
    if node.name~="air" then return nil,node.name==cell.item_id and "already_present" or "occupied" end
    if core.is_protected(pos,B.player_name) then return nil,"protected" end
    for _,obj in ipairs(core.get_objects_inside_radius(pos,2.5)) do
        local predicted=eye_override and obj==player and vector.add(player:get_pos(),vector.subtract(eye_override,B.eye(player))) or nil
        if collision(pos,obj,predicted) then return nil,"collision" end
    end
    local eye=eye_override or B.eye(player)
    if vector.distance(eye,pos)>4 then return nil,"out_of_reach" end
    for _,offset in ipairs(faces) do
        local under=vector.add(pos,offset);local support=core.get_node_or_nil(under)
        if support and plain(support.name) then
            local target=vector.add(pos,vector.multiply(offset,.51))
            for hit in core.raycast(eye,target,true,false) do
                if hit.type=="object" and hit.ref~=player then break end
                if hit.type=="node" then
                    if same(hit.under,under) and same(hit.above,pos) then
                        return {type="node",under=under,above=pos,intersection_point=hit.intersection_point},nil
                    end
                    break
                end
            end
        end
    end
    return nil,"no_visible_support"
end
local function visible_cell(player,pos)
    local eye=B.eye(player)
    if vector.distance(eye,pos)>4 then return false end
    local hit_node=false
    for hit in core.raycast(eye,pos,true,false) do
        if hit.type=="object" and hit.ref~=player then return false end
        if hit.type=="node" then hit_node=true;return same(hit.under,pos) end
    end

    return not hit_node and core.get_node_or_nil(pos)~=nil
end
local function stack_for(player,name)
    local inv=player:get_inventory()
    for slot=1,math.min(36,inv:get_size("main")) do
        local stack=inv:get_stack("main",slot)
        if stack:get_name()==name and not stack:is_empty() and stack:get_wear()==0 and
            not next(stack:get_meta():to_table().fields or {}) then return slot,stack end
    end
end
local function legal_step(player,dx,dz)
    if B.travel_step then
        local move=B.travel_step(player,dx,dz)
        if move and (move.kind=='MOVE' or move.kind=='JUMP_MOVE') and move.reaches_solid_land then
            return move.target,move.kind=='JUMP_MOVE'
        end
        return nil
    end
    local pos=player:get_pos();local x=math.floor(pos.x+.5)+dx;local z=math.floor(pos.z+.5)+dz
    local foot=math.floor(pos.y+.1);local target={x=x,y=foot+.5,z=z}

    local support={x=x,y=foot,z=z};local node=core.get_node_or_nil(support)
    if not node or not plain(node.name) or (core.registered_nodes[node.name].damage_per_second or 0)>0 or core.is_protected(support,B.player_name) then return nil end
    for h=1,2 do
        local node=core.get_node_or_nil({x=x,y=foot+h,z=z})
        if not node or node.name~="air" then return nil end
    end
    local eye=B.eye(player);local visible=false
    for hit in core.raycast(eye,{x=x,y=foot+.49,z=z},false,false) do
        if hit.type=="node" then visible=same(hit.under,support);break end
    end
    if not visible then return nil end
    for i=1,4 do
        local p=vector.add(pos,vector.multiply(vector.subtract(target,pos),i/4))
        for h=1,2 do
            local n=core.get_node_or_nil({x=p.x,y=p.y+h-.5,z=p.z})
            if not n or n.name~="air" then return nil end
        end
    end
    return target
end
local function public_materials()
    local materials={}
    for name in pairs(B.build_materials) do materials[#materials+1]={item_id=name,name=B.name(name),
        sources=B.crafting and B.crafting.material_sources(name) or {}} end
    table.sort(materials,function(a,b)return a.item_id<b.item_id end)
    return materials
end
B.legal_build_step=legal_step
B.can_place_cell=can_place
B.visible_cell=visible_cell
B.valid_build_stack=valid_stack
function B.jump_place_cell(player,name)

    if not B.build_materials[name] or not B.ground_status or not B.ground_status(player) or not jump_clearance(player) then return nil end
    if player:get_player_control_bits()~=0 or vector.length(player:get_velocity())>.2 then return nil end
    local p=player:get_pos();local cell={x=math.floor(p.x+.5),y=math.floor(p.y+.1)+1,z=math.floor(p.z+.5),item_id=name}
    if math.abs(p.x-cell.x)>.35 or math.abs(p.z-cell.z)>.35 then return nil end
    if can_place(player,cell,vector.offset(B.eye(player),0,1.15,0)) then return cell end
end
function B.build_observe(s,player)
    s.view.construction={materials=public_materials(),project=B.project and {project_id=B.project.id,grant_id=B.project.grant_id,
        revision=B.project.revision,status=B.project.input and "INPUT" or "RUNNING",expires_in_ms=math.max(0,B.project.expires-B.now())} or nil}
    s.view.construction.terrain=B.build_terrain and B.build_terrain(player) or {}
    s.view.construction.survey=B.site_terrain and B.site_terrain(player) or {}
    s.capabilities.cooperative_build=core.settings:get_bool("jev_cooperative_build",false)
    local project=B.project
    if not player or not project then return end
    if project.expires<=B.now() then B.revoke("project_deadline");return end
    local completed=0;local remaining={};local legal={};local private={}
    for i,cell in ipairs(project.cells) do
        local pos={x=cell.x,y=cell.y,z=cell.z}

        local record=project.confirmed[i]
        if record and visible_cell(player,pos) then
            local current=core.get_node_or_nil(pos)
            if current and current.name~=cell.item_id then project.confirmed[i]=nil;record=nil end
        end
        if record then completed=completed+1
        else remaining[#remaining+1]={index=i,item_id=cell.item_id,position=pos} end
    end
    s.view.construction.project.completed=completed;s.view.construction.project.total=#project.cells
    s.view.construction.project.remaining_count=#remaining
    s.view.construction.project.remaining_sample={}
    for i=1,math.min(16,#remaining) do s.view.construction.project.remaining_sample[i]={position=remaining[i].position,item_id=remaining[i].item_id} end
    local ok,why=ready(player)
    s.view.construction.project.blocked_reason=why
    if not ok or project.input then return end
    local wield=player:get_wielded_item()
    local slots={}
    for _,row in ipairs(remaining) do
        local cell=project.cells[row.index];local pointed,reason=can_place(player,cell)
        local jump=false
        if not pointed and reason=="no_visible_support" then
            local pos=player:get_pos();local head=core.get_node_or_nil({x=pos.x,y=pos.y+2.6,z=pos.z})
            if head and head.name=="air" and jump_clearance(player) then
                pointed=can_place(player,cell,vector.add(B.eye(player),{x=0,y=1.15,z=0}));jump=pointed~=nil
            end
        end
        if pointed then
            if valid_stack(wield,cell.item_id) then
                if #legal<24 then
                    local id=B.id("build-place")
                    legal[#legal+1]={action_id=id,kind=jump and "PROJECT_JUMP_PLACE" or "PROJECT_PLACE",cell=row.index,item_id=cell.item_id,target=row.position,
                        label="放置一块"..B.name(cell.item_id)}
                    private[id]={kind=jump and "PROJECT_JUMP_PLACE" or "PROJECT_PLACE",cell=row.index,state=B.state_key(player),pointed=pointed}
                end
            else
                local slot=stack_for(player,cell.item_id)
                if slot then slots[slot]=cell.item_id end
            end
        elseif reason=="already_present" then

            local pos=row.position;local visible=visible_cell(player,pos)
            if visible then project.confirmed[row.index]={source="observed_existing",at=B.now()} end
        end
    end
    for slot,name in pairs(slots) do
        local kind=slot<=9 and 'PROJECT_SELECT' or 'PROJECT_EQUIP'
        local id=B.id("build-slot");legal[#legal+1]={action_id=id,kind=kind,slot=slot,item_id=name,label="拿出"..B.name(name)}
        private[id]={kind=kind,slot=slot,item_id=name,state=B.state_key(player)}
    end
    for _,d in ipairs({{1,0},{-1,0},{0,1},{0,-1}}) do
        local p,jump=legal_step(player,d[1],d[2])
        if p and vector.distance(p,project.origin)<18 then
            local id=B.id("build-move");local closest=10000;local useful=0
            local eye=vector.add(B.eye(player),vector.subtract(p,player:get_pos()))
            for _,row in ipairs(remaining) do
                closest=math.min(closest,vector.distance(p,row.position))
                if can_place(player,project.cells[row.index],eye) then useful=useful+1 end
            end
            legal[#legal+1]={action_id=id,kind="PROJECT_MOVE",target=p,jump=jump,closest_remaining=closest,useful_placements=useful,label="短距离移动到可见空地"}
            private[id]={kind="PROJECT_MOVE",target=p,jump=jump,state=B.state_key(player)}
        end
    end
    s.view.construction.project.actions=legal
    B.build_contexts=B.build_contexts or {}
    B.build_contexts[s.snapshot_seq]={seq=s.snapshot_seq,set=s.candidate_set_id,at=B.now(),values=private}
    B.build_contexts[s.snapshot_seq-32]=nil
end
function B.build_allows_native_change(key)
    local p=B.project;local op=p and p.input
    if not p or p.control_epoch~=B.control_epoch or p.input_seq~=B.input_seq then return false end
    if not op then
        return p.settle_until and p.settle_until>B.now() and key.controls==0 and vector.distance(key.position,p.settle_origin)<1.6 and B.previous and key.slot==B.previous.slot and math.abs(key.yaw-B.previous.yaw)<.001
    end
    if op.expires<B.now() then return false end
    if op.kind=="PROJECT_SELECT" then
        return vector.distance(key.position,op.state.position)<.1 and math.abs(key.yaw-op.state.yaw)<.02 and math.abs(key.pitch-op.state.pitch)<.02 and key.controls==0 and
            (key.slot==op.state.slot or key.slot==op.slot)
    end
    if op.kind=="PROJECT_JUMP_PLACE" then
        local old=op.state.position

        local presented=op.receipt or B.presentation and B.presentation.command==op.command_id
        return math.abs(key.position.x-old.x)<.2 and math.abs(key.position.z-old.z)<.2 and key.position.y>=old.y-.1 and key.position.y<old.y+1.6 and
            (presented or math.abs(key.yaw-op.state.yaw)<.02 and math.abs(key.pitch-op.state.pitch)<.02) and key.slot==op.state.slot and (op.receipt or key.stack==op.state.stack)
    end
    if op.kind=="PROJECT_MOVE" then return vector.distance(key.position,op.state.position)<=4 and
        key.position.y>=math.min(op.state.position.y,op.target.y)-.2 and key.position.y<=math.max(op.state.position.y,op.target.y)+1.35 and
        ((B.presentation and B.presentation.command==op.command_id) or math.abs(key.yaw-op.state.yaw)<.02 and math.abs(key.pitch-op.state.pitch)<.02) and key.slot==op.state.slot and key.stack==op.state.stack end
    return false
end
local function result(m,status,reason,extra)
    local row=extra or {};row.request_id=m.request_id;row.command_id=m.command_id;row.status=status;row.reason=reason;row.at_game_mono_ms=B.now()
    if m.command_id and not B.command_ledger.remember(B,m.command_id,row) then row.status="UNKNOWN";row.reason="ledger_write_failed" end
    return row
end
local function place(m,project,player,index)
    local cell=project.cells[index];local pointed,why=can_place(player,cell)
    if not pointed then return result(m,"REJECTED",why) end
    local stack=player:get_wielded_item();local before=stack:get_count()
    if not valid_stack(stack,cell.item_id) or before<1 then return result(m,"REJECTED","wrong_held_material") end
    if not B.command_ledger.remember(B,m.command_id,{status="RUNNING",command_id=m.command_id,project_id=project.id,cell=index}) then return result(m,"UNKNOWN","journal_error") end
    local active={command_id=m.command_id,item_id=cell.item_id,target=pointed.above,hook=false};B.active_command=active
    local ok,returned=pcall(stack:get_definition().on_place,stack,player,pointed);B.active_command=nil
    if ok and returned~=nil then ok=pcall(function()player:set_wielded_item(returned)end) end
    B.previous=B.state_key(player)
    local node=core.get_node_or_nil(pointed.above);local after=player:get_wielded_item():get_count()
    if ok and node and node.name==cell.item_id and active.hook and after==before-1 then
        if B.present then B.present('PLACE',pointed.above,450,m.command_id) end
        project.confirmed[index]={source="command",command_id=m.command_id,at=B.now()}
        B.event("project_placed",{project_id=project.id,cell=index,position=pointed.above,item_id=cell.item_id,command_id=m.command_id},"jev","own_command")
        return result(m,"SUCCEEDED",nil,{project_id=project.id,cell=index,item_id=cell.item_id,target=pointed.above,count_before=before,count_after=after})
    end
    B.uncertain=true;B.revoke("project_effect_unknown")
    return result(m,"UNKNOWN","placement_unconfirmed")
end
function B.build_command(m)
    if not B.protocol.command(m) then B.revoke("invalid_project_command");return nil end
    local old=m.command_id and B.ledger[m.command_id]
    if old and old.status~="INPUT_ARMED" then
        if m.kind=="PROJECT_INPUT_DONE" and B.project and B.project.input and B.project.input.command_id==m.command_id then
            B.project.settle_until=B.now()+700;B.project.settle_origin=B.player():get_pos();B.project.input=nil
            if B.stop_presentation then B.stop_presentation() end
            B.previous=B.state_key(B.player())
        end
        local row=table.copy(old);row.request_id=m.request_id;return row
    end
    if old and m.kind=="PROJECT_STEP" then local row=table.copy(old);row.request_id=m.request_id;return row end
    if not core.settings:get_bool("jev_cooperative_build",false) then return result(m,"REJECTED","capability_disabled") end
    local player=B.player()
    if m.session_id~=B.session_id or B.session_id=="" then return result(m,"REJECTED","not_bound") end
    if m.seq<=B.command_seq then return result(m,"REJECTED","old_command") end
    B.command_seq=m.seq
    B.check_manual()
    if m.kind=="PROJECT_ARM" then
        local a=m.anchor
        if not a or a.game_instance_id~=B.instance_id or a.player_life_id~=B.life_id or a.bridge_epoch~=B.bridge_epoch or a.control_epoch~=B.control_epoch then return result(m,"REJECTED","stale_source") end
        local observed=B.snapshots[a.snapshot_seq]
        if not observed or observed.public.candidate_set_id~=a.candidate_set_id or B.now()-observed.public.sampled_at_game_mono_ms>1500 then return result(m,"REJECTED","source_expired") end
        local ok,why=ready(player);if not ok then return result(m,"REJECTED",why) end
        if B.sources[m.source_event_id] or type(m.cells)~="table" or #m.cells<1 or #m.cells>512 then return result(m,"REJECTED","invalid_project") end
        local sources=0;for _ in pairs(B.sources) do sources=sources+1 end
        if sources>=512 then return result(m,"REJECTED","source_ledger_full") end
        local seen={};local origin=m.origin
        if type(origin)~="table" or vector.distance(player:get_pos(),origin)>8 then return result(m,"REJECTED","site_too_far") end
        for _,c in ipairs(m.cells) do
            if not B.build_materials[c.item_id] or type(c.x)~="number" or type(c.y)~="number" or type(c.z)~="number" or
                c.x%1~=0 or c.y%1~=0 or c.z%1~=0 or c.x<origin.x or c.x>=origin.x+12 or c.z<origin.z or c.z>=origin.z+12 or c.y<origin.y or c.y>=origin.y+8 then return result(m,"REJECTED","invalid_cell") end
            local k=core.pos_to_string(c)
            if seen[k] then return result(m,"REJECTED","duplicate_cell") end;seen[k]=true
        end
        B.sources[m.source_event_id]=B.now();B.grant=nil;B.task=nil
        B.project={id=m.project_id,revision=m.revision,grant_id=m.grant_id,origin=origin,cells=m.cells,
            confirmed={},expires=B.now()+m.duration_ms,control_epoch=B.control_epoch,input_seq=B.input_seq}
        for _,id in ipairs(m.receipts or {}) do
            local receipt=B.ledger[id]
            if receipt and receipt.status=="SUCCEEDED" and receipt.project_id==m.project_id and receipt.target then
                for i,c in ipairs(m.cells) do
                    if same(c,receipt.target) and c.item_id==receipt.item_id and not receipt.removed then

                        B.project.confirmed[i]={source="prior_command",command_id=id,at=B.now()}
                    end
                end
            end
        end
        return result(m,"ARMED",nil,{project_id=m.project_id,grant_id=m.grant_id})
    end
    local project=B.project
    if not project or not player or project.id~=m.project_id or project.grant_id~=m.grant_id or project.expires<B.now() or project.control_epoch~=B.control_epoch or project.input_seq~=B.input_seq then return result(m,"REJECTED","project_revoked") end
    if m.kind=='PROJECT_CANCEL_STEP' then
        local op=project.input
        if op then
            project.input=nil;project.settle_until=B.now()+700;project.settle_origin=player:get_pos()
            if not op.receipt then result({command_id=op.command_id},'CANCELLED','voice_attention',{kind=op.kind}) end
            if B.stop_presentation then B.stop_presentation() end
            B.previous=B.state_key(player)
        end
        return result(m,'STEP_CANCELLED')
    end
    if m.kind=="PROJECT_INPUT_DONE" then
        local op=project.input
        if not op or op.command_id~=m.command_id then return result(m,"REJECTED","input_not_armed") end
        project.input=nil
        B.previous=B.state_key(player)
        if op.kind=='PROJECT_MOVE' and B.stop_presentation then B.stop_presentation() end
        if op.kind=='PROJECT_MOVE' or op.kind=='PROJECT_JUMP_PLACE' then project.settle_until=B.now()+700;project.settle_origin=player:get_pos() end
        if op.kind=="PROJECT_JUMP_PLACE" then
            local row=op.receipt
            if row then row=table.copy(row);row.request_id=m.request_id;return row end
            return result(m,"REJECTED","jump_did_not_reach_face")
        end
        local success=op.kind=="PROJECT_SELECT" and player:get_wield_index()==op.slot and player:get_wielded_item():get_name()==op.item_id or
            op.kind=="PROJECT_MOVE" and vector.distance(player:get_pos(),op.target)<.65
        local reason=nil;if not success then reason="input_target_not_reached" end
        return result(m,success and "SUCCEEDED" or "REJECTED",reason,{kind=op.kind,position=player:get_pos()})
    end
    if project.input then return result(m,"REJECTED","input_in_progress") end
    local a=m.anchor
    if a.game_instance_id~=B.instance_id or a.player_life_id~=B.life_id or a.bridge_epoch~=B.bridge_epoch or a.control_epoch~=B.control_epoch then return result(m,"REJECTED","stale_source") end
    local set=B.build_contexts and B.build_contexts[m.anchor.snapshot_seq]
    local candidate=set and set.values[m.action_id]
    if not candidate or set.seq~=a.snapshot_seq or set.set~=a.candidate_set_id or B.now()-set.at>1500 then return result(m,"REJECTED","candidate_expired") end
    local ok,why=ready(player);if not ok then return result(m,"REJECTED",why) end
    if not B.same_key(candidate.state,B.state_key(player)) then return result(m,"REJECTED","native_state_changed") end
    local target=candidate.target
    if candidate.cell then
        local cell=project.cells[candidate.cell]
        if cell then target={x=cell.x,y=cell.y,z=cell.z} end
    end
    if m.kind=='PROJECT_AIM' then
        if not target or not B.present then return result(m,'REJECTED','no_presentation_target') end
        B.present('AIM',target,1800,m.request_id)
        return result(m,'AIMING',nil,{target=target})
    end
    if candidate.kind=='PROJECT_PLACE' and B.facing_target and not B.facing_target(player,target) then
        return result(m,'REJECTED','camera_not_aligned')
    end
    if candidate.kind=='PROJECT_EQUIP' then
        local slot=stack_for(player,candidate.item_id)
        if slot~=candidate.slot then return result(m,'REJECTED','material_slot_changed') end
        if not B.command_ledger.remember(B,m.command_id,{status='RUNNING',command_id=m.command_id,kind='PROJECT_EQUIP'}) then return result(m,'UNKNOWN','journal_error') end
        local info,why,unknown=B.equip_item(player,{public=candidate},m.command_id)
        if unknown then B.uncertain=true;B.revoke('project_equip_unknown');return result(m,'UNKNOWN',why) end
        return result(m,info and 'SUCCEEDED' or 'REJECTED',why,info)
    end
    if candidate.kind=="PROJECT_MOVE" or candidate.kind=="PROJECT_SELECT" or candidate.kind=="PROJECT_JUMP_PLACE" then
        if candidate.kind=="PROJECT_MOVE" then
            local pos=player:get_pos();local target,jump=legal_step(player,candidate.target.x-math.floor(pos.x+.5),candidate.target.z-math.floor(pos.z+.5))
            if not same(target,candidate.target) or jump~=candidate.jump then return result(m,"REJECTED","move_no_longer_legal") end
        elseif candidate.kind=="PROJECT_SELECT" then
            local slot=stack_for(player,candidate.item_id)
            if slot~=candidate.slot then return result(m,"REJECTED","material_slot_changed") end
        end
        project.input=table.copy(candidate);project.input.command_id=m.command_id;project.input.expires=math.min(project.expires,B.now()+6500)
        if B.present and candidate.kind=='PROJECT_MOVE' then B.present('MOVE',candidate.target,6000,m.command_id) end
        if B.present and candidate.kind=='PROJECT_JUMP_PLACE' then B.present('AIM',target,1800,m.command_id) end
        return result(m,"INPUT_ARMED",nil,{kind=candidate.kind,target=candidate.target,jump=candidate.jump,slot=candidate.slot,item_id=candidate.item_id})
    end
    return place(m,project,player,candidate.cell)
end
core.register_globalstep(function()
    local p=B.project;local op=p and p.input;local player=B.player()
    if op and op.kind=="PROJECT_JUMP_PLACE" and not op.receipt and player and p.expires>B.now() and op.expires>B.now() and
        p.control_epoch==B.control_epoch and p.input_seq==B.input_seq and not B.client_menu and B.client_focused and
        B.now()-B.client_seen<500 and B.now()-B.browser_seen<1200 and (B.browser_mode=="watch" or B.browser_mode=="look") and
        player:get_hp()>0 and not B.uncertain and not B.overflow and player:get_pos().y>op.state.position.y+.25 then
        local pointed=can_place(player,p.cells[op.cell])
        if pointed and (not B.facing_target or B.facing_target(player,p.cells[op.cell])) then
            op.receipt=place({command_id=op.command_id,request_id=op.command_id},p,player,op.cell)
        end
    end
    if B.project and (B.project.expires<B.now() or B.project.input and B.project.input.expires<B.now()) then B.revoke("project_input_or_grant_expired") end
end)
core.register_on_dignode(function(pos)
    for _,row in pairs(B.ledger) do
        if row.status=="SUCCEEDED" and row.target and same(row.target,pos) then row.removed=true end
    end
    if B.persist_ledger then pcall(B.persist_ledger) end
end)
end
