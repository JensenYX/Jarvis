from __future__ import annotations
import json
import time
from .focus import EXECUTORS
from .survival_intent import context


class Discoveries:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.reset()

    def reset(self):
        self.identity = None
        self.seen = set()
        self.pending = {}
        self.last_spoken = 0.0

    def observe(self, snapshot, previous):
        identity = tuple(
            (
                snapshot.get(k)
                for k in ("world_id", "game_instance_id", "player_life_id")
            )
        )
        if identity != self.identity:
            self.reset()
            self.identity = identity
        if snapshot.get("phase") != "playing":
            return
        now = self.clock()
        game = context(snapshot)
        old = (
            context(previous)["inventory"]
            if previous and previous.get("world_id") == snapshot.get("world_id")
            else {}
        )
        candidates = []
        for item, count in game["inventory"].items():
            if previous and count > old.get(item, 0):
                candidates.append(
                    {
                        "kind": "acquired",
                        "item_id": item,
                        "gained": count - old.get(item, 0),
                        "count": count,
                    }
                )
        for row in game["drops"]:
            candidates.append({"kind": "drop", **row})
        for row in game["nodes"]:
            candidates.append(
                {
                    "kind": "visible",
                    "item_id": row["item_id"],
                    "name": row["name"],
                    "position": row["position"],
                }
            )
        for row in game["entities"]:
            candidates.append({**row, "entity_kind": row["kind"], "kind": "creature"})
        for row in candidates:
            key = (
                row["kind"],
                row.get("item_id", row.get("kind"))
                if row["kind"] != "creature"
                else row.get("target_id"),
            )
            if key in self.seen:
                continue
            self.pending[key] = {**row, "observed_at": now}
        self.pending = {
            k: r
            for k, r in self.pending.items()
            if now - r["observed_at"] < 30
            and (
                r["kind"] != "drop"
                or any(
                    (
                        d["item_id"] == r["item_id"]
                        and sum(
                            (
                                (d["position"][a] - r["position"][a]) ** 2
                                for a in ("x", "y", "z")
                            )
                        )
                        < 4
                        for d in game["drops"]
                    )
                )
            )
        }

    def take(self):
        if self.clock() - self.last_spoken < 60 or not self.pending:
            return None
        rows = sorted(
            self.pending.items(),
            key=lambda kv: {"acquired": 0, "drop": 1, "creature": 2, "visible": 3}[
                kv[1]["kind"]
            ],
        )[:10]
        for key, _ in rows:
            self.seen.add(key)
            self.pending.pop(key, None)
        self.last_spoken = self.clock()
        return [r for _, r in rows]

    def relevant(self, evidence, snapshot):
        if (
            not snapshot
            or tuple(
                (
                    snapshot.get(k)
                    for k in ("world_id", "game_instance_id", "player_life_id")
                )
            )
            != self.identity
        ):
            return False
        game = context(snapshot)
        items = set(game["inventory"]) | {
            r["item_id"] for r in game["nodes"] + game["drops"]
        }
        return any(
            (
                r.get("item_id") in items
                or r.get("target_id") in {e.get("target_id") for e in game["entities"]}
                for r in evidence
            )
        )

    def current_evidence(self, evidence, snapshot):
        game = context(snapshot)
        items = set(game["inventory"])
        rows = []
        for r in evidence:
            if r["kind"] == "acquired" and r["item_id"] in items:
                rows.append({**r, "count": game["inventory"][r["item_id"]]})
            elif r["kind"] == "visible" and any(
                (
                    n["item_id"] == r["item_id"] and n["position"] == r["position"]
                    for n in game["nodes"]
                )
            ):
                rows.append(r)
            elif r["kind"] == "drop" and any(
                (n["item_id"] == r["item_id"] for n in game["drops"])
            ):
                rows.append(r)
            elif r["kind"] == "creature" and any(
                (n["target_id"] == r["target_id"] for n in game["entities"])
            ):
                rows.append(r)
        return rows


def available_recipes(mode, snapshot):
    survival = snapshot["view"].get("survival", {})
    game = context(snapshot)
    inventory = game["inventory"]
    facts = {r["item_id"]: r for r in survival.get("item_facts", [])}
    catalog = dict(mode.bridge.recipe_catalog)
    catalog.update({r["recipe_id"]: r for r in survival.get("recipes", [])})
    result = {}
    for r in catalog.values():
        remaining = dict(inventory)
        used = {}
        missing = {}
        ingredients = r.get("ingredients", [])
        if isinstance(ingredients, dict):
            ingredients = ingredients.values()
        for ingredient in ingredients:
            if not ingredient:
                continue

            def matches(item):
                if not ingredient.startswith("group:"):
                    return item == ingredient
                groups = facts.get(item, {}).get("groups") or {}
                return all((groups.get(g, 0) > 0 for g in ingredient[6:].split(",")))

            item = next((i for i, n in remaining.items() if n > 0 and matches(i)), None)
            if item:
                remaining[item] -= 1
                used[item] = used.get(item, 0) + 1
            else:
                missing[ingredient] = missing.get(ingredient, 0) + 1
        if used and (not missing):
            result[r["recipe_id"]] = {
                k: r[k]
                for k in ("recipe_id", "item_id", "name", "name_zh", "count", "grid")
                if k in r
            }
            result[r["recipe_id"]].update(
                consumes=used,
                requires_workbench=r.get("grid", 2) > 2 and (not game["workbench"]),
            )
    return result


async def respond(mode, request, snapshot, evidence=None, *, advice=False):
    game = context(snapshot)
    survival = snapshot["view"].get("survival", {})
    known = (
        dict(getattr(mode.operations, "native_item_memory", {}))
        if mode.operations
        else {}
    )
    for item, row in (
        getattr(mode.operations, "item_knowledge", {}) if mode.operations else {}
    ).items():
        known[item] = {**known.get(item, {}), **row}
    known.update({r["item_id"]: r for r in survival.get("item_help", [])})
    known.update(
        {
            r["item_id"]: {
                **r,
                **{
                    k: v
                    for k, v in known.get(r["item_id"], {}).items()
                    if k in {"description", "usage"} and v
                },
            }
            for r in survival.get("item_facts", [])
        }
    )
    recipes = available_recipes(mode, snapshot)
    records, current = mode.focus.catalog(mode)
    if advice:
        suggested = await suggest_from_facts(
            mode, request, snapshot, known, recipes, records.get(current)
        )
        if suggested:
            return suggested
    choices = {}
    for i, r in enumerate(evidence or []):
        choices["E" + str(i)] = {"type": "observation", "fact": r}
    for item, r in known.items():
        choices[item] = {"type": "documentation", "name": r.get("name", item)}
    for key, r in recipes.items():
        choices["R" + key] = {"type": "recipe", **r}
    from .judgment import bounded_choice

    if not evidence and (not advice) and recipes:
        choice = await bounded_choice(
            mode.r.controller,
            "voxel_recipe_explanation",
            {
                "question": request,
                "last_explanation": getattr(mode.focus, "discussion", None),
                "recent_dialogue": mode.r.history[-3:],
            },
            "Does the current user ask HOW to craft or the ingredients of one of these products? Resolve a short 怎么合成 from the actual last explanation. Choose its recipe only for a recipe/how-to question, including an omitted product. Purpose, why, next activity or an execution request are NONE. Do not offer unrelated products.",
            {
                "NONE": "Not a recipe/how-to question or no matching recipe",
                **{
                    k: {
                        "product": r.get("name_zh") or r.get("name", r["item_id"]),
                        "ingredients": r["consumes"],
                        "count": r["count"],
                    }
                    for k, r in recipes.items()
                },
            },
            deadline_ms=1600,
        )
        key = choice["action"]["choice"]
        if key in recipes and choice["action"].get("confidence", 0) >= 0.7:
            r = recipes[key]
            catalog = mode.bridge.recipe_catalog
            native_names = {
                v["item_id"]: v.get("name_zh")
                for v in (getattr(mode.bridge, "cached_catalog", None) or {}).get(
                    "recipes", []
                )
                if v.get("name_zh")
            }
            names = {
                v["item_id"]: native_names.get(v["item_id"])
                or v.get("name_zh")
                or v.get("name", v["item_id"]).split("\n")[0]
                for v in catalog.values()
            }
            product = (
                native_names.get(r["item_id"])
                or r.get("name_zh")
                or r.get("name", r["item_id"]).split("\n")[0]
            )
            ingredients = "、".join(
                (
                    f"{n}个{names.get(i, known.get(i, {}).get('name', i)).split(chr(10))[0]}"
                    for i, n in r["consumes"].items()
                )
            )
            grid = (
                "按 E 打开背包合成格"
                if r.get("grid", 2) <= 2
                else "右键工作台，打开3×3合成格"
            )
            return {
                "text": f"{grid}，用{ingredients}按配方摆放，取出{r['count']}个{product}。",
                "offers": [
                    {"request": f"合成{r['count']}个{product}", "executor": "CRAFT"}
                ],
            }
    selected = await bounded_choice(
        mode.r.controller,
        "voxel_companion_topic",
        {
            "current_question": request,
            "new_discoveries": evidence,
            "inventory": game["inventory"],
            "current_goal": records.get(current),
            "recent_dialogue": mode.r.history[-4:],
            "preferences": mode.memory.preferences,
        },
        "Select the fact/recipe MOST useful for answering the current question or explaining one new discovery to a beginner. A found drop should be named, its use explained, and pickup suggested while still visible. For broad what-next/where questions choose CONTEXT if appropriate. For proactive speech choose SILENT if repetitive, uninteresting or unsupported. Do not divert a specific question to an old task.",
        {
            "SILENT": "No useful new proactive message",
            "CONTEXT": "Use the current game context",
            **choices,
        },
        deadline_ms=2300,
    )
    key = selected["action"]["choice"]
    if key == "SILENT" and evidence:
        return {"text": "", "offers": []}
    doc = known.get(key)
    if not doc and key in choices and (choices[key]["type"] == "observation"):
        doc = known.get(choices[key]["fact"].get("item_id"))
    if not advice and (not evidence) and doc and doc.get("description_zh"):
        local_text = doc["description_zh"]
        if any(("一" <= c <= "鿿" for c in local_text)):
            direct = await mode.r.controller.judge_typed(
                "voxel_localized_answer_fit",
                {"question": request, "native_text": local_text},
                {
                    "direct": {
                        "type": "noul",
                        "instructions": "Does this exact native documentation text directly answer the current question? If asking what food can be made, a sentence explicitly saying this ingredient can make cookies answers it. A mere description of growth/harvest without the requested product does not. No speculative steps.",
                    }
                },
                deadline_ms=1400,
            )
            if direct["direct"]["noul"] >= 0.8:
                return {"text": local_text, "offers": []}
    if evidence:
        chosen = choices.get(key, {})
        if chosen.get("type") == "observation":
            evidence = [chosen["fact"]]
        elif key in known:
            evidence = [r for r in evidence if r.get("item_id") == key] or evidence[:1]
    selected_recipe = recipes.get(key[1:]) if key.startswith("R") else None
    if not advice and doc and doc.get("possible_drops"):
        from .rules import render_item

        related = [known[i] for i in doc["possible_drops"] if i in known]
        if related:
            localized = {
                r["item_id"]: r["description_zh"]
                for r in related
                if r.get("description_zh")
                and any(("一" <= c <= "鿿" for c in r["description_zh"]))
            }
            if localized:
                direct = await mode.r.controller.judge_typed(
                    "voxel_drop_uses_answer",
                    {
                        "question": request,
                        "observed_source": doc.get("name"),
                        "native_drop_descriptions": localized,
                    },
                    {
                        "answers": {
                            "type": "noul",
                            "instructions": "Do these exact registered descriptions of the harvested drops answer what the source can be used for? A pod yields beans; documentation that beans make cookies answers what food the pod can provide. A question for the recipe amounts needs actual recipe data and is NO if those amounts are absent.",
                        }
                    },
                    deadline_ms=1500,
                )
                if direct["answers"]["noul"] >= 0.55:
                    return {"text": " ".join(localized.values()), "offers": []}
            combined = {"observed_source": doc, "registered_drop_uses": related}
            try:
                text = await render_item(mode.reasoning, request, combined)
                return {"text": text, "offers": []}
            except Exception:
                pass
    if not advice and doc and (not evidence):
        grounded_sections = {
            k: doc[k]
            for k in ("description", "usage")
            if doc.get(k) and any(("一" <= c <= "鿿" for c in doc[k]))
        }
        if grounded_sections:
            fit = await mode.r.controller.judge_typed(
                "voxel_direct_item_answer",
                {
                    "current_question": request,
                    "item_name": doc.get("name"),
                    "current_task": records.get(current),
                    "recent_dialogue": mode.r.history[-3:],
                },
                {
                    "section": {
                        "type": "choice",
                        "instructions": "Can a supplied native documentation section directly answer this current mechanics/purpose question? Select usage for how to use, description for purpose. Select NONE if the player asks for a recipe/product made from its drops and this text only describes the source/drop itself, or next-step advice, crafting recommendation, live quantity, location or an operation. Short pronouns may refer to current_task/recent dialogue.",
                        "criteria": {
                            "NONE": "Needs live advice or other context",
                            **grounded_sections,
                        },
                    }
                },
                deadline_ms=1600,
            )
            section = fit["section"]["choice"]
            if section in grounded_sections and fit["section"]["confidence"] >= 0.65:
                text = (
                    "。".join(grounded_sections[section].split("。")[:2]).rstrip("。")
                    + "。"
                )
                return {"text": text, "offers": []}
    from .rules import rules_for

    data = {
        "question": request,
        "proactive": bool(evidence),
        "advice": advice,
        "discoveries": evidence,
        "item_names": {i: r.get("name", i).split("\n")[0] for i, r in known.items()},
        "inventory": game["inventory"],
        "player": game["player"],
        "visible_drops": game["drops"],
        "visible_nodes": game["nodes"][:12],
        "visible_creatures": game["entities"],
        "navigation": game["navigation"][:4],
        "documentation": doc,
        "selected_topic": choices.get(key),
        "related_drop_documentation": [
            {
                k: v
                for k, v in known.get(i, {}).items()
                if k in {"name", "description", "usage", "edible"}
            }
            for i in (doc or {}).get("possible_drops") or []
        ],
        "actual_recipes": [selected_recipe]
        if selected_recipe
        else list(recipes.values())[:12],
        "item_facts": [
            {
                k: r[k]
                for k in (
                    "item_id",
                    "name",
                    "edible",
                    "food_risk",
                    "possible_drops",
                    "plant",
                    "description",
                    "usage",
                )
                if k in r
            }
            for r in known.values()
            if r["item_id"] in game["inventory"]
        ][:16],
        "current_goal": records.get(current),
        "recent_dialogue": mode.r.history[-6:],
        "task_history": [r for r in records.values() if r.get("historical")],
        "preferences": mode.memory.preferences,
        "prior_teaching": [r["text"] for r in mode.memory.discoveries[-3:]],
        "versioned_mechanics": {
            k: v
            for k, v in rules_for(snapshot).items()
            if k in {"crafting", "workbench", "materials"}
        },
    }
    messages = [
        {
            "role": "system",
            "content": "Be a helpful Chinese game companion for a beginner. Return JSON {text:string,offers:[{request:string,executor:string}]}. Answer the CURRENT question directly in 1-2 complete short sentences, at most 70 Chinese characters. Give one concrete useful next step and why; avoid redundant confirmations. For proactive messages explain ONE selected discovery. Never recite coordinates/internal IDs. Read item names: wood PLANKS and tree LOGS are different. inventory is the CURRENT carried total. kind=drop is still on the ground; kind=acquired is ALREADY included in inventory. Never add either to inventory. Use live facts, not old dialogue as current inventory/terrain. Explain uses/recipes only from native documentation, versioned_mechanics and actual_recipes; a 2x2 recipe needs no workbench. Unknown places stay unknown. Do not claim actions completed. offers has at most ONE concrete action actually suggested in text, exact target and output count; no action suggestion means []. Executors are "
            + ",".join((k for k in EXECUTORS if k != "NONE"))
            + '. A later user "do that" may refer to this offer; do not execute now. Answer how/why directly. A beginner benefits from useful survival/building/exploration choices, not an internal documentation lookup tool just because it is craftable.',
        },
        {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
    ]
    for attempt in range(2):
        try:
            reply = await mode.reasoning.complete(
                messages,
                json_mode=True,
                max_tokens=450,
                reasoning_effort="none",
                latency_sensitive=True,
            )
        except Exception as error:
            mode.r.emit("voxel.companion.text_unavailable", reason=type(error).__name__)
            return await factual_fallback(mode, request, doc, selected_recipe, data)
        c = reply["choices"][0]
        try:
            value = json.loads(c["message"]["content"])
        except (ValueError, TypeError):
            return await factual_fallback(mode, request, doc, selected_recipe, data)
        if (
            c.get("finish_reason") != "stop"
            or not isinstance(value.get("text"), str)
            or (not value["text"].strip())
        ):
            raise ValueError("Incomplete companion response")
        offers = value.get("offers", [])
        if (
            not isinstance(offers, list)
            or len(offers) > 2
            or any(
                (
                    not isinstance(o, dict)
                    or o.get("executor") not in EXECUTORS
                    or o["executor"] == "NONE"
                    or (not isinstance(o.get("request"), str))
                    or (len(o["request"]) > 500)
                    for o in offers
                )
            )
        ):
            raise ValueError("Invalid companion offers")
        review = await mode.r.controller.judge_typed(
            "voxel_companion_grounding",
            {
                "question": request,
                "answer": value,
                "documentation": doc,
                "versioned_mechanics": data["versioned_mechanics"],
                "new_observations": evidence,
                "inventory": game["inventory"],
                "visible_nodes": data["visible_nodes"],
                "visible_drops": data["visible_drops"],
                "recipes": data["actual_recipes"],
            },
            {
                "grounded": {
                    "type": "noul",
                    "instructions": "Does the answer directly address the question or new observation without unsupported item uses/recipe claims or invented completed actions? General route exploration is allowed; claims about drops/products must follow facts. Inventory is the current total including all acquired events; adding gained again is wrong.",
                },
                "offers_spoken": {
                    "type": "noul",
                    "instructions": "Are all offered actions actually suggested in text with the same target and no extra scope? An empty offers list is YES. A usage explanation alone is not permission.",
                },
            },
            deadline_ms=2100,
        )
        if review["grounded"]["noul"] >= 0.6:
            if review["offers_spoken"]["noul"] < 0.7:
                value["offers"] = []
            return value
        mode.r.emit("voxel.companion.revision", draft=value, review=review)
        messages.append(
            {"role": "assistant", "content": json.dumps(value, ensure_ascii=False)}
        )
        messages.append(
            {
                "role": "user",
                "content": "Revise the answer to only supported facts. Current inventory already includes gains. Answer the question, avoid unproven directions/counts/uses. Use a listed exact recipe if suggesting crafting. Keep complete short Chinese sentences.",
            }
        )
    return await factual_fallback(mode, request, doc, selected_recipe, data)


async def suggest_from_facts(mode, request, snapshot, known, recipes, current):
    from .judgment import bounded_choice

    game = context(snapshot)
    catalog = (
        (mode.bridge.cached_catalog or {}).get("recipes", [])
        if hasattr(mode.bridge, "cached_catalog")
        else []
    )
    names = {
        r["item_id"]: r.get("name_zh") or r["name"].split("\n")[0] for r in catalog
    }

    def name(item):
        return names.get(item, known.get(item, {}).get("name", item).split("\n")[0])

    options = {}
    texts = {}
    offers = {}

    def add(key, facts, text, offer):
        options[key] = facts
        texts[key] = text
        offers[key] = [offer]

    for i, r in enumerate(recipes.values()):
        key = "R" + str(i)
        product = r.get("name_zh") or name(r["item_id"])
        ingredients = "、".join(
            (f"{count}个{name(item)}" for item, count in r["consumes"].items())
        )
        doc = known.get(r["item_id"], {}).get("description", "").split("。")[0]
        if len(doc) > 75:
            doc = ""
        add(
            key,
            {
                "activity": "craft",
                "product": product,
                "item_id": r["item_id"],
                "count": r["count"],
                "requires_workbench": r["requires_workbench"],
                "owned": game["inventory"].get(r["item_id"], 0),
                "purpose": doc,
                "ingredients": ingredients,
            },
            f"可以用{ingredients}做{r['count']}个{product}。"
            + (doc + "。" if doc else "")
            + ("需要先放下工作台。" if r["requires_workbench"] else ""),
            {"request": f"合成{r['count']}个{product}", "executor": "CRAFT"},
        )
    for i, r in enumerate(game["drops"]):
        add(
            "D" + str(i),
            {"activity": "pickup", "item": name(r["item_id"]), "count": r["count"]},
            f"附近地上有{r['count']}个{name(r['item_id'])}，可以先走近捡起来。",
            {"request": f"捡起附近的{name(r['item_id'])}", "executor": "AGENT"},
        )
    seen = set()
    for i, r in enumerate(game["nodes"]):
        if r["item_id"] in seen:
            continue
        seen.add(r["item_id"])
        f = known.get(r["item_id"], {})
        description = f.get("description", "").split("。")[0]
        if len(description) > 65:
            description = ""
        add(
            "N" + str(i),
            {
                "activity": "gather",
                "item": name(r["item_id"]),
                "currently_harvestable": r.get("diggable"),
                "purpose": description,
                "possible_drops": f.get("possible_drops"),
                "plant": f.get("plant"),
            },
            f"可以先收集附近的{name(r['item_id'])}。"
            + (description + "。" if description else ""),
            {"request": f"收集附近的{name(r['item_id'])}", "executor": "AGENT"},
        )
    if game["navigation"]:
        add(
            "EXPLORE",
            {"activity": "explore", "observed_routes": len(game["navigation"])},
            "可以沿眼前能走的地面探索一段，看看新地形和资源，再决定下一步。",
            {"request": "探索附近区域，发现有用的资源时告诉我", "executor": "AGENT"},
        )
    if not options:
        return None
    a = await bounded_choice(
        mode.r.controller,
        "voxel_next_activity",
        {
            "question": request,
            "current_goal": current,
            "question_kind": "The user asks for one useful NEXT activity, including after a previous crafting/collection step.",
            "hp": game["player"].get("hp"),
            "hunger": game["player"].get("hunger"),
            "inventory": {name(i): n for i, n in game["inventory"].items()},
            "prior_offers": [r["presented_text"] for r in mode.focus.offers[-2:]],
            "latest_explanation": getattr(mode.focus, "discussion", None),
            "recent_dialogue": mode.r.history[-3:],
            "preferences": mode.memory.preferences,
            "known_facilities": list(
                getattr(mode.operations, "facilities", {}).values()
            )[-4:],
        },
        "Choose ONE concrete useful NEXT activity answering the beginner current question. Follow current needs, preferences and previous work. A native recipe being possible does not make it useful: documentation/lookup tools rarely advance ordinary survival. Avoid another workbench when one is owned/placed. Consider harvesting, food, exploring and tool preparation. Do not treat immature crops as food. These are suggestions only. NONE if the question needs a factual explanation, comparison, or an unprovided activity.",
        {"NONE": "No directly answering activity among these facts", **options},
        deadline_ms=1800,
    )
    key = a["action"]["choice"]
    if key == "NONE":
        follow = await mode.r.controller.judge_typed(
            "voxel_next_activity_form",
            {"question": request},
            {
                "next": {
                    "type": "noul",
                    "instructions": "Is this a request for the next useful activity, including 合成完某物之后呢/then what after making it? An item definition question alone is NO.",
                }
            },
            deadline_ms=1200,
        )
        if follow["next"]["noul"] < 0.7:
            return None
        retry = await bounded_choice(
            mode.r.controller,
            "voxel_next_activity_pick",
            {
                "question": request,
                "inventory": {name(i): n for i, n in game["inventory"].items()},
                "known_facilities": list(
                    getattr(mode.operations, "facilities", {}).values()
                )[-3:],
            },
            "Recommend one concrete useful next activity after the step the user mentioned. These are actual available recipes/resources. Prefer useful tools or resource preparation over duplicate facilities and lookup tools. Select an option; several good choices are fine.",
            options,
            deadline_ms=1500,
        )
        key = retry["action"]["choice"]
    return {"text": texts[key], "offers": offers[key]}


async def factual_fallback(mode, request, doc, recipe, data):
    from .judgment import bounded_choice

    options = {}
    offers = {}
    for i, r in enumerate(data.get("discoveries") or []):
        if r["kind"] not in {"drop", "acquired"}:
            continue
        name = (doc or {}).get("name", r.get("item_id", "物品")).split("\n")[0]
        detail = (doc or {}).get("description", "")
        if r["kind"] == "drop":
            key = "FOUND" + str(i)
            options[key] = f"附近地上有{r['count']}个{name}，可以走近捡起来。" + detail
            offers[key] = [
                {"request": f"捡起附近地上的{r['count']}个{name}", "executor": "AGENT"}
            ]
        else:
            options["GAIN" + str(i)] = (
                f"刚拿到{name}，现在背包里有{r['count']}个。" + detail
            )
    sources = [doc] if doc else []
    sources += data.get("item_facts", [])
    for i, r in enumerate(sources):
        for field in ("description", "usage"):
            text = (r or {}).get(field, "")
            if text and any(("一" <= c <= "鿿" for c in text)):
                sentences = [
                    s.strip() for s in text.replace("\n", "。").split("。") if s.strip()
                ]
                options[f"D{i}_{field}"] = "。".join(sentences[:2]) + "。"
    options.update(data.get("versioned_mechanics", {}))
    for i, r in enumerate([recipe] if recipe else data.get("actual_recipes", [])):
        if not r:
            continue
        name = r["name"].split("\n")[0]
        ingredients = "、".join(
            (
                f"{n} 个 {data.get('item_names', {}).get(item, item.split(':')[-1])}"
                for item, n in r["consumes"].items()
            )
        )
        key = "R" + str(i)
        options[key] = f"可以先用{ingredients}合成 {r['count']} 个{name}。" + (
            "需要先放置工作台。" if r["requires_workbench"] else ""
        )
        offers[key] = [{"request": f"合成{r['count']}个{name}", "executor": "CRAFT"}]
    if data.get("advice"):
        suggestions = {k: v for k, v in options.items() if k.startswith(("R", "FOUND"))}
        if suggestions:
            options = suggestions
    a = await bounded_choice(
        mode.r.controller,
        "voxel_companion_native_answer",
        {"current_question": request, "new_discoveries": data.get("discoveries")},
        "Select the native documentation or exact recipe text that directly answers this question. A general what-next question can be answered by one useful available recipe or relevant inventory item use; choose that suggestion, not NONE merely because several options exist. Prefer item usage for how-to-use questions. NONE only if no supplied fact or proposed next action answers.",
        {"NONE": "No supplied fact answers the question", **options},
        deadline_ms=2300,
    )
    key = a["action"]["choice"]
    if key == "NONE":
        intent = await mode.r.controller.judge_typed(
            "voxel_fallback_question_type",
            {"current_question": request},
            {
                "kind": {
                    "type": "choice",
                    "instructions": "Does this question request a next activity recommendation or a specific factual explanation?",
                    "criteria": {
                        "NEXT": "What should I do next / useful activity suggestion",
                        "FACT": "Specific item/mechanic question",
                    },
                }
            },
            deadline_ms=1300,
        )
        suggestions = {
            k: v for k, v in options.items() if k.startswith(("R", "FOUND", "GAIN"))
        }
        if intent["kind"]["choice"] == "NEXT" and suggestions:
            chosen = await bounded_choice(
                mode.r.controller,
                "voxel_native_next_suggestion",
                {
                    "question": request,
                    "player": data.get("player"),
                    "inventory": data.get("inventory"),
                },
                "Choose one useful currently possible next activity for this beginner. Every option is a grounded available recipe or newly observed resource. Several useful choices are acceptable; choose one and explain using its supplied text.",
                suggestions,
                deadline_ms=1500,
            )
            key = chosen["action"]["choice"]
        else:
            raise ValueError("No grounded fallback answer")
    return {"text": options[key], "offers": offers.get(key, [])}
