from __future__ import annotations
from pathlib import Path
import yaml


class SkillStore:
    def __init__(self, assets: Path):
        self.root = assets / "skills"
        self.manifests = {}
        self.bodies = {}
        for file in sorted(self.root.glob("*/manifest.yaml")):
            manifest = yaml.safe_load(file.read_text())
            if manifest["id"] != file.parent.name:
                raise ValueError("Skill ID must match its directory")
            self.manifests[manifest["id"]] = manifest
            self.bodies[manifest["id"]] = (file.parent / "SKILL.md").read_text()

    def cards(self, limit=5):
        return list(self.manifests.values())[:limit]

    def body(self, skill_id: str):
        if skill_id == "NONE":
            return None
        if skill_id not in self.manifests:
            raise ValueError("Unknown selected skill")
        return self.bodies[skill_id]
