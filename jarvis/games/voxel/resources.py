from .survival_intent import context


async def choose_resource_goal(controller, snapshot, task, project=None):
    game = context(snapshot)
    requirements = (task.get("craft_deficit") or {}).get("requirements", [])
    missing = {k: v for r in requirements for k, v in (r.get("missing") or {}).items()}
    if task.get("agent_goal") == "MATERIALS" and task.get("materials_needed"):
        facts = {
            r["item_id"]: r
            for r in snapshot["view"].get("survival", {}).get("item_facts", [])
        }
        options = {}
        for row in game["nodes"]:
            f = facts.get(row["item_id"], {})
            options[row["item_id"]] = {
                "name": row.get("name"),
                "groups": f.get("groups"),
                "possible_drops": f.get("possible_drops"),
            }
        if options:
            needed = task["materials_needed"]
            a = await controller.judge_typed(
                "voxel_blueprint_material_sources",
                {
                    "needed_materials": needed,
                    "inventory": game["inventory"],
                    "native_material_recipes": [
                        r
                        for r in snapshot["view"]
                        .get("construction", {})
                        .get("materials", [])
                        if r["item_id"] in needed
                    ],
                },
                {
                    "source": {
                        "type": "choice",
                        "instructions": "Which observed node is a source of currently missing blueprint material, directly via normal drops or its registered recipe? A dirt shelter needs dirt/grass-block drops, not logs. A wood-plank structure may need the matching tree. Choose only actual required materials or native ingredient sources, NONE if no relevant source is observed.",
                        "criteria": {
                            "NONE": "No useful source in these observations",
                            **options,
                        },
                    }
                },
                deadline_ms=1900,
            )
            item = a["source"]["choice"]
            if item in options:
                return {"wanted_resources": [item], "resource_group": "MODEL_SELECTED"}
            matching = {
                i: v
                for i, v in options.items()
                if any(
                    (
                        n in needed and game["inventory"].get(n, 0) < needed[n]
                        for n in v.get("possible_drops") or []
                    )
                )
            }
            if matching:
                retry = await controller.judge_typed(
                    "voxel_native_material_source",
                    {
                        "remaining_bill": {
                            i: max(0, n - game["inventory"].get(i, 0))
                            for i, n in needed.items()
                        }
                    },
                    {
                        "source": {
                            "type": "choice",
                            "instructions": "Choose an observed source for this missing material. The listed native drops contain a required material. This is selecting a resource type, not claiming its position is already reachable.",
                            "criteria": matching,
                        }
                    },
                    deadline_ms=1600,
                )
                return {
                    "wanted_resources": [retry["source"]["choice"]],
                    "resource_group": "MODEL_SELECTED",
                }
            return {
                "wanted_resources": [],
                "resource_group": "SEARCH",
                "retry_after_observation": True,
            }
    options = {
        "LOGS": "Tree logs for wood/planks/tool preparation",
        "STONE": "Stone/cobble with a usable pick",
    }
    if not missing:
        options["NONE"] = "No resource needed for current movement/survival goal"
    a = await controller.judge_typed(
        "voxel_agent_resource_goal",
        {
            "request": task["request"],
            "goal": task.get("agent_goal"),
            "missing_ingredients_now": missing,
            "craft_deficit": task.get("craft_deficit"),
            "current_plan_step": task.get("stage_goal"),
            "inventory": game["inventory"],
            "hotbar_tools": game["tools"],
            "project": {
                "title": project.title,
                "requirements": project.requirements,
                "blueprint": project.blueprint
                and {"materials": project.blueprint.get("materials")},
            }
            if project
            else None,
        },
        {
            "resource": {
                "type": "choice",
                "instructions": "Choose needed resource from CURRENT stage and native craft_deficit, considering inventory AND hotbar_tools. Missing wood/planks/sticks or a workbench needs LOGS for native processing. Missing cobble for a stone tool needs STONE when a pick is owned. Need stone with NO pick anywhere: LOGS for a pick first. NONE only for a non-resource goal or when inputs are already sufficient. Do not ignore native missing ingredients or an existing pick.",
                "criteria": options,
            }
        },
        deadline_ms=1800,
    )
    group = a["resource"]["choice"]
    items = {
        "LOGS": [
            "mcl_core:tree",
            "mcl_core:birchtree",
            "mcl_core:sprucetree",
            "mcl_core:jungletree",
            "mcl_core:acaciatree",
            "mcl_core:darktree",
        ],
        "STONE": [
            "mcl_core:stone",
            "mcl_core:cobble",
            "mcl_core:andesite",
            "mcl_core:granite",
            "mcl_core:diorite",
        ],
        "NONE": [],
    }[group]
    return {"wanted_resources": items, "resource_group": group}
