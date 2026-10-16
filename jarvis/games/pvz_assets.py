import re


def cfunction(source):
    source = source.replace("EBody.scrollLeft", "document.body.scrollLeft").replace(
        "EBody.scrollTop", "document.body.scrollTop"
    )
    source = source.replace(
        "EElement.scrollLeft", "document.documentElement.scrollLeft"
    ).replace("EElement.scrollTop", "document.documentElement.scrollTop")
    source = source.replace(
        "audio.muted = oS.Silence;",
        "audio.muted = oS.Silence;\n\t\t\taudio.volume = globalThis.companionGameVolume ?? 0.2;",
    )
    source = re.sub(
        "(?P<call>(?:audio|oAudio\\[src\\]|oAudio\\[PausedAudioArr\\[i\\]\\]|NewAudio\\(\\{ source: src, loop: !!loop \\}\\))\\.play\\(\\));",
        '\\g<call>.catch(function(error){ if(error.name!=="AbortError") console.warn("Game audio:",error.name); });',
        source,
    )
    source = source.replace(
        "var StartAdventure = function (level) {",
        'var StartAdventure = function (level) {\n\tif (level === undefined || (level && typeof level === "object")) level = $User.Visitor.Progress || 1;',
    )
    return source


def tutorial_level(source):
    needle = "i > 0 &&"
    if source.count(needle) != 1:
        raise ValueError("Pinned first-level tutorial changed")
    planted = "Object.values(oGd.$).some(p => p && p.R === 3 && p.C >= 1 && p.C <= oS.C && p.PKind === 1)"
    return source.replace(needle, "(i > 0 || " + planted + ") &&", 1)


def custom_audio(source):
    needle = "masterGain.gain.value = oS.Silence ? 0 : 1;"
    if source.count(needle) != 1:
        raise ValueError("Pinned audio mixer changed")
    return source.replace(
        needle,
        "masterGain.gain.value = oS.Silence ? 0 : (globalThis.companionGameVolume ?? 0.2);",
    )


ALIASES = {
    "game/images/interface/Almanac_Ground.png": "game/images/interface/Almanac_GroundDay.jpg",
    "game/images/interface/Shovel.png": "game/images/interface/Shovel/0.gif",
    "game/32LawnCleaner.png": "game/images/interface/LawnCleaner.png",
    "game/images/Zombies/FlagZombie/1.gif": "game/images/Zombies/FlagZombie/FlagZombie.gif",
}
