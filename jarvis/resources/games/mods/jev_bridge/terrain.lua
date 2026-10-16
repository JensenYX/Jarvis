

return function(B)
local cache={};local survey={};local stamp=-10000;local old
local offsets={}
for dx=-6,6 do for dz=-6,6 do offsets[#offsets+1]={dx,dz} end end
table.sort(offsets,function(a,b)return a[1]*a[1]+a[2]*a[2]<b[1]*b[1]+b[2]*b[2] end)
function B.build_terrain(player)
    if not player then return {} end
    local at=player:get_pos();local now=B.now()
    if old and now-stamp<1200 and vector.distance(old,at)<.7 then return cache end
    old=at;stamp=now;cache={};survey={};local seen={};local eye=B.eye(player)
    local base=math.floor(at.y+.1)
    for _,delta in ipairs(offsets) do
        local dx,dz=delta[1],delta[2]
        local x=math.floor(at.x+.5)+dx;local z=math.floor(at.z+.5)+dz
        for _,height in ipairs({base,base+1,base-1,base+2,base-2,base-3}) do
            local target={x=x,y=height+.35,z=z}
            if vector.distance(eye,target)<=8 then
            for hit in core.raycast(eye,target,false,false) do
                if hit.type=='node' then
                    local p=hit.under;local key=core.pos_to_string(p)
                    if B.known_nodes then
                        local observed=core.get_node_or_nil(p)
                        if observed then B.known_nodes[key]={position=p,item_id=observed.name,name=B.name(observed.name),at=now} end
                    end
                    if not seen[key] and hit.above.y==p.y+1 and #survey<80 then
                        seen[key]=true
                        local n=core.get_node_or_nil(p);local def=n and core.registered_nodes[n.name]
                        if def and def.walkable and not def.on_rightclick and not core.is_protected(p,B.player_name) and (def.damage_per_second or 0)==0 then
                            local clear=0;local obstacle=nil
                            for h=1,4 do
                                local q=vector.offset(p,0,h,0);local visible=true
                                for h in core.raycast(eye,q,false,false) do
                                    if h.type=='node' then
                                        visible=false
                                        if h.under.x==q.x and h.under.y==q.y and h.under.z==q.z then
                                            local n=core.get_node_or_nil(h.under)
                                            if n then obstacle={position=h.under,item_id=n.name} end
                                        end
                                        break
                                    end
                                end
                                if not visible then break end
                                local air=core.get_node_or_nil(q);if not air or air.name~='air' then break end
                                clear=h
                            end
                            if clear>=2 then cache[#cache+1]={position=p,clearance=clear} end
                            survey[#survey+1]={position=p,item_id=n.name,clearance=clear,obstacle=obstacle}
                        end
                    end
                    break
                end
            end
            end
        end
    end
    return cache
end
function B.site_terrain(player)
    B.build_terrain(player)
    return survey
end
end
