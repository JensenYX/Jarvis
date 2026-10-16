
return function(B)
B.known_nodes={};B.object_ids=setmetatable({},{__mode='k'});B.objects={};B.task_contexts={}
B.last_combat=0;B.task=nil;local cached_catalog=nil
local function xyz(p)return {x=p.x,y=p.y,z=p.z}end
function B.visible_position(player,pos,object)
    local eye=B.eye(player);if vector.distance(eye,pos)>8 then return false end
    local delta=vector.subtract(pos,eye);local length=vector.length(delta)
    for i=0,math.ceil(length*2) do
        if not core.get_node_or_nil(vector.add(eye,vector.multiply(delta,i/math.max(1,math.ceil(length*2))))) then return false end
    end

    for hit in core.raycast(eye,pos,true,false) do
        if hit.type=='object' and hit.ref~=player then return object and hit.ref==object or false end
        if hit.type=='node' then
            if object then return false end
            if vector.equals(hit.under,pos) then return true end
            break
        end
    end
    if object then return true end
    for _,offset in ipairs({{0,.35,0},{0,.1,0},{0,-.2,0},{0,-.4,0},{.4,0,0},{-.4,0,0},{0,0,.4},{0,0,-.4}}) do
        for hit in core.raycast(eye,vector.offset(pos,offset[1],offset[2],offset[3]),true,false) do
            if hit.type=='object' and hit.ref~=player then break end
            if hit.type=='node' then if vector.equals(hit.under,pos) then return true end;break end
        end
    end
    return false
end
local function allowed_item(stack)
    for k in pairs(stack:get_meta():to_table().fields or {}) do if k~='description' then return false end end
    return true
end
local function available(player)
    if not player or player:get_hp()<=0 then return false,'not_alive' end
    if B.uncertain or B.overflow then return false,'unreconciled' end
    if B.client_menu or not B.client_focused or B.now()-B.client_seen>700 then return false,'native_client_not_ready' end
    if (B.browser_mode~='look' and B.browser_mode~='watch') or B.now()-B.browser_seen>1200 then return false,'browser_not_ready' end
    if core.is_creative_enabled(B.player_name) or not core.check_player_privs(player,{interact=true}) then return false,'survival_required' end
    return true
end
local function steady(player)
    return player:get_player_control_bits()==0 and vector.length(player:get_velocity())<.2
end
local function own_support_descent(player,pos,item)
    local pp=player:get_pos()
    if math.abs(pp.x-pos.x)>.25 or math.abs(pp.z-pos.z)>.25 or math.abs(pp.y-pos.y-.5)>.12 then return false end
    local prior=nil
    for _,r in pairs(B.ledger) do
        if r.status=='SUCCEEDED' and not r.removed and r.kind=='JUMP_PLACE' and r.target and vector.equals(r.target,pos)
            and r.item_id==item and r.support_position and r.support_position.y==pos.y-1 then prior=r;break end
    end
    if not prior then return false end

    local n=core.get_node_or_nil(prior.support_position);local d=n and core.registered_nodes[n.name]
    return n and n.name==prior.support_item_id and d and d.walkable and (d.damage_per_second or 0)==0 and
        not core.is_protected(prior.support_position,B.player_name)
end
local function diggable(player,row)
    local pos=row.position;local node=core.get_node_or_nil(pos)
    if not node then return nil,'unloaded' end
    if vector.distance(B.eye(player),pos)>4 or not B.visible_position(player,pos) then return nil,'not_visible_or_reachable' end
    if node.name~=row.item_id then return nil,'target_changed' end
    local def=core.registered_nodes[node.name]
    if not def or def.diggable==false or not def.on_dig then return nil,'not_diggable' end
    if core.is_protected(pos,B.player_name) or def.can_dig and not def.can_dig(pos,player) then return nil,'protected_or_locked' end

    local pp=player:get_pos()
    if math.abs(pp.x-pos.x)<.85 and math.abs(pp.z-pos.z)<.85 and pos.y<=pp.y and pos.y>pp.y-1 then
        if not row.support_descent or not own_support_descent(player,pos,row.item_id) then return nil,'standing_support' end
    end
    local held=player:get_wielded_item()
    if not allowed_item(held) then return nil,'custom_tool' end
    local params=core.get_dig_params(def.groups,held:get_tool_capabilities(),held:get_wear())
    if not params.diggable then
        local hand=player:get_inventory():get_stack('hand',1);params=core.get_dig_params(def.groups,hand:get_tool_capabilities(),hand:get_wear())
    end
    if not params.diggable or params.time>12 then return nil,'tool_needed' end
    return {node=node,definition=def,time_ms=math.max(50,params.time*1000),wear=params.wear,
        harvest=mcl_autogroup and mcl_autogroup.can_harvest(node.name,held:get_name(),player) or false}
end
local function visible_objects(player)
    local rows={};B.objects={}
    for _,obj in ipairs(core.get_objects_inside_radius(B.eye(player),8)) do
        if obj~=player and not obj:is_player() then
            local pos=obj:get_pos();local entity=obj:get_luaentity()
            local props=obj:get_properties()
            if pos and entity and entity.name and props and props.pointable~=false and entity.is_mob and B.visible_position(player,pos,obj) then
                local id=B.object_ids[obj]
                if not id then id=B.id('object');B.object_ids[obj]=id end
                B.objects[id]=obj
                local drops={}

                local species=core.registered_entities[entity.name]
                for _,r in ipairs(species and species.drops or {}) do if type(r.name)=='string' and #drops<8 then drops[#drops+1]=r.name end end
                rows[#rows+1]={target_id=id,kind=entity.name,position=pos,relation=entity.type=='monster' and 'hostile_species' or 'other_creature',distance=vector.distance(player:get_pos(),pos),possible_drops=drops}
                if #rows>=12 then break end
            end
        end
    end
    return rows
end
local function fingerprint(player)
    local inv=player:get_inventory();local rows={}
    for _,name in ipairs({'main','craft','craftresult'}) do
        rows[name]={};for i,s in ipairs(inv:get_list(name) or {}) do rows[name][i]=s:to_string() end
    end
    return core.sha1(core.write_json(rows))
end
local recipe_cache={}
local function corridor_depth(c,p)
    return (p.x-c.origin.x)*c.dx+(p.z-c.origin.z)*c.dz
end
local function corridor_observation(player,task)
    local c=task.scope.corridor;local targets={};local clear=0;local unknown=false;local floor_gap=false
    task.clear_depths=task.clear_depths or {}
    local exit=nil;local obstruction=false
    for depth=1,c.length do
        local open=true;local visible=true
        for side=-math.floor((c.width-1)/2),math.floor(c.width/2) do
            local x=c.origin.x+c.dx*depth+c.dz*side;local z=c.origin.z+c.dz*depth-c.dx*side
            for height=1,c.height do
                local p={x=x,y=c.origin.y+height,z=z}
                if not B.visible_cell(player,p) then open=false;visible=false
                else
                    local node=core.get_node_or_nil(p)
                    local def=node and core.registered_nodes[node.name]

                    if node and node.name~='air' and (not def or def.walkable or (def.drowning or 0)>0 or (def.damage_per_second or 0)>0) then
                        open=false;obstruction=true
                        B.known_nodes[core.pos_to_string(p)]={position=p,item_id=node.name,name=B.name(node.name),at=B.now()}
                        targets[#targets+1]={position=p,item_id=node.name}
                    end
                end
            end
            local floor={x=x,y=c.origin.y,z=z}
            if not B.visible_cell(player,floor) then visible=false;open=false
            else
                local n=core.get_node_or_nil(floor);local def=n and core.registered_nodes[n.name]
                if not def or not def.walkable or (def.damage_per_second or 0)>0 then open=false;floor_gap=true end
            end
        end
        if open then task.clear_depths[depth]=true
        elseif visible then task.clear_depths[depth]=nil end
        if task.clear_depths[depth] and clear==depth-1 then
            clear=depth
            if c.until_open and task.furthest_dug and depth>task.furthest_dug and open then exit=exit or depth end
        end

        if c.until_open and task.furthest_dug and depth>task.furthest_dug and open then exit=exit or depth end
        if not visible then unknown=true end
        if exit then break end
    end

    if c.until_open and not task.furthest_dug and clear>=2 and not obstruction then exit=2 end
    local cleared=c.until_open and exit~=nil or clear>=c.length
    if cleared then task.exit_depth=task.exit_depth or exit or c.length end

    local arrived=task.exit_depth and corridor_depth(c,player:get_pos())>=task.exit_depth-.18
    return targets,{clear_length=clear,limit=c.length,unknown_ahead=unknown,floor_gap=floor_gap,
        complete=cleared and (not c.advance or arrived) or c.advance and arrived or false,cleared=cleared,exit_depth=task.exit_depth,furthest_dug=task.furthest_dug or 0,
        observed_obstruction=obstruction}
end
function B.survival_observe(s,player)
    s.capabilities.survival_tasks=core.settings:get_bool('jev_survival_tasks',false)
    if not player then return end
    local navigation=B.navigation_observe and B.navigation_observe(player) or {}
    for _,route in ipairs(navigation) do for _,row in ipairs(route.blockers) do
        B.known_nodes[core.pos_to_string(row.position)]={position=row.position,item_id=row.item_id,name=B.name(row.item_id),at=B.now()}
    end end
    local nodes={};local focus=s.view.focus
    for _,row in ipairs(s.view.environment.visible_nodes or {}) do B.known_nodes[core.pos_to_string(row.position)]={position=row.position,item_id=row.item_id,name=row.name,at=B.now()} end
    if focus then B.known_nodes[core.pos_to_string(focus.position)]={position=focus.position,item_id=focus.item_id,name=focus.name,at=B.now()} end
    for _,row in ipairs(B.site_terrain and B.site_terrain(player) or {}) do
        B.known_nodes[core.pos_to_string(row.position)]={position=row.position,item_id=row.item_id,name=B.name(row.item_id),at=B.now()}
        local b=row.obstacle
        if b then B.known_nodes[core.pos_to_string(b.position)]={position=b.position,item_id=b.item_id,name=B.name(b.item_id),at=B.now()} end
    end
    local known={};for _,row in pairs(B.known_nodes) do known[#known+1]=row end
    table.sort(known,function(a,b)
        local da=vector.distance(a.position,player:get_pos());local db=vector.distance(b.position,player:get_pos())
        return da<db
    end)
    local representatives,rest,seen={},{},{}
    for _,row in ipairs(known) do
        if not seen[row.item_id] and B.visible_position(player,row.position) then representatives[#representatives+1]=row;seen[row.item_id]=true
        else rest[#rest+1]=row end
    end
    known=representatives;for _,r in ipairs(rest) do known[#known+1]=r end
    for _,row in ipairs(known) do
        local key=core.pos_to_string(row.position)
        if B.now()-row.at>300000 then B.known_nodes[key]=nil
        elseif #nodes<48 and B.visible_position(player,row.position) then
            local node=core.get_node_or_nil(row.position)
            if node and node.name~='air' then
                row.item_id=node.name;row.name=B.name(node.name)
                local d,reason=diggable(player,row)
                nodes[#nodes+1]={target_id=key,position=row.position,item_id=row.item_id,name=row.name,diggable=d~=nil,reason=reason,
                    dig_ms=d and d.time_ms,harvest=d and d.harvest,focused=focus and vector.equals(row.position,focus.position) or false}
            end
        end
    end
    local objects=visible_objects(player);local tools={}
    for slot=1,math.min(36,player:get_inventory():get_size('main')) do
        local stack=player:get_inventory():get_stack('main',slot)
        if not stack:is_empty() and allowed_item(stack) then
            local cap=stack:get_tool_capabilities()
            tools[#tools+1]={slot=slot,item_id=stack:get_name(),name=B.name(stack:get_name()),count=stack:get_count(),wear=stack:get_wear(),
                damage=cap.damage_groups and cap.damage_groups.fleshy or 0,interval=cap.full_punch_interval or 1.4,
                tool=core.registered_tools[stack:get_name()]~=nil,placeable_support=B.build_materials[stack:get_name()]==true}
        end
    end
    local travel=B.travel_observe and B.travel_observe(player) or {}
    local bench=B.crafting and B.crafting.table_near(player) or nil
    local recipe_key=fingerprint(player)..tostring(bench~=nil)
    if B.crafting and recipe_cache.key~=recipe_key then
        recipe_cache={key=recipe_key,rows={}};local seen={}
        for _,recipe in ipairs(B.crafting.catalog) do
            if not seen[recipe.item_id] and #recipe_cache.rows<40 then
                local plan=B.crafting.resolve(player,recipe.recipe_id)
                if plan then
                    seen[recipe.item_id]=true
                    recipe_cache.rows[#recipe_cache.rows+1]={recipe_id=recipe.recipe_id,item_id=recipe.item_id,name=recipe.name,count=recipe.count,ingredients=recipe.ingredients,grid=recipe.grid}
                end
            end
        end
    end
    local grounded,dry_land=false,false
    if B.ground_status then grounded,dry_land=B.ground_status(player) end
    local drops={}
    for _,object in ipairs(core.get_objects_inside_radius(B.eye(player),8)) do
        if not object:is_player() and #drops<12 then
            local entity=object:get_luaentity();local p=object:get_pos()
            if entity and entity.itemstring and p then

                local visible=true
                for hit in core.raycast(B.eye(player),p,false,false) do if hit.type=='node' then visible=false;break end end
                if visible then local item=ItemStack(entity.itemstring);drops[#drops+1]={item_id=item:get_name(),count=item:get_count(),position=p} end
            end
        end
    end
    local item_help={};local documented={}
    local function document(name)
        if documented[name] or name=='' or #item_help>=16 then return end
        documented[name]=true;local def=core.registered_items[name]
        if def and (def._doc_items_longdesc or def._doc_items_usagehelp) then
            local function clean(value)
                value=tostring(value or '')
                if core.get_translated_string then value=core.get_translated_string('zh_CN',value) end
                if core.strip_colors then value=core.strip_colors(value) end
                value=value:gsub('\27%([^)]*%)',''):gsub('\27E',''):gsub('\27F','')
                return value:sub(1,1400)
            end
            item_help[#item_help+1]={item_id=name,name=B.name(name),description=clean(def._doc_items_longdesc),usage=clean(def._doc_items_usagehelp)}
        end
    end
    document(player:get_wielded_item():get_name())
    for _,stack in ipairs(player:get_inventory():get_list('main') or {}) do document(stack:get_name()) end
    for _,row in ipairs(nodes) do document(row.item_id) end
    local item_facts={};local fact_seen={}
    local function fact(name)
        if not B.item_facts or not name or name=='' or fact_seen[name] or #item_facts>=64 then return end
        fact_seen[name]=true;local row=B.item_facts(name)
        if row then item_facts[#item_facts+1]=row end
    end
    for _,r in ipairs(tools) do fact(r.item_id) end
    for _,r in ipairs(nodes) do fact(r.item_id);local f=B.item_facts and B.item_facts(r.item_id);for _,id in ipairs(f and f.possible_drops or {}) do fact(id) end end
    for _,r in ipairs(drops) do fact(r.item_id) end
    for _,r in ipairs(objects) do for _,id in ipairs(r.possible_drops) do fact(id) end end
    local view={nodes=nodes,entities=objects,drops=drops,tools=tools,travel=travel,item_help=item_help,item_facts=item_facts,in_water=B.in_water and B.in_water(player) or false,
        grounded=grounded,dry_land=dry_land,shore=B.water_contact and B.water_contact(player) and B.shore_observe(player) or {},
        recipes=recipe_cache.rows or {},workbench=bench,navigation=navigation,task=nil}
    s.view.survival=view
    local task=B.task;if not task then return end
    if B.now()>=task.expires then B.revoke('task_deadline');return end
    view.task={task_id=task.id,grant_id=task.grant_id,status=task.pending and 'EXECUTING' or 'RUNNING',expires_in_ms=task.expires-B.now(),actions={},defeated=task.defeated or {},craft_requirements={}}
    for _,id in ipairs(task.scope.recipes or {}) do
        local r=B.crafting.requirements and B.crafting.requirements(player,id)
        if r then view.task.craft_requirements[#view.task.craft_requirements+1]=r end
    end
    local ok,why=available(player);view.task.blocked_reason=why
    if not ok or task.pending or (task.scope.kind~='COMBAT' and task.scope.kind~='AGENT' and task.scope.kind~='TRAVEL' and not steady(player)) then return end
    local actions=view.task.actions;local private={}
    local function add(kind,public,details)
        if kind=='USE' and public.interaction=='self' and task.scope.consume==false then return end
        local motion=kind=='MOVE' or kind=='JUMP_MOVE' or kind=='SWIM' or kind=='SWIM_UP' or kind=='JUMP'
        if #actions>=(motion and 80 or 70) then return end
        local id=B.id('task-action');public.kind=kind;public.action_id=id;actions[#actions+1]=public
        private[id]={kind=kind,public=public,state=B.state_key(player),inventory=fingerprint(player),details=details}
    end
    if task.scope.kind=='AGENT' or task.scope.kind=='TRAVEL' and task.scope.navigation then
        local dig_count=0
        local focus_items={};for _,id in ipairs(task.scope.focus_items or {}) do focus_items[id]=true end
        local dig_nodes={};for _,row in ipairs(nodes) do dig_nodes[#dig_nodes+1]=row end
        table.sort(dig_nodes,function(a,b)
            local function rank(r)return (r.focused and 100 or 0)+(focus_items[r.item_id] and 50 or 0)-vector.distance(player:get_pos(),r.position) end
            return rank(a)>rank(b)
        end)
        local unique,others,types={},{},{}
        for _,r in ipairs(dig_nodes) do
            if r.diggable and not types[r.item_id] then unique[#unique+1]=r;types[r.item_id]=true
            else others[#others+1]=r end
        end
        dig_nodes=unique;for _,r in ipairs(others) do dig_nodes[#dig_nodes+1]=r end
        for _,row in ipairs(dig_nodes) do
            if row.diggable and dig_count<24 and (task.scope.alter~=false) then add('DIG',{target=xyz(row.position),item_id=row.item_id,dig_ms=row.dig_ms,harvest=row.harvest},{});dig_count=dig_count+1 end
        end
        if task.scope.alter~=false then
            local p=player:get_pos();local pos={x=math.floor(p.x+.5),y=math.floor(p.y+.1),z=math.floor(p.z+.5)}
            local n=core.get_node_or_nil(pos)
            local d=n and diggable(player,{position=pos,item_id=n.name,support_descent=true})
            if d then add('DIG_DOWN',{target=pos,item_id=n.name,dig_ms=d.time_ms,harvest=d.harvest,rise=-1},{support_descent=true}) end
        end
        for _,row in ipairs(objects) do
            if (row.relation=='hostile_species' or task.scope.creatures==true) and row.distance<=3 then
                local cap=player:get_wielded_item():get_tool_capabilities()
                if B.now()-B.last_combat>=(cap.full_punch_interval or 1.4)*1000 then add('ATTACK',{target_id=row.target_id,entity_kind=row.kind,target=row.position,distance=row.distance},{}) end
            end
        end
        local focus=s.view.focus;local held=player:get_wielded_item();local name=held:get_name()
        if focus and B.build_materials[name] and task.scope.kind=='AGENT' and task.scope.alter~=false then
            local p=focus.place_position;local cell={x=p.x,y=p.y,z=p.z,item_id=name}
            if B.can_place_cell(player,cell) then add('PLACE',{target=p,item_id=name},{cell=cell}) end
        end
        if B.build_materials[name] and task.scope.alter~=false then
            local cell=B.jump_place_cell and B.jump_place_cell(player,name)
            if cell and B.valid_build_stack(held,name) then
                add('JUMP_PLACE',{target={x=cell.x,y=cell.y,z=cell.z},item_id=name,rise=1,step_support=true},{cell=cell})
            end
            local placed={}
            for _,route in ipairs(navigation) do
                local p=route.place_step
                if p and not placed[core.pos_to_string(p)] then
                    local cell={x=p.x,y=p.y,z=p.z,item_id=name}
                    if B.can_place_cell(player,cell) then
                        add('PLACE',{target=p,item_id=name,route_id=route.id,step_support=true},{cell=cell});placed[core.pos_to_string(p)]=true
                    end
                end
            end
            if task.scope.site_box then
                local b=task.scope.site_box;local o=b.origin;local count=0
                for x=o.x,o.x+b.width-1 do for z=o.z,o.z+b.depth-1 do
                    local p={x=x,y=o.y-1,z=z};local cell={x=x,y=p.y,z=z,item_id=name}

                    if count<8 and B.visible_cell(player,p) and B.can_place_cell(player,cell) then
                        add('PLACE',{target=p,item_id=name,site_support=true},{cell=cell});count=count+1
                    end
                end end
            end
        end
        if name=='mcl_crafting_table:crafting_table' then
            local p=player:get_pos();local y=math.floor(p.y+.1)+1
            for _,d in ipairs({{1,0},{-1,0},{0,1},{0,-1}}) do
                local cell={x=math.floor(p.x+.5)+d[1],y=y,z=math.floor(p.z+.5)+d[2],item_id=name}
                if B.can_place_cell(player,cell) then add('PLACE',{target={x=cell.x,y=cell.y,z=cell.z},item_id=name},{cell=cell}) end
            end
        end
        local offered={}
        for _,id in ipairs(task.scope.kind=='AGENT' and task.scope.recipes or {}) do
            for _,candidate in ipairs(B.crafting.dependencies(player,id)) do
                if not offered[candidate] then
                    offered[candidate]=true;local recipe=B.crafting.by_id[candidate]
                    add('CRAFT',{recipe_id=candidate,item_id=recipe.item_id,count=recipe.count,dependency_for=candidate~=id and id or nil},{})
                end
            end
        end
        for index,recipe in ipairs(task.scope.kind=='AGENT' and not task.scope.model_outcome and recipe_cache.rows or {}) do
            if index<=8 and not offered[recipe.recipe_id] then add('CRAFT',{recipe_id=recipe.recipe_id,item_id=recipe.item_id,count=recipe.count},{}) end
        end
        for _,row in ipairs(tools) do
            if row.slot~=player:get_wield_index() then add(row.slot<=9 and 'SELECT' or 'EQUIP',{slot=row.slot,item_id=row.item_id,damage=row.damage,tool=row.tool,placeable_support=row.placeable_support},{}) end
        end
        if task.scope.kind=='AGENT' and task.scope.model_outcome and B.interaction_candidates then B.interaction_candidates(player,nodes,add) end

        for sector=0,3 do
            local yaw=sector*math.pi/2
            if math.abs(math.atan2(math.sin(yaw-player:get_look_horizontal()),math.cos(yaw-player:get_look_horizontal())))>.25 then
                add('LOOK',{yaw=yaw,pitch=.1,sector=sector},{})
            end
        end
    elseif task.scope.kind=='DIG' then
        view.task.target_states={}
        local targets=task.scope.targets or {}
        if task.scope.corridor then targets,view.task.path=corridor_observation(player,task) end
        for index,row in ipairs(targets) do
            local d,reason=diggable(player,row)
            if #view.task.target_states<48 then view.task.target_states[#view.task.target_states+1]={target_index=index,position=row.position,item_id=row.item_id,reason=reason,dig_ms=d and d.time_ms} end
            if d then add('DIG',{target=xyz(row.position),item_id=row.item_id,target_index=index,dig_ms=d.time_ms,harvest=d.harvest},d) end
            if reason and reason~='target_changed' then view.task.blocked_reason=reason end
            if #actions>=20 then break end
        end
    elseif task.scope.kind=='PLACE' then
        view.task.target_states={}
        for index,row in ipairs(task.scope.targets) do
            local pos=row.position;local cell={x=pos.x,y=pos.y,z=pos.z,item_id=row.item_id}
            local pointed,reason=B.can_place_cell(player,cell)
            view.task.target_states[#view.task.target_states+1]={position=pos,item_id=row.item_id,reason=reason}
            if pointed then
                local found=false
                for _,tool in ipairs(tools) do
                    if tool.item_id==row.item_id then
                        found=true
                        if player:get_wield_index()==tool.slot then add('PLACE',{target=pos,item_id=row.item_id,target_index=index},{cell=cell})
                        else add('SELECT',{slot=tool.slot,item_id=row.item_id},{}) end
                    end
                end
                if not found then view.task.blocked_reason='material_not_in_hotbar';view.task.missing={[row.item_id]=1} end
            else view.task.blocked_reason=reason end
        end
    elseif task.scope.kind=='CRAFT' then
        for _,id in ipairs(task.scope.recipes or {}) do
            local plan,reason,missing=B.crafting.resolve(player,id)
            if plan then add('CRAFT',{recipe_id=id,item_id=plan.recipe.item_id,count=plan.recipe.count},plan)
            else
                view.task.blocked_reason=reason;view.task.missing=missing
                for _,dependency in ipairs(B.crafting.dependencies and B.crafting.dependencies(player,id) or {}) do
                    local p=B.crafting.resolve(player,dependency)
                    if p then add('CRAFT',{recipe_id=dependency,item_id=p.recipe.item_id,count=p.recipe.count,dependency_for=id},p) end
                end
            end
        end
    elseif task.scope.kind=='COMBAT' then
        for _,row in ipairs(objects) do
            local allow=task.scope.hostiles and row.relation=='hostile_species'
            for _,id in ipairs(task.scope.entities or {}) do if id==row.target_id then allow=true end end
            if allow then
                local held=player:get_wielded_item();local cap=held:get_tool_capabilities();local interval=cap.full_punch_interval or 1.4
                if row.distance<=math.min(4,held:get_definition().range or 3) and B.now()-B.last_combat>=interval*1000 then
                    add('ATTACK',{target_id=row.target_id,entity_kind=row.kind,target=row.position,distance=row.distance,cooldown_ms=interval*1000},{object=B.objects[row.target_id]})
                end
            end
        end
    end
    if task.scope.kind~='CRAFT' and task.scope.kind~='PLACE' then
        if task.scope.kind~='AGENT' and task.scope.kind~='TRAVEL' then
            for _,row in ipairs(tools) do
            if row.slot~=player:get_wield_index() and row.tool then
                local comparison={}
                for _,candidate in ipairs(actions) do
                    if candidate.kind=='DIG' and not comparison[candidate.item_id] then
                        local def=core.registered_nodes[candidate.item_id]
                        local stack=player:get_inventory():get_stack('main',row.slot)
                        local params=def and core.get_dig_params(def.groups,stack:get_tool_capabilities(),stack:get_wear())
                        if params and params.diggable then comparison[candidate.item_id]={dig_ms=math.max(50,params.time*1000),wear_after=stack:get_wear()+(params.wear or 0)} end
                    end
                end
                add(row.slot<=9 and 'SELECT' or 'EQUIP',{slot=row.slot,item_id=row.item_id,damage=row.damage,tool=row.tool,wear=row.wear,dig_comparison=comparison},{})
            end
        end
        end
        if B.legal_build_step then
            local moves=travel
            if #moves==0 then
                moves={}
                for _,d in ipairs({{1,0},{-1,0},{0,1},{0,-1}}) do local p=B.legal_build_step(player,d[1],d[2]);if p then moves[#moves+1]={kind='MOVE',target=p} end end
            end
            for _,move in ipairs(moves) do
                local pos=move.target
                local in_scope=true
                if pos and task.scope.corridor then
                    local c=task.scope.corridor;local depth=corridor_depth(c,pos)
                    local side=math.abs((pos.x-c.origin.x)*c.dz-(pos.z-c.origin.z)*c.dx)
                    in_scope=depth>=0 and depth<=c.length and side<=math.max(1,c.width/2)
                    if view.task.path and view.task.path.floor_gap and #actions==0 then in_scope=false;view.task.blocked_reason='path_has_no_support' end
                end
                if pos and in_scope and (task.scope.kind=='AGENT' or task.scope.kind=='TRAVEL' or vector.distance(pos,task.origin)<12) then
                    local distance=nil;for _,obj in ipairs(objects) do if obj.relation=='hostile_species' then distance=math.min(distance or 1000,vector.distance(pos,obj.position)) end end
                    local extra={target=pos,distance_to_nearest_enemy=distance,dx=move.dx,dz=move.dz,rise=move.rise,duration_ms=move.duration_ms,reaches_solid_land=move.reaches_solid_land,safe_walk_margin=move.safe_walk_margin}
                    if task.scope.corridor then
                        local c=task.scope.corridor;extra.passage_depth=corridor_depth(c,pos)
                        extra.distance_from_passage=math.abs((pos.x-c.origin.x)*c.dz-(pos.z-c.origin.z)*c.dx)
                    end
                    add(move.kind,extra,{})
                end
            end
        end
    end
    B.task_contexts[s.snapshot_seq]={at=B.now(),set=s.candidate_set_id,values=private};B.task_contexts[s.snapshot_seq-32]=nil
end
local function result(m,status,reason,extra)
    local r=extra or {};r.request_id=m.request_id;r.command_id=m.command_id;r.status=status;r.reason=reason;r.at_game_mono_ms=B.now()
    if m.command_id and not B.command_ledger.remember(B,m.command_id,r) then r.status='UNKNOWN';r.reason='ledger_error' end
    return r
end
local function place(m,task,player,cell,kind)
    local pointed,reason=B.can_place_cell(player,cell)
    if not pointed then return result(m,'REJECTED',reason) end
    local stack=player:get_wielded_item();local before=stack:get_count()
    if not B.valid_build_stack(stack,cell.item_id) then return result(m,'REJECTED','wrong_held_material') end
    if not B.command_ledger.remember(B,m.command_id,{status='RUNNING',command_id=m.command_id,kind=kind}) then return result(m,'UNKNOWN','journal_error') end
    local support_node=core.get_node_or_nil(pointed.under)
    local active={command_id=m.command_id,item_id=cell.item_id,target=pointed.above,hook=false};B.active_command=active
    local success,returned=pcall(stack:get_definition().on_place,stack,player,pointed);B.active_command=nil
    if success and returned~=nil then success=pcall(function()player:set_wielded_item(returned)end) end
    B.previous=B.state_key(player)
    local node=core.get_node_or_nil(pointed.above);local after=player:get_wielded_item():get_count()
    if success and active.hook and node and node.name==cell.item_id and after==before-1 then
        if B.present then B.present('PLACE',pointed.above,450,m.command_id) end
        task.remaining=task.remaining-1
        return result(m,'SUCCEEDED',nil,{kind=kind,target=pointed.above,item_id=cell.item_id,count_before=before,count_after=after,
            support_position=kind=='JUMP_PLACE' and xyz(pointed.under) or nil,support_item_id=kind=='JUMP_PLACE' and support_node.name or nil})
    end
    B.uncertain=true;B.revoke('place_unknown');return result(m,'UNKNOWN','placement_unconfirmed')
end
local function same_anchor(m)
    local a=m.anchor
    return a and a.game_instance_id==B.instance_id and a.bridge_epoch==B.bridge_epoch and a.player_life_id==B.life_id and a.control_epoch==B.control_epoch
end
function B.survival_command(m)
    if not B.protocol.command(m) then B.revoke('invalid_task_command');return nil end
    if m.kind=='TASK_CATALOG' then return {request_id=m.request_id,status='CATALOG',catalog=B.crafting.page()} end
    local old=m.command_id and B.ledger[m.command_id]
    if old and old.status~='RUNNING' and old.status~='INPUT_ARMED' then
        if m.kind=='TASK_INPUT_DONE' and B.task and B.task.pending and B.task.pending.command_id==m.command_id then
            B.task.settle_until=B.now()+700;B.task.settle_origin=B.player():get_pos();B.task.pending=nil
            if B.stop_presentation then B.stop_presentation() end
            B.previous=B.state_key(B.player())
        end
        local r=table.copy(old);r.request_id=m.request_id;return r
    end
    if old and m.kind=='TASK_STEP' then local r=table.copy(old);r.request_id=m.request_id;return r end
    if not core.settings:get_bool('jev_survival_tasks',false) or m.session_id~=B.session_id then return result(m,'REJECTED','not_bound_or_enabled') end
    if m.seq<=B.command_seq then return result(m,'REJECTED','old_command') end;B.command_seq=m.seq
    local player=B.player();local ok,why=available(player)
    if not ok then return result(m,'REJECTED',why) end
    if m.kind=='TASK_ARM' then
        if not same_anchor(m) or B.sources[m.source_event_id] then return result(m,'REJECTED','stale_source') end
        local s=B.snapshots[m.anchor.snapshot_seq]
        if not s or s.public.candidate_set_id~=m.anchor.candidate_set_id or B.now()-s.public.sampled_at_game_mono_ms>1500 then return result(m,'REJECTED','source_expired') end
        local count=0;for _ in pairs(B.sources) do count=count+1 end
        if count>=512 then return result(m,'REJECTED','source_limit') end
        if m.scope.kind=='DIG' then
            for _,target in ipairs(m.scope.targets) do
                local known=B.known_nodes[core.pos_to_string(target.position)]
                local proven=known and known.item_id==target.item_id
                if not proven then
                    for _,r in pairs(B.ledger) do
                        if r.status=='SUCCEEDED' and r.target and not r.removed and vector.equals(r.target,target.position) and r.item_id==target.item_id then proven=true;break end
                    end
                end
                if not proven then return result(m,'REJECTED','unobserved_target') end
            end
            if m.scope.corridor then
                local c=m.scope.corridor;local pp=player:get_pos()
                if vector.distance(pp,c.origin)>2 or math.abs(math.floor(pp.y+.1)-c.origin.y)>0 then return result(m,'REJECTED','passage_origin_changed') end
            end
        elseif m.scope.kind=='PLACE' then
            for _,row in ipairs(m.scope.targets) do
                if not B.build_materials[row.item_id] or not B.visible_cell(player,row.position) then return result(m,'REJECTED','unobserved_place') end
            end
        elseif m.scope.kind=='AGENT' or m.scope.kind=='TRAVEL' then
            for _,id in ipairs(m.scope.recipes or {}) do if not B.crafting.by_id[id] then return result(m,'REJECTED','unknown_recipe') end end
        elseif m.scope.kind=='CRAFT' then
            for _,id in ipairs(m.scope.recipes) do if not B.crafting.by_id[id] then return result(m,'REJECTED','unknown_recipe') end end
        elseif m.scope.kind=='COMBAT' then
            for _,id in ipairs(m.scope.entities or {}) do if not B.objects[id] then return result(m,'REJECTED','target_not_visible') end end
        end
        B.sources[m.source_event_id]=B.now();B.grant=nil;B.project=nil
        B.task={id=m.task_id,grant_id=m.grant_id,scope=m.scope,origin=player:get_pos(),control_epoch=B.control_epoch,input_seq=B.input_seq,expires=B.now()+m.duration_ms,remaining=m.max_effects}
        return result(m,'ARMED',nil,{task_id=m.task_id,grant_id=m.grant_id})
    end
    local task=B.task
    if not task or task.id~=m.task_id or task.grant_id~=m.grant_id or task.control_epoch~=B.control_epoch or task.input_seq~=B.input_seq or B.now()>=task.expires then return result(m,'REJECTED','task_revoked') end
    if m.kind=='TASK_CANCEL_STEP' then
        local pending=task.pending
        if pending then
            task.pending=nil
            if pending.consumed then
                task.remaining=task.remaining-1;result({command_id=pending.command_id},'SUCCEEDED',nil,pending.consumed)
            elseif not pending.receipt then result({command_id=pending.command_id},'CANCELLED',m.reason=='voice_attention' and 'voice_attention' or 'survival_interrupt',{kind=pending.kind}) end
            task.settle_until=B.now()+700;task.settle_origin=player:get_pos()
            if B.stop_presentation then B.stop_presentation() end
            B.previous=B.state_key(player)
        end
        return result(m,'STEP_CANCELLED')
    end
    if m.kind=='TASK_INPUT_DONE' then
        local pending=task.pending
        if not pending or pending.command_id~=m.command_id then return result(m,'REJECTED','input_not_armed') end
        task.pending=nil;B.previous=B.state_key(player)
        if B.stop_presentation then B.stop_presentation() end
        if pending.kind=='JUMP_PLACE' then
            task.settle_until=B.now()+700;task.settle_origin=player:get_pos()
            if pending.receipt then
                local row=table.copy(pending.receipt);row.request_id=m.request_id;row.position=xyz(player:get_pos());row.position_before=xyz(pending.state.position);return row
            end
            return result(m,'REJECTED','jump_did_not_clear_cell',{kind='JUMP_PLACE'})
        end
        if pending.kind=='USE' then
            if pending.consumed then task.remaining=task.remaining-1;return result(m,'SUCCEEDED',nil,pending.consumed) end
            return result(m,'REJECTED','use_not_consumed',{kind='USE',item_id=pending.public.item_id})
        end
        if pending.kind=='SWIM_UP' or pending.kind=='SWIM' or pending.kind=='JUMP_MOVE' then
            local _,dry=B.ground_status(player)
            task.buoyant=B.water_contact(player) and not dry
        end
        if pending.kind=='MOVE' or pending.kind=='JUMP_MOVE' or pending.kind=='JUMP' then task.settle_until=B.now()+700;task.settle_origin=player:get_pos() end
        local moved=pending.public.target and vector.distance(player:get_pos(),pending.public.target)<.65
        local swim_progress=pending.kind=='SWIM' and vector.distance(player:get_pos(),pending.state.position)>.2
        local success=pending.kind=='SELECT' and player:get_wield_index()==pending.public.slot or
            (pending.kind=='MOVE' or pending.kind=='JUMP_MOVE') and moved or
            pending.kind=='LOOK' or pending.kind=='JUMP' or swim_progress or
            pending.kind=='SWIM_UP' and (player:get_pos().y>pending.state.position.y+.15 or not B.in_water(player))
        return result(m,success and 'SUCCEEDED' or 'REJECTED',success and 'input_confirmed' or 'input_target_not_reached',
            {kind=pending.kind,reached_air=pending.reached_air,position=xyz(player:get_pos()),position_before=xyz(pending.state.position)})
    end
    if task.pending then return result(m,'REJECTED','step_inflight') end
    if task.remaining<=0 then return result(m,'REJECTED','quantity_complete') end
    if not same_anchor(m) then return result(m,'REJECTED','stale_source') end
    local set=B.task_contexts[m.anchor.snapshot_seq];local a=set and set.values[m.action_id]
    if not a or set.set~=m.anchor.candidate_set_id or B.now()-set.at>1500 then return result(m,'REJECTED','candidate_expired') end
    local current=B.state_key(player)
    local unchanged=B.same_key(a.state,current)
    if a.kind=='ATTACK' or a.kind=='SWIM_UP' or a.kind=='SWIM' then unchanged=a.state.slot==current.slot and (current.controls==0 or task.buoyant) end
    if (a.kind=='MOVE' or a.kind=='JUMP_MOVE') and (task.scope.kind=='AGENT' or task.scope.kind=='TRAVEL') then

        unchanged=a.state.slot==current.slot and math.abs(a.state.yaw-current.yaw)<.1 and
            vector.distance(a.state.position,current.position)<1.6 and (current.controls==0 or task.buoyant)
    end
    if not unchanged or a.inventory~=fingerprint(player) then return result(m,'REJECTED','player_state_changed') end
    if m.kind=='TASK_AIM' then
        local target=a.public.target
        if a.kind=='ATTACK' then local obj=B.objects[a.public.target_id];target=obj and obj:get_pos() end
        if not target or not B.present then return result(m,'REJECTED','no_presentation_target') end
        B.present('AIM',target,1800,m.request_id)
        return result(m,'AIMING',nil,{target=xyz(target)})
    end
    if ({DIG=true,DIG_DOWN=true,PLACE=true,USE=true,ATTACK=true})[a.kind] and not (a.kind=='USE' and a.public.interaction=='self') then
        local target=a.public.target
        if a.kind=='ATTACK' then local obj=B.objects[a.public.target_id];target=obj and obj:get_pos() end
        if target and B.facing_target and not B.facing_target(player,target) then return result(m,'REJECTED','camera_not_aligned') end
    end
    if ({SELECT=true,MOVE=true,JUMP_MOVE=true,SWIM=true,SWIM_UP=true,JUMP=true,JUMP_PLACE=true,LOOK=true})[a.kind] then
        if a.kind=='JUMP_PLACE' then
            local cell=B.jump_place_cell(player,a.public.item_id)
            if not cell or not vector.equals(cell,a.public.target) or task.scope.alter==false then return result(m,'REJECTED','jump_place_no_longer_legal') end
        end
        if a.kind=='SELECT' and player:get_inventory():get_stack('main',a.public.slot):get_name()~=a.public.item_id then return result(m,'REJECTED','slot_changed') end
        if a.kind=='MOVE' or a.kind=='JUMP_MOVE' or a.kind=='SWIM' then
            local p=player:get_pos();local move=B.travel_step(player,a.public.dx or a.public.target.x-math.floor(p.x+.5),a.public.dz or a.public.target.z-math.floor(p.z+.5))
            if not move or move.kind~=a.kind or vector.distance(move.target,a.public.target)>.8 then return result(m,'REJECTED','move_no_longer_legal') end
        end
        task.pending=table.copy(a);task.pending.command_id=m.command_id;task.pending.expires=math.min(task.expires,B.now()+(a.kind=='SWIM_UP' and 9000 or 6500))
        if B.present and (a.kind=='MOVE' or a.kind=='JUMP_MOVE' or a.kind=='SWIM') then B.present('MOVE',a.public.target,6000,m.command_id) end
        if B.present and a.kind=='JUMP_PLACE' then B.present('AIM',a.public.target,1800,m.command_id) end
        return result(m,'INPUT_ARMED',nil,{kind=a.kind=='SELECT' and 'PROJECT_SELECT' or a.kind=='MOVE' and 'PROJECT_MOVE' or a.kind,
            slot=a.public.slot,target=a.public.target,item_id=a.public.item_id,yaw=a.public.yaw,pitch=a.public.pitch,duration_ms=a.public.duration_ms,safe_walk_margin=a.public.safe_walk_margin})
    elseif a.kind=='USE' and a.public.interaction=='self' then
        if task.scope.consume==false then return result(m,'REJECTED','consumption_not_authorized') end
        if not B.item_facts(a.public.item_id).edible then return result(m,'REJECTED','not_edible') end
        task.pending=table.copy(a);task.pending.command_id=m.command_id;task.pending.expires=B.now()+6000
        task.pending.hunger_before=mcl_hunger and mcl_hunger.get_hunger(player)

        return result(m,'INPUT_ARMED',nil,{kind='USE',interaction='self',item_id=a.public.item_id,duration_ms=a.public.duration_ms})
    elseif a.kind=='USE' or a.kind=='EQUIP' then
        if not B.command_ledger.remember(B,m.command_id,{status='RUNNING',command_id=m.command_id,kind=a.kind}) then return result(m,'UNKNOWN','journal_error') end
        local ok,info,reason,unknown=pcall(a.kind=='USE' and B.use_item or B.equip_item,player,a,m.command_id)
        if not ok or unknown then B.uncertain=true;B.revoke('item_interaction_unknown');return result(m,'UNKNOWN','item_interaction_unknown') end
        if not info then return result(m,'REJECTED',reason) end
        if a.kind=='USE' then task.remaining=task.remaining-1 end
        return result(m,'SUCCEEDED',nil,info)
    elseif a.kind=='DIG' or a.kind=='DIG_DOWN' then
        local d,reason=diggable(player,{position=a.public.target,item_id=a.public.item_id,support_descent=a.kind=='DIG_DOWN'});if not d then return result(m,'REJECTED',reason) end
        task.pending=table.copy(a);task.pending.command_id=m.command_id;task.pending.details=d
        B.command_ledger.remember(B,m.command_id,{status='RUNNING',command_id=m.command_id,kind='DIG',target=a.public.target,item_id=a.public.item_id})
        task.pending.ready_at=B.now()+d.time_ms;task.pending.expires=B.now()+d.time_ms+3000
        if B.present then B.present('DIG',a.public.target,d.time_ms+350,m.command_id) end

        return {request_id=m.request_id,command_id=m.command_id,status='RUNNING',kind='DIG',wait_ms=d.time_ms}
    elseif a.kind=='PLACE' then
        return place(m,task,player,a.details.cell,'PLACE')
    elseif a.kind=='CRAFT' then
        if not a.public.dependency_for and a.public.count>task.remaining then return result(m,'REJECTED','quantity_exceeded') end
        if not B.command_ledger.remember(B,m.command_id,{status='RUNNING',command_id=m.command_id,kind='CRAFT'}) then return result(m,'UNKNOWN','journal_error') end
        B.active_command={command_id=m.command_id,kind='CRAFT'}
        local success,info,reason,unknown=pcall(B.crafting.execute,player,a.public.recipe_id);B.active_command=nil;B.previous=B.state_key(player)
        if not success or unknown then B.uncertain=true;B.revoke('craft_unknown');return result(m,'UNKNOWN','craft_callback_unknown') end
        if not info then return result(m,'REJECTED',reason) end
        if not a.public.dependency_for then task.remaining=task.remaining-info.count end
        info.dependency_for=a.public.dependency_for
        B.event('crafted',info,'jev','native_craft');return result(m,'SUCCEEDED',nil,info)
    elseif a.kind=='ATTACK' then
        local obj=B.objects[a.public.target_id];local pos=obj and obj:get_pos()
        if not pos or not B.visible_position(player,pos,obj) or vector.distance(player:get_pos(),pos)>math.min(4,player:get_wielded_item():get_definition().range or 3) then return result(m,'REJECTED','target_moved') end
        if core.is_protected(pos,B.player_name) then return result(m,'REJECTED','protected_target') end
        local cap=player:get_wielded_item():get_tool_capabilities();local elapsed=(B.now()-B.last_combat)/1000
        if elapsed<(cap.full_punch_interval or 1.4) then return result(m,'REJECTED','weapon_cooldown') end
        if not B.command_ledger.remember(B,m.command_id,{status='RUNNING',command_id=m.command_id,kind='ATTACK'}) then return result(m,'UNKNOWN','journal_error') end
        B.active_command={command_id=m.command_id,kind='ATTACK'};B.last_combat=B.now()
        local success=pcall(obj.punch,obj,player,elapsed,cap,vector.normalize(vector.subtract(pos,B.eye(player))))
        if success and B.present then B.present('ATTACK',pos,450,m.command_id) end
        B.active_command=nil;B.previous=B.state_key(player);task.expected_wear_until=B.now()+700
        if not success then B.uncertain=true;B.revoke('attack_unknown');return result(m,'UNKNOWN','attack_callback_unknown') end
        local info={kind='ATTACK',target_id=a.public.target_id,entity_kind=a.public.entity_kind}
        task.remaining=task.remaining-1
        local entity=obj:get_luaentity()
        if not obj:get_pos() or entity and (entity.state=='die' or entity.dead) then
            task.defeated=task.defeated or {};task.defeated[#task.defeated+1]=a.public.target_id;info.defeated=true
        end
        task.attacked=task.attacked or {};task.attacked[a.public.target_id]={object=obj,kind=a.public.entity_kind}
        B.event('attacked',info,'jev','native_punch');return result(m,'SUCCEEDED',nil,info)
    end
    return result(m,'REJECTED','unsupported_task_action')
end
function B.task_allows_native_change(key)
    local task=B.task;local p=task and task.pending
    if not task or task.control_epoch~=B.control_epoch or task.input_seq~=B.input_seq then return false end
    if task.harvest_settle_until and B.now()<task.harvest_settle_until then
        local s=task.harvest_state
        if key.controls==0 and key.slot==s.slot and key.stack==s.stack and
            math.abs(key.position.x-s.position.x)<.03 and math.abs(key.position.z-s.position.z)<.03 and
            math.abs(key.position.y-s.position.y)<.12 then return true end
    end
    if p and p.kind=='JUMP_PLACE' and B.now()<p.expires then
        local old=p.state.position;local c=B.player():get_player_control()
        local presented=p.receipt or B.presentation and B.presentation.command==p.command_id
        return not (c.up or c.down or c.left or c.right or c.LMB or c.RMB) and
            math.abs(key.position.x-old.x)<.2 and math.abs(key.position.z-old.z)<.2 and key.position.y>=old.y-.1 and key.position.y<old.y+1.6 and
            key.slot==p.state.slot and (p.receipt or key.stack==p.state.stack) and
            (presented or math.abs(key.yaw-p.state.yaw)<.02 and math.abs(key.pitch-p.state.pitch)<.02)
    end
    if p and p.kind=='USE' and p.public.interaction=='self' and B.now()<p.expires then
        local c=B.player():get_player_control()
        local only_use=not (c.up or c.down or c.left or c.right or c.jump or c.sneak or c.LMB)
        return only_use and key.slot==p.state.slot and vector.distance(key.position,p.state.position)<.2 and
            (key.stack==p.state.stack or p.consumed~=nil)
    end
    if task.knockback_until and B.now()<task.knockback_until and B.previous then
        local p=B.previous
        if key.controls==0 and key.slot==p.slot and math.abs(key.yaw-p.yaw)<.001 and math.abs(key.pitch-p.pitch)<.001 and vector.distance(key.position,p.position)<2 then return true end
    end
    if task.settle_until and B.now()<task.settle_until and key.controls==0 and vector.distance(key.position,task.settle_origin)<1.6 then return true end
    if B.travel_allowed_change and B.travel_allowed_change(key,task) then return true end
    if p and p.kind=='LOOK' and B.now()<p.expires then return key.controls==0 end
    if (task.scope.kind=='AGENT' or task.scope.kind=='TRAVEL') and B.water_contact and B.water_contact(B.player()) and
        B.previous and key.slot==B.previous.slot and math.abs(key.yaw-B.previous.yaw)<.001 and math.abs(key.pitch-B.previous.pitch)<.001 and
        (key.controls==0 or task.buoyant) then return true end
    if task.expected_wear_until and B.now()<task.expected_wear_until and B.previous then
        local previous=B.previous
        local own_presentation=B.presentation and B.presentation.input_seq==B.input_seq and B.presentation.control_epoch==B.control_epoch
        return vector.distance(key.position,previous.position)<.02 and key.slot==previous.slot and key.controls==0 and
            (own_presentation or math.abs(math.atan2(math.sin(key.yaw-previous.yaw),math.cos(key.yaw-previous.yaw)))<.001)
    end
    if not p or B.now()>p.expires then return false end
    local s=p.state
    if p.kind=='SELECT' then return vector.distance(key.position,s.position)<.05 and (key.slot==s.slot or key.slot==p.public.slot) and key.controls==0 end
    if p.kind=='MOVE' then return vector.distance(key.position,s.position)<2 and math.abs(key.position.y-s.position.y)<.5 and key.slot==s.slot and key.stack==s.stack and math.abs(key.yaw-s.yaw)<.001 end
    return false
end
core.register_globalstep(function()
    local task=B.task;if not task then return end
    local p=task.pending;local player=B.player()
    if p and p.kind=='JUMP_PLACE' and not p.receipt and player and task.expires>B.now() and p.expires>B.now() and
        task.control_epoch==B.control_epoch and task.input_seq==B.input_seq and available(player) and
        player:get_pos().y>p.state.position.y+.8 then

        if B.can_place_cell(player,p.details.cell) and (not B.facing_target or B.facing_target(player,p.public.target)) then
            p.receipt=place({command_id=p.command_id},task,player,p.details.cell,'JUMP_PLACE')
        end
    end
    if p and p.kind=='SWIM_UP' and player then
        local node=core.get_node_or_nil(B.eye(player));local def=node and core.registered_nodes[node.name]
        if def and not ((def.drowning or 0)>0) then p.reached_air=true end
    end
    for id,row in pairs(task.attacked or {}) do
        local entity=row.object:get_luaentity()
        if entity and (entity.state=='die' or entity.dead or entity.health and entity.health<=0) then
            task.defeated=task.defeated or {};task.defeated[#task.defeated+1]=id;task.attacked[id]=nil
            B.event('creature_defeated',{target_id=id,kind=row.kind},'jev','native_death_after_punch')
        end
    end
    if B.now()>=task.expires then B.revoke('task_deadline');return end
    if p and B.now()>p.expires then
        if p.kind=='DIG' or p.kind=='DIG_DOWN' then

            task.pending=nil;result({command_id=p.command_id},'REJECTED','dig_window_expired');return
        end
        B.revoke('task_input_expired');return
    end
    if not p or (p.kind~='DIG' and p.kind~='DIG_DOWN') or B.now()<p.ready_at then return end
    if B.facing_target and not B.facing_target(player,p.public.target) then
        task.pending=nil;B.stop_presentation();result({command_id=p.command_id},'REJECTED','camera_not_aligned');return
    end
    task.pending=nil
    local m={command_id=p.command_id};local ok=available(player)
    local d,reason=ok and diggable(player,{position=p.public.target,item_id=p.public.item_id,support_descent=p.kind=='DIG_DOWN'}) or nil,'state_changed'
    local key=B.state_key(player)
    local same=B.same_key(p.state,key)
    if B.presentation and B.presentation.command==p.command_id then
        same=key.slot==p.state.slot and key.stack==p.state.stack and key.controls==p.state.controls and vector.distance(key.position,p.state.position)<.02
    end
    if not d or not same then result(m,'REJECTED',reason);return end
    B.active_command={kind='DIG',command_id=p.command_id,target=p.public.target,item_id=p.public.item_id,hook=false}
    local active=B.active_command;local success=pcall(d.definition.on_dig,p.public.target,d.node,player)
    B.active_command=nil;B.previous=B.state_key(player)
    local node=core.get_node_or_nil(p.public.target)
    if success and active.hook and node and node.name~=p.public.item_id then
        if p.kind=='DIG_DOWN' then task.settle_until=B.now()+1100;task.settle_origin=player:get_pos() end

        if p.kind=='DIG' then task.harvest_settle_until=B.now()+900;task.harvest_state=B.state_key(player) end
        B.known_nodes[core.pos_to_string(p.public.target)]=nil
        task.remaining=task.remaining-1
        if task.scope.corridor then
            local def=core.registered_nodes[p.public.item_id]
            if not def or def.walkable then task.furthest_dug=math.max(task.furthest_dug or 0,corridor_depth(task.scope.corridor,p.public.target)) end
        end
        result(m,'SUCCEEDED',nil,{kind='DIG',target=p.public.target,item_id=p.public.item_id,dig_ms=p.details.time_ms})
    else B.uncertain=true;B.revoke('dig_unknown');result(m,'UNKNOWN','dig_effect_unconfirmed') end
    if B.stop_presentation then B.stop_presentation() end
end)
end
