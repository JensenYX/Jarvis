import json

from jarvis.paths import model_root, resource


def catalog():
    return json.loads(resource("models.json").read_text())


def download(name):
    from huggingface_hub import snapshot_download

    entry = catalog()[name]
    directory = model_root() / name
    patterns = entry.get("allow_patterns")
    if entry.get("subdirectory"):
        patterns = [entry["subdirectory"] + "/*", "LICENSE*", "LEGAL*", "README*"]
    snapshot_download(
        entry["repo"],
        revision=entry["revision"],
        local_dir=directory,
        allow_patterns=patterns,
        max_workers=4,
    )
    target = (
        directory / entry["subdirectory"] if entry.get("subdirectory") else directory
    )
    (target / "jarvis-model.json").write_text(
        json.dumps({"repo": entry["repo"], "revision": entry["revision"]}, indent=2)
        + "\n"
    )
    print(f"{name}: {target}")
    return target
