"""R01-R07 execution checks and start-of-round resource reservation."""
from collections import Counter
from .rules import (ACTORS, WEAPONS, MINERALS, UPGRADES, SUMMONS, TARGET_ITEMS,
                    pos, inside, distance, unit_distance, footprint, build_ring, reach)
from .combat import cone_ok


class Ledger:
    def __init__(self, world):
        self.gold = world.team.get("goldNum", 0)
        self.used = set()
        self.sites = set()
        self.new_weapons = 0
        self.summons = 0


def eligible(w, uid: int, cmd: dict, ledger: Ledger) -> str | None:
    """Returns execution-failure reason; only reserves resources on success.

    Task geometry: adjacent to either tile, see A01. Occupied actor destinations
    are resolved simultaneously by the judge, not rejected as static obstacles.
    """
    unit = w.units.get(uid)
    if unit is None:
        return "unit missing or dead"
    action = cmd["action"]
    targets = [pos(p) for p in cmd.get("targetPos", [])]
    if any(not inside(p) for p in targets):
        return "target outside map"
    p = pos(unit["pos"])
    target = targets[0] if targets else None
    name = cmd.get("name", "")
    bag = Counter(unit.get("backpack", []))
    capacity = unit.get("backPackCapability", 100 if unit["roleType"] == "worker" else 40)
    cost, new_weapon, actor_id = 0, 0, uid
    if action == "attack":
        actor_id = int(cmd["controllerId"])
        actor = w.units.get(actor_id)
        if unit["roleType"] not in WEAPONS or w.day:
            return "weapon attack only at night"
        if not actor or actor["roleType"] not in ACTORS or distance(pos(actor["pos"]), p) > 1:
            return "controller unavailable"
        count = 1 if unit["roleType"] == "railgun" else unit.get("level", 1)
        if len(targets) != count or unit.get("cooldown", 0) > 0:
            return "target count or cooldown"
        if any(distance(p, t) > reach(unit) or p == t for t in targets):
            return "target outside official range"
        if unit["roleType"] == "gatling" and not cone_ok(p, targets):
            return "gatling cone exceeds 90 degrees"
    else:
        if unit["roleType"] not in ACTORS:
            return "not a controllable actor"
        if action == "move":
            if distance(p, target) != 1:
                return "move must be one cell"
            static = set(w.zones)
            for other in w.ours + w.enemies:
                if other["roleType"] not in ACTORS:
                    static.update(footprint(other))
            if target in static:
                return "static obstacle"
        elif action == "build":
            if unit["roleType"] != "worker" or not w.day or not w.base:
                return "only workers build by day with a living base"
            if name not in WEAPONS + ("wall",) or distance(p, target) != 1:
                return "invalid building or distance"
            if target not in build_ring(w.base, 2 if name == "wall" else 1):
                return "wrong construction zone"
            existing = next((u for u in w.ours if target in footprint(u)), None)
            if target in w.blocked and (not existing or existing["roleType"] != name and
                                       not (name in WEAPONS and existing["roleType"] in WEAPONS)):
                return "occupied construction site"
            if target in ledger.sites:
                return "site reserved"
            if name == "wall":
                if not bag["stone"]:
                    return "stone missing"
            else:
                cost = 25
                new_weapon = int(existing is None)
                if len(w.weapons) + ledger.new_weapons + new_weapon > 3:
                    return "three-weapon limit"
        elif action == "collect":
            if unit["roleType"] != "worker" or w.zones.get(target) not in MINERALS:
                return "worker and mineral required"
            if distance(p, target) != 1 or sum(bag.values()) >= capacity:
                return "collection distance or capacity"
        elif action in ("buy", "sell"):
            zone = "weaponShop" if action == "buy" else "vendor"
            if not any(k == zone and distance(p, cell) == 1 for cell, k in w.zones.items()):
                return "shop out of reach"
            num = cmd.get("num", 1)
            if action == "buy":
                if name not in w.shop or sum(bag.values()) + num > capacity:
                    return "unknown merchandise or capacity"
                cost = w.shop[name] * num
            elif name not in MINERALS or name not in w.vendor or bag[name] < num:
                return "mineral missing or not purchased by vendor"
        elif action == "remove":
            if unit["roleType"] != "worker" or distance(p, target) != 1:
                return "worker and adjacent wall required"
            if not any(u["roleType"] == "wall" and pos(u["pos"]) == target for u in w.ours):
                return "own wall missing"
        elif action in ("acceptTask", "submitAnswer"):
            if unit["roleType"] != "pioneer":
                return "only pioneer handles tasks"
            if action == "submitAnswer" and not w.phase:
                return "no active task"
            if action == "acceptTask":
                if w.phase:
                    return "task already active"
                valid = False
                for task in w.tasks:
                    anchor = pos(task["taskPosition"])
                    kind = w.zones.get(anchor)
                    cells = {c for c, k in w.zones.items() if k == kind} if kind else {anchor}
                    if kind and kind.startswith(w.side) and task.get("isValid") and any(
                        distance(p, c) == 1 for c in cells
                    ):
                        valid = True
                if not valid:
                    return "no available own task in reach"
        elif action == "summonTreasure":
            if unit["roleType"] != "pioneer" or distance(p, target) > 1:
                return "pioneer or treasure distance"
            if Counter(cmd["item"]) - bag:
                return "sacrifice items missing"
        elif action == "drop":
            if not bag[name]:
                return "item missing"
        elif action == "use":
            if not bag[name]:
                return "item missing"
            if name in UPGRADES or name == "WallFixer":
                building = next((u for u in w.ours if target in footprint(u)), None)
                if not building or unit_distance(p, building) != 1:
                    return "building out of reach"
                if name == "WallFixer":
                    if building["roleType"] != "wall":
                        return "not a wall"
                else:
                    kinds, level = UPGRADES[name]
                    if building["roleType"] not in kinds or building.get("level", 1) != level:
                        return "wrong voucher level or target"
            elif name in SUMMONS:
                if ledger.summons >= 10:
                    return "daily summons exhausted"
            elif name not in ("Medicine", "Bomb", "DizzyWeapon"):
                return "unknown usable item"
    if actor_id in ledger.used or uid in ledger.used:
        return "one action per controller and weapon"
    if cost > ledger.gold:
        return "start-of-round gold exhausted"
    ledger.gold -= cost
    ledger.used.update((actor_id, uid))
    ledger.new_weapons += new_weapon
    if action == "build":
        ledger.sites.add(target)
    if action == "use" and name in SUMMONS:
        ledger.summons += 1
    return None
