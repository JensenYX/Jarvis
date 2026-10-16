
return function(B)
local function node_at(p)return core.get_node_or_nil(p)end
local function liquid(n)
    local d=n and core.registered_nodes[n.name]
    return d and d.liquidtype and d.liquidtype~='none'
end
local function open(p)
    local n=node_at(p);local d=n and core.registered_nodes[n.name]
    return d and not d.walkable and (d.damage_per_second or 0)==0
end
local function floor_ok(p)
    local n=node_at(p);local d=n and core.registered_nodes[n.name]
    return d and d.walkable and (d.damage_per_second or 0)==0 and not core.is_protected(p,B.player_name)
end
function B.in_water(player)
    local p=player:get_pos()
    return liquid(node_at(B.eye(player))) or liquid(node_at(vector.offset(p,0,.2,0))) or false
end
function B.water_contact(player,at)
    local p=at or player:get_pos()
    return liquid(node_at(vector.offset(p,0,.2,0))) or liquid(node_at(vector.offset(p,0,-.6,0))) or liquid(node_at(vector.offset(p,0,-1.2,0))) or B.in_water(player) or false
end
function B.ground_status(player)
    local p=player:get_pos();local floor={x=math.floor(p.x+.5),y=math.floor(p.y-.05+.5),z=math.floor(p.z+.5)}
    local grounded=floor_ok(floor) and math.abs(p.y-(floor.y+.5))<.12 and math.abs(player:get_velocity().y)<.3 or false
    return grounded,grounded and not B.in_water(player)
end
function B.passive_water_change(player,key,old)
    return old and key.slot==old.slot and key.stack==old.stack and key.controls==old.controls and
        math.abs(key.yaw-old.yaw)<.001 and math.abs(key.pitch-old.pitch)<.001 and
        math.abs(key.position.x-old.position.x)<.02 and math.abs(key.position.z-old.position.z)<.02 and
        math.abs(key.position.y-old.position.y)<1.5 and
        (B.water_contact(player) or B.water_contact(player,old.position))
end
local function sight(player,p,range)
    local eye=B.eye(player)
    if vector.distance(eye,p)>(range or 5) then return false end
    for h in core.raycast(eye,p,false,false) do
        if h.type=='node' then return vector.distance(h.under,p)<.8 end
    end
    return node_at(p)~=nil
end

function B.shore_observe(player)
    local rows,seen={},{};local eye=B.eye(player)
    for i=0,15 do
        local angle=i*math.pi/8
        for _,dy in ipairs({-.5,-.15,.15}) do
            local direction=vector.normalize({x=math.sin(angle),y=dy,z=math.cos(angle)})
            for hit in core.raycast(eye,vector.add(eye,vector.multiply(direction,8)),false,false) do
                if hit.type=='node' then
                    local floor=hit.under;local key=core.pos_to_string(floor)

                    if not seen[key] and floor_ok(floor) and sight(player,vector.offset(floor,0,.49,0),8) and
                        open(vector.offset(floor,0,1,0)) and open(vector.offset(floor,0,2,0)) and
                        not liquid(node_at(vector.offset(floor,0,1,0))) then
                        rows[#rows+1]={position=vector.offset(floor,0,.5,0)};seen[key]=true
                    end
                    break
                end
            end
        end
    end
    table.sort(rows,function(a,b)return vector.distance(a.position,eye)<vector.distance(b.position,eye) end)
    while #rows>12 do table.remove(rows) end
    return rows
end
function B.travel_step(player,dx,dz)
    if math.abs(dx)+math.abs(dz)~=1 then return nil end
    local at=player:get_pos();local x=math.floor(at.x+.5)+dx;local z=math.floor(at.z+.5)+dz
    local base=math.floor(at.y+.1)

    for _,dy in ipairs({0,1,-1,-2,-3}) do
        local y=base+dy;local floor={x=x,y=y,z=z}
        if floor_ok(floor) and open({x=x,y=y+1,z=z}) and open({x=x,y=y+2,z=z}) and sight(player,vector.offset(floor,0,.49,0)) then
            local clear=true

            for i=1,4 do
                local px=at.x+(x-at.x)*i/4;local pz=at.z+(z-at.z)*i/4
                local fy=math.max(base,y)
                if not open({x=px,y=fy+1,z=pz}) or not open({x=px,y=fy+2,z=pz}) then clear=false;break end
            end
            if clear and dy< -1 then
                for h=y+1,base+2 do if not open({x=x,y=h,z=z}) then clear=false;break end end
            end
            if clear then
                local margin=dy==0 and not B.water_contact(player)
                if margin then
                    for _,d in ipairs({{1,0},{-1,0},{0,1},{0,-1}}) do
                        local q={x=x+d[1],y=y,z=z+d[2]}
                        if not sight(player,vector.offset(q,0,.35,0),5) or not floor_ok(q) then margin=false;break end
                    end
                end
                return {kind=(dy>0 or B.water_contact(player) and not liquid(node_at({x=x,y=y+1,z=z}))) and 'JUMP_MOVE' or 'MOVE',target={x=x,y=y+.5,z=z},rise=dy,dx=dx,dz=dz,
                    safe_walk_margin=margin,reaches_solid_land=not liquid(node_at({x=x,y=y+1,z=z}))}
            end
        end
    end
    if B.water_contact(player) and open({x=x,y=at.y+.2,z=z}) and open({x=x,y=at.y+1.2,z=z}) and sight(player,{x=x,y=at.y+1,z=z}) then
        return {kind='SWIM',target={x=x,y=at.y,z=z},dx=dx,dz=dz,duration_ms=700}
    end
end
function B.travel_observe(player)
    local options={};local at=player:get_pos();local eye=B.eye(player)
    for _,delta in ipairs({{1,0},{-1,0},{0,1},{0,-1}}) do
        local move=B.travel_step(player,delta[1],delta[2]);if move then options[#options+1]=move end
    end
    if B.water_contact(player) then
        local clear=true
        for i=1,4 do if not open(vector.offset(eye,0,i*.4,0)) then clear=false;break end end
        if clear then options[#options+1]={kind='SWIM_UP',duration_ms=6500,target=vector.offset(at,0,2,0)} end
    elseif open(vector.offset(eye,0,1.1,0)) then
        options[#options+1]={kind='JUMP',duration_ms=350,target=vector.offset(at,0,1,0)}
    end
    return options
end

function B.navigation_observe(player)
    local at=player:get_pos();local base=math.floor(at.y+.1);local routes={};local seen={}
    local function observed(p)return B.visible_cell(player,p) end
    for _,delta in ipairs({{1,0},{-1,0},{0,1},{0,-1}}) do
        local dx,dz=delta[1],delta[2];local x=math.floor(at.x+.5)+dx;local z=math.floor(at.z+.5)+dz
        for _,rise in ipairs({0,1}) do
            local y=base+rise;local floor={x=x,y=y,z=z};local blockers={};local unknown=false;local protected=false
            local support=observed(floor) and floor_ok(floor)
            local step=nil
            if not support and observed(floor) and open(floor) and not liquid(node_at(floor)) and B.can_place_cell then

                if B.can_place_cell(player,{x=x,y=y,z=z,item_id='mcl_core:dirt'}) then step=floor end
            end
            if not support and not step and rise==1 and observed(floor) and open(floor) and not liquid(node_at(floor)) then
                local below=vector.offset(floor,0,-1,0)
                local ahead=vector.offset(floor,dx,1,dz)

                if observed(below) and floor_ok(below) and observed(ahead) and floor_ok(ahead) then step=floor end
            end
            do
                local cells={}
                for i=1,4 do
                    local px=math.floor(at.x+(x-at.x)*i/4+.5);local pz=math.floor(at.z+(z-at.z)*i/4+.5)
                    for h=1,2 do cells[core.pos_to_string({x=px,y=y+h,z=pz})]={x=px,y=y+h,z=pz} end
                end
                for _,p in pairs(cells) do
                    if not observed(p) then unknown=true
                    elseif not open(p) then
                        local n=node_at(p)
                        blockers[#blockers+1]={position=p,item_id=n.name}
                        if core.is_protected(p,B.player_name) then protected=true end
                    end
                end
                local target={x=x,y=y+.5,z=z};local id=core.pos_to_string(target)

                if (support or step or #blockers>0) and not seen[id] then
                    seen[id]=true;routes[#routes+1]={id=id,target=target,rise=rise,dx=dx,dz=dz,blockers=blockers,place_step=step,
                        unknown_clearance=unknown,unknown_support=not support and not step,protected=protected,supported=support and true or false}
                end
            end
        end
    end
    return routes
end
function B.travel_allowed_change(key,task)
    local p=task.pending
    if not p or not ({MOVE=true,JUMP_MOVE=true,SWIM=true,SWIM_UP=true,JUMP=true})[p.kind] or B.now()>p.expires then return false end
    local view=B.presentation and B.presentation.command==p.command_id
    return key.slot==p.state.slot and (view or math.abs(key.yaw-p.state.yaw)<.08 and math.abs(key.pitch-p.state.pitch)<.08)
end
end
