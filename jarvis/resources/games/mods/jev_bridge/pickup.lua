

return function(B)
local pending={}
local function identity(stack)
    local s=ItemStack(stack);s:set_count(1);return s:to_string()
end
local function observe(stack,player)
    if not player or player:get_player_name()~=B.player_name then return end
    pending[#pending+1]={before=player:get_wielded_item():to_string(),item=identity(stack),
        count=stack:get_count(),slot=player:get_wield_index(),input_seq=B.input_seq,
        control_epoch=B.control_epoch,expires=B.now()+600}
    while #pending>12 do table.remove(pending,1) end
end
core.register_on_mods_loaded(function()
    if mcl_item_entity and mcl_item_entity.register_on_pickup then
        mcl_item_entity.register_on_pickup(observe)
    end
end)
function B.pickup_stack_change(old,key)
    if not old or key.slot~=old.slot or key.stack==old.stack then return false end
    local before=ItemStack(old.stack);local after=ItemStack(key.stack)
    if after:is_empty() or not before:is_empty() and identity(before)~=identity(after) then return false end
    local delta=after:get_count()-before:get_count();if delta<=0 then return false end
    local count=0;local matched={}
    for i,p in ipairs(pending) do
        if p.expires>=B.now() and p.slot==key.slot and p.input_seq==B.input_seq and
            p.control_epoch==B.control_epoch and p.item==identity(after) and
            (p.before==old.stack or not ItemStack(p.before):is_empty() and identity(p.before)==identity(after)) then
            count=count+p.count;matched[#matched+1]=i
        end
    end
    if delta>count then return false end
    for i=#matched,1,-1 do table.remove(pending,matched[i]) end
    B.event('item_picked_up',{item_id=after:get_name(),count=delta},'engine','native_pickup_and_inventory_delta')
    return true
end
end
