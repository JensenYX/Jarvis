

return function(B)
local C={catalog={},by_id={},cursor=1};B.crafting=C
local function clean(stack)
    local fields=stack:get_meta():to_table().fields or {}
    for k in pairs(fields) do if k~='description' then return false end end
    return stack:get_wear()==0
end
local function matches(name,ingredient)
    if ingredient:sub(1,6)=='group:' then
        for group in ingredient:sub(7):gmatch('[^,]+') do if core.get_item_group(name,group)<=0 then return false end end
        return true
    end
    return name==ingredient
end
local function sorted_inventory(player)
    local rows={}
    for slot,stack in ipairs(player:get_inventory():get_list('main') or {}) do
        if not stack:is_empty() and clean(stack) then rows[#rows+1]={slot=slot,item_id=stack:get_name(),count=stack:get_count()} end
    end
    return rows
end
core.register_on_mods_loaded(function()
    local names={};for name in pairs(core.registered_items) do names[#names+1]=name end;table.sort(names)
    for _,name in ipairs(names) do
        for _,r in ipairs(core.get_all_craft_recipes(name) or {}) do
            local size=0
            for i in pairs(r.items or {}) do if type(i)=='number' then size=math.max(size,i) end end
            if r.method=='normal' and type(r.items)=='table' and size>0 and size<=9 and r.width<=3 then
                local output=ItemStack(r.output);local id=core.sha1(core.write_json({output=r.output,width=r.width,items=r.items})):sub(1,20)
                if not C.by_id[id] and not output:is_empty() then
                    local row={recipe_id=id,item_id=output:get_name(),name=B.name(output:get_name()),count=output:get_count(),width=r.width,ingredients=r.items}
                    row.grid=math.max(r.width,r.width>0 and math.ceil(size/r.width) or math.ceil(math.sqrt(size)))
                    C.by_id[id]=row;C.catalog[#C.catalog+1]=row
                end
            end
        end
    end
end)
function C.page()
    local rows={}
    for i=C.cursor,math.min(#C.catalog,C.cursor+39) do rows[#rows+1]=C.catalog[i] end
    local result={offset=C.cursor,total=#C.catalog,recipes=rows}
    C.cursor=C.cursor+40;if C.cursor>#C.catalog then C.cursor=1 end
    return result
end
function C.material_sources(item_id)
    local rows={}
    for _,r in ipairs(C.catalog) do
        if r.item_id==item_id and #rows<4 then
            rows[#rows+1]={ingredients=r.ingredients,count=r.count,grid=r.grid}
        end
    end
    return rows
end
function C.table_near(player)
    local eye=B.eye(player)
    for _,row in pairs(B.known_nodes or {}) do
        if row.item_id=='mcl_crafting_table:crafting_table' and vector.distance(eye,row.position)<=4 and B.visible_position(player,row.position) then
            local node=core.get_node_or_nil(row.position)
            if node and node.name==row.item_id and not core.is_protected(row.position,B.player_name) then return row.position end
        end
    end
end
function C.requirements(player,id)
    local recipe=C.by_id[id];if not recipe then return nil end
    local slots=sorted_inventory(player);local missing={}
    for _,ingredient in pairs(recipe.ingredients) do
        if ingredient~='' then
            local found=false
            for _,row in ipairs(slots) do
                if row.count>0 and matches(row.item_id,ingredient) then row.count=row.count-1;found=true;break end
            end
            if not found then missing[ingredient]=(missing[ingredient] or 0)+1 end
        end
    end
    return {recipe_id=id,item_id=recipe.item_id,grid=recipe.grid,missing=missing,
        inventory_blocker=not player:get_inventory():is_empty('craft') and 'craft_grid_occupied' or nil,
        workbench_required=recipe.grid>2 and not C.table_near(player) or false}
end
function C.resolve(player,id)
    local recipe=C.by_id[id];if not recipe then return nil,'unknown_recipe' end
    local inv=player:get_inventory()
    if not inv:is_empty('craft') or not inv:is_empty('craftresult') then return nil,'craft_grid_occupied' end
    if recipe.grid>2 and not C.table_near(player) then return nil,'workbench_required' end
    local slots=sorted_inventory(player);local selected={};local grid={};local missing={}
    local size=0;for i in pairs(recipe.ingredients) do if type(i)=='number' then size=math.max(size,i) end end
    for i=1,size do
        local ingredient=recipe.ingredients[i] or ''
        grid[i]=ItemStack('')
        if ingredient~='' then
            local found=nil
            for _,row in ipairs(slots) do
                if row.count>0 and matches(row.item_id,ingredient) then found=row;break end
            end
            if found then found.count=found.count-1;grid[i]=ItemStack(found.item_id);selected[#selected+1]=found.slot
            else missing[ingredient]=(missing[ingredient] or 0)+1 end
        end
    end
    if next(missing) then return nil,'ingredients_missing',missing end
    local width=recipe.width>0 and recipe.width or math.min(3,math.max(2,recipe.grid))
    local output,left=core.get_craft_result({method='normal',width=width,items=grid})
    if output.item:get_name()~=recipe.item_id or output.item:get_count()~=recipe.count then return nil,'recipe_changed' end
    return {recipe=recipe,grid=grid,selected=selected,width=width,output=output,left=left,inventory=inv}
end

function C.dependencies(player,id,depth,seen)
    depth=depth or 0;seen=seen or {};if depth>3 or seen[id] then return {} end;seen[id]=true
    local ready,reason,missing=C.resolve(player,id)
    if ready then return {id} end
    if reason=='workbench_required' then

        local inv=player:get_inventory()
        if inv:contains_item('main','mcl_crafting_table:crafting_table') then return {} end
        for _,r in ipairs(C.catalog) do
            if r.item_id=='mcl_crafting_table:crafting_table' then return C.dependencies(player,r.recipe_id,depth+1,seen) end
        end
        return {}
    end
    if reason~='ingredients_missing' then return {} end
    local result={};local wanted={}
    for ingredient in pairs(missing or {}) do wanted[#wanted+1]=ingredient end
    for _,r in ipairs(C.catalog) do
        local match=false
        for _,i in ipairs(wanted) do if matches(r.item_id,i) then match=true;break end end
        if match then
            local branch=C.dependencies(player,r.recipe_id,depth+1,table.copy(seen))
            for _,found in ipairs(branch) do result[found]=true end
        end
    end
    local ids={};for key in pairs(result) do ids[#ids+1]=key;if #ids>=12 then break end end
    return ids
end
local function add_to(rows,stack)
    stack=ItemStack(stack)
    for _,row in ipairs(rows) do if not row:is_empty() then stack=row:add_item(stack);if stack:is_empty() then return true end end end
    for i,row in ipairs(rows) do if row:is_empty() then rows[i]=ItemStack(stack);return true end end
    return stack:is_empty()
end
function C.execute(player,id)
    local plan,reason=C.resolve(player,id);if not plan then return nil,reason end
    local inv=plan.inventory;local original_main=inv:get_list('main');local after_main={}
    for i,s in ipairs(original_main) do after_main[i]=ItemStack(s) end
    for _,slot in ipairs(plan.selected) do after_main[slot]:take_item(1) end
    local original_grid=inv:get_list('craft');local original_width=inv:get_width('craft');local original_size=inv:get_size('craft')
    local predicted=core.craft_predict(ItemStack(plan.output.item),player,plan.grid,inv)
    predicted=predicted or plan.output.item
    local capacity={};for i,s in ipairs(after_main) do capacity[i]=ItemStack(s) end
    if not add_to(capacity,predicted) then return nil,'inventory_full' end
    for _,item in ipairs(plan.left.items or {}) do if not add_to(capacity,item) then return nil,'replacement_no_space' end end
    for _,item in ipairs(plan.output.replacements or {}) do if not add_to(capacity,item) then return nil,'replacement_no_space' end end

    inv:set_list('main',after_main);inv:set_width('craft',plan.width);inv:set_size('craft',#plan.grid)
    inv:set_list('craft',plan.left.items)
    local ok,crafted=pcall(core.on_craft,ItemStack(plan.output.item),player,plan.grid,inv)
    if not ok then return nil,'craft_callback_unknown',true end
    crafted=crafted or plan.output.item
    local leftover=inv:add_item('main',crafted)
    for _,item in ipairs(inv:get_list('craft') or {}) do
        local rem=inv:add_item('main',item)
        if not rem:is_empty() then core.add_item(player:get_pos(),rem) end
    end
    for _,item in ipairs(plan.output.replacements or {}) do
        local rem=inv:add_item('main',item)
        if not rem:is_empty() then core.add_item(player:get_pos(),rem) end
    end
    if not leftover:is_empty() then core.add_item(player:get_pos(),leftover) end
    inv:set_size('craft',original_size);inv:set_width('craft',original_width);inv:set_list('craft',original_grid)
    return {item_id=ItemStack(crafted):get_name(),count=ItemStack(crafted):get_count(),recipe_id=id,consumed=#plan.selected}
end
end
