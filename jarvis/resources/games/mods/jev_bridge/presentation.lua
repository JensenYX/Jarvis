

return function(B)
local channel=core.mod_channel_join('jev_bridge:presentation');local sequence=0
local function transmit(kind,target,ms,progress)
    if not channel or not channel:is_writeable() then return end
    sequence=sequence+1;target=target or {x=0,y=0,z=0}
    channel:send_all(string.format('%d %s %.4f %.4f %.4f %d %.3f',sequence,kind,target.x,target.y,target.z,ms or 0,progress or 0))
end
function B.present(kind,target,ms,command)
    local player=B.player();if not player then return end
    B.presentation={kind=kind,target=target,expires=B.now()+(ms or 0),command=command,
        input_seq=B.input_seq,control_epoch=B.control_epoch,last=0,started=B.now(),duration=ms or 1}
    transmit(kind,target,math.min(ms or 0,800))
end
function B.facing_target(player,target,movement)
    if not player or not target then return false end
    local delta=vector.subtract(target,B.eye(player))
    local horizontal=math.sqrt(delta.x*delta.x+delta.z*delta.z)
    local yaw=math.atan2(-delta.x,delta.z)
    local pitch=movement and math.rad(8) or -math.atan2(delta.y,horizontal)
    local yaw_error=math.abs(math.atan2(math.sin(yaw-player:get_look_horizontal()),math.cos(yaw-player:get_look_horizontal())))
    return (horizontal<.15 or yaw_error<.14) and math.abs(pitch-player:get_look_vertical())<.18
end
function B.stop_presentation()
    if B.presentation then
        local p=B.presentation
        B.presentation_tail={expires=B.now()+350,input_seq=p.input_seq,control_epoch=p.control_epoch}
        transmit('STOP',nil,0);B.presentation=nil
    end
end
function B.presentation_change(key)
    local p=B.presentation;local old=B.previous
    if not old then return false end
    if not p or B.now()>p.expires then
        local tail=B.presentation_tail
        if not tail or B.now()>tail.expires or tail.input_seq~=B.input_seq or tail.control_epoch~=B.control_epoch then return false end
        return key.slot==old.slot and key.stack==old.stack and key.controls==old.controls and vector.distance(key.position,old.position)<.02
    end
    if p.input_seq~=B.input_seq or p.control_epoch~=B.control_epoch then return false end

    if key.slot~=old.slot or key.stack~=old.stack or key.controls~=old.controls then return false end
    return vector.distance(key.position,old.position)<.02
end
core.register_globalstep(function()
    local p=B.presentation
    if not p then return end
    if B.now()>p.expires or p.input_seq~=B.input_seq or p.control_epoch~=B.control_epoch or
        B.client_menu or not B.client_focused or B.now()-B.browser_seen>1200 then B.stop_presentation();return end
    if B.now()-p.last>180 then p.last=B.now();transmit(p.kind,p.target,math.min(700,p.expires-B.now()),math.min(.98,(B.now()-p.started)/p.duration)) end
end)
end
