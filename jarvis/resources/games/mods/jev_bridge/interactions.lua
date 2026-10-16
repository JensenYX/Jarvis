

return function(B)
local facts_cache={}
local function text(value,language)
    value=tostring(value or '')
    if core.get_translated_string then value=core.get_translated_string(language or 'en',value) end
    if core.strip_colors then value=core.strip_colors(value) end
    return value:gsub('\27%([^)]*%)',''):gsub('\27E',''):gsub('\27F','')
end
function B.item_facts(name)
    if facts_cache[name] then return facts_cache[name] end
    local def=core.registered_items[name];if not def then return nil end
    local groups={}
    for k,v in pairs(def.groups or {}) do
        if v>0 and not k:match('^not_in_') and not k:match('^dig_') then groups[k]=v end
    end
    local drops={};local seen={}
    local function drop(value)
        if type(value)~='string' then return end
        local id=ItemStack(value):get_name()
        if id~='' and not seen[id] and #drops<10 then drops[#drops+1]=id;seen[id]=true end
    end
    if def.type=='node' then
        if def.drop==nil then drop(name)
        elseif type(def.drop)=='string' then drop(def.drop)
        elseif type(def.drop)=='table' then for _,r in ipairs(def.drop.items or {}) do for _,v in ipairs(r.items or {}) do drop(v) end end end
    end
    local plant=nil
    for _,r in pairs(mcl_farming and mcl_farming.plant_lists or {}) do
        if r.full_grown==name then plant={mature=true,final=r.full_grown} end
        for i,n in ipairs(r.names or {}) do if n==name then plant={mature=false,stage=i,final=r.full_grown} end end
    end
    local food=mcl_hunger and mcl_hunger.registered_foods[name]
    local row={item_id=name,name=B.name(name),groups=groups,possible_drops=drops,plant=plant,
        description=text(def._doc_items_longdesc):sub(1,420),usage=text(def._doc_items_usagehelp):sub(1,600),
        name_zh=text(def.short_description or def.description,'zh_CN'):sub(1,180),
        description_zh=text(def._doc_items_longdesc,'zh_CN'):sub(1,1400),usage_zh=text(def._doc_items_usagehelp,'zh_CN'):sub(1,1400),
        has_on_place=type(def.on_place)=='function',has_on_use=type(def.on_use)=='function',
        edible=(groups.eatable or 0)>0,food_points=food and food.saturation or groups.eatable,
        food_risk=food and food.poisontime and 'possible_food_poisoning' or nil}
    facts_cache[name]=row;return row
end

local function plain(stack)
    for k in pairs(stack:get_meta():to_table().fields or {}) do if k~='description' then return false end end
    return true
end
local function legal_face(player,under,above)
    if vector.distance(B.eye(player),under)>4 or not B.visible_position(player,under) then return false end
    if not B.visible_cell(player,above) then return false end
    if core.is_protected(under,B.player_name) or core.is_protected(above,B.player_name) then return false end
    local n=core.get_node_or_nil(under);local a=core.get_node_or_nil(above)
    local def=n and core.registered_nodes[n.name]
    local p=player:get_pos()

    if math.abs(p.x-under.x)<.85 and math.abs(p.z-under.z)<.85 and under.y<=p.y and under.y>p.y-1 then return false end
    return n and a and def and not def.on_rightclick and true or false
end

function B.interaction_candidates(player,nodes,add)
    local stack=player:get_wielded_item();local name=stack:get_name();local def=stack:get_definition()
    if stack:is_empty() or not plain(stack) or not def then return end
    local facts=B.item_facts(name)
    if facts.edible and (def.on_secondary_use or def.on_place) then
        if not mcl_hunger or mcl_hunger.get_hunger(player)<20 or (facts.groups.can_eat_when_full or 0)>0 then
            add('USE',{item_id=name,interaction='self',duration_ms=math.ceil(((mcl_hunger and mcl_hunger.EATING_DELAY) or 1.6)*1000+350)}, {})
        end
    end

    if B.build_materials[name] or def.type=='node' or type(def.on_place)~='function' then return end
    local count=0;local faces={};local remaining={};local seen={}

    for _,row in ipairs(nodes) do
        if not seen[row.item_id] then faces[#faces+1]=row;seen[row.item_id]=true else remaining[#remaining+1]=row end
    end
    for _,row in ipairs(remaining) do faces[#faces+1]=row end
    for _,row in ipairs(faces) do
        local under=row.position;local above=vector.offset(under,0,1,0)
        local n=core.get_node_or_nil(under);local a=core.get_node_or_nil(above)

        local suitable=not (facts.groups.hoe and ((core.get_item_group(n and n.name or '', 'cultivatable') or 0)==0 or not a or a.name~='air'))
        if count<14 and suitable and legal_face(player,under,above) then
            add('USE',{item_id=name,interaction='place',target=under,above=above,target_item_id=n.name},
                {pointed={type='node',under=under,above=above},before_node=n.name,before_above=a.name})
            count=count+1
        end
    end
end

function B.use_item(player,action,command)
    local stack=player:get_wielded_item();local pub=action.public;local name=stack:get_name()
    if name~=pub.item_id or not plain(stack) then return nil,'held_item_changed' end
    local pointed=action.details.pointed
    if not pointed or not legal_face(player,pointed.under,pointed.above) then return nil,'use_target_changed' end
    local n=core.get_node_or_nil(pointed.under);local a=core.get_node_or_nil(pointed.above)
    if n.name~=action.details.before_node or a.name~=action.details.before_above then return nil,'use_target_changed' end
    local before=stack:to_string();local fn=stack:get_definition().on_place
    if type(fn)~='function' then return nil,'no_item_callback' end
    local active={kind='USE',command_id=command,target=pointed.above,item_id=name};B.active_command=active
    local ok,returned=pcall(fn,stack,player,pointed)
    if ok and returned~=nil then ok=pcall(function()player:set_wielded_item(returned)end) end
    B.active_command=nil;B.previous=B.state_key(player)
    if not ok then return nil,'item_callback_unknown',true end
    local after_under=core.get_node_or_nil(pointed.under);local after_above=core.get_node_or_nil(pointed.above)
    if not after_under or not after_above then return nil,'use_effect_unknown',true end
    local changed=after_under.name~=n.name or after_above.name~=a.name or player:get_wielded_item():to_string()~=before
    if not changed then return nil,'no_effect' end
    if B.task then B.task.expected_wear_until=B.now()+350 end
    local info={kind='USE',item_id=name,target=pointed.under,above=pointed.above,
        node_before=n.name,node_after=after_under.name,above_before=a.name,above_after=after_above.name,
        count_before=ItemStack(before):get_count(),count_after=player:get_wielded_item():get_count()}
    if B.present then B.present('PLACE',pointed.under,450,command) end
    B.event('item_used',info,'jev','registered_item_callback')
    return info
end

function B.equip_item(player,action,command)
    local inv=player:get_inventory();local from=action.public.slot;local to=player:get_wield_index()
    local a=inv:get_stack('main',from);local b=inv:get_stack('main',to)
    if a:get_name()~=action.public.item_id or not plain(a) or not plain(b) then return nil,'inventory_changed' end
    local moves={{from_list='main',to_list='main',from_index=from,to_index=to,count=a:get_count()},
                 {from_list='main',to_list='main',from_index=to,to_index=from,count=b:get_count()}}
    for _,move in ipairs(moves) do if move.count>0 then
        for _,fn in ipairs(core.registered_allow_player_inventory_actions or {}) do
            local allowed=fn(player,'move',inv,move)
            if allowed and allowed<move.count then return nil,'inventory_move_denied' end
        end
    end end
    inv:set_stack('main',to,a);inv:set_stack('main',from,b)
    B.active_command={kind='EQUIP',command_id=command}
    local ok,err=pcall(function()
        for _,move in ipairs(moves) do if move.count>0 then
            for _,fn in ipairs(core.registered_on_player_inventory_actions or {}) do fn(player,'move',inv,move) end
        end end
    end)
    B.active_command=nil;B.previous=B.state_key(player)
    if not ok or inv:get_stack('main',to):to_string()~=a:to_string() or inv:get_stack('main',from):to_string()~=b:to_string() then return nil,'inventory_swap_unknown',true end
    return {kind='EQUIP',item_id=a:get_name(),slot=from,wield_slot=to}
end
core.register_on_item_eat(function(_,_,_,user,_,old_stack)
    local task=B.task;local pending=task and task.pending
    if not user or user:get_player_name()~=B.player_name or not pending or pending.kind~='USE' or pending.public.interaction~='self' then return end
    if task.input_seq~=B.input_seq or B.now()>task.expires then return end
    pending.consumed={kind='USE',interaction='self',item_id=pending.public.item_id,count=1,
        hunger_before=pending.hunger_before,hunger_after=mcl_hunger and mcl_hunger.get_hunger(user)}
    B.event('item_used',pending.consumed,'jev','native_item_eat')
end)
end
