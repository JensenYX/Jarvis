from __future__ import annotations
import hashlib
import re
from pathlib import Path
from uuid import uuid4

SUFFIXES = {".html", ".js", ".css", ".py", ".txt", ".md", ".json", ".svg"}


def artifact_path(root: Path, token: str, filename: str) -> Path:
    if not re.fullmatch("[a-f0-9]{32}", token) or not re.fullmatch(
        "[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", filename
    ):
        raise ValueError("Invalid artifact address")
    if Path(filename).suffix.lower() not in SUFFIXES or ".." in filename:
        raise ValueError("Unsupported artifact filename")
    path = root / token / filename
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Artifact symlinks are not allowed")
    return path


def write_artifact(root: Path, filename: str, content: str, title: str) -> dict:
    token = uuid4().hex
    path = artifact_path(root, token, filename)
    data = content.encode("utf-8")
    if not data or len(data) > 300000:
        raise ValueError("Artifact must contain 1 to 300000 bytes")
    path.parent.mkdir(parents=True, exist_ok=False)
    with path.open("xb") as output:
        output.write(data)
    return {
        "ok": True,
        "kind": "artifact",
        "title": title[:160],
        "filename": filename,
        "url": f"/artifacts/{token}/{filename}",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "previewable": path.suffix.lower() == ".html",
        "execution": "Created as a file; no server-side execution or automatic browser execution.",
    }


def read_artifact(root, url, allowed):
    if url not in allowed:
        raise ValueError(
            "This file is not a recorded artifact of the current user's task"
        )
    match = re.fullmatch("/artifacts/([a-f0-9]{32})/([A-Za-z0-9_.-]+)", url)
    if not match:
        raise ValueError("Invalid artifact URL")
    path = artifact_path(Path(root), *match.groups())
    data = path.read_bytes()
    if len(data) > 300000:
        raise ValueError("Artifact exceeds read limit")
    return {
        "ok": True,
        "url": url,
        "filename": path.name,
        "content": data.decode("utf-8"),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def edit_artifact(root, url, old, new, title, allowed):
    return edit_artifact_many(root, url, [{"old": old, "new": new}], title, allowed)


def edit_artifact_many(root, url, edits, title, allowed):
    original = read_artifact(root, url, allowed)
    content = original["content"]
    for edit in edits:
        old, new = (edit["old"], edit["new"])
        if not old or content.count(old) != 1:
            raise ValueError(
                "Every exact old span must occur once; no file was written"
            )
        content = content.replace(old, new, 1)
    result = write_artifact(Path(root), original["filename"], content, title)
    result["parent_url"] = url
    result["parent_sha256"] = original["sha256"]
    return result
