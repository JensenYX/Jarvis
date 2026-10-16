return function(B)
local clear=core.settings:get_bool('jev_clear_weather',false)
local daylight=core.settings:get_bool('jev_always_day',false)
if not clear and not daylight then return end
core.register_on_mods_loaded(function()
    assert(mcl_weather and vl_tuning,'Clear weather requires VoxeLibre weather APIs')
    if clear then vl_tuning.setting('gamerule:doWeatherCycle'):set(false) end
    if daylight then
        vl_tuning.setting('gamerule:doDaylightCycle'):set(false)
        vl_tuning.setting('fixed_daylight_time'):set(.5)
    end

    core.after(0,function()
        if clear then assert(mcl_weather.change_weather('none',nil,'Companion clear-weather preference')) end
        if daylight then core.set_timeofday(.5) end
    end)
end)
end
