"""Strategy v1: rolling economy, deadline return, coordinated artillery, task agent."""
from collections import Counter
from copy import deepcopy
from itertools import permutations
import hashlib
import json
import time

from .audit import Ledger, eligible
from .combat import choose_targets, damage
from .protocol import empty_response, schema_errors
from .rules import (ACTORS, WEAPONS, MINERALS, UPGRADES, pos, xy, distance, footprint,
                    build_ring, neighbours, command, max_health, inside)
from .tasks import TaskMemory, structured_answer
from .world import World


class Agent:
    def __init__(self, loadout=("rocket", "railgun", "gatling")):
        if len(loadout) != 3 or any(k not in WEAPONS for k in loadout):
            raise ValueError("loadout must contain three official weapons")
        self.loadout = tuple(loadout)
        self.reset()

    def reset(self):
        self.memory = TaskMemory()
        self.identity = None
        self.last_round = 0
        self.cache_key = None
        self.cache = None
        self.fired = {}
        self.diagnostics = []
        self.failed_mines = {}
        self.previous_commands = {}

    def decide(self, payload):
        deadline = time.perf_counter() + 2.5
        key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if self.cache_key == key:
            return deepcopy(self.cache)
        identity = (payload["teamOur"].get("teamId"), payload["teamOur"]["type"])
        round_no = int(payload["roundNo"])
        if identity != self.identity or round_no < self.last_round:
            self.reset()
        self.identity, self.last_round = identity, round_no
        # Never mutate official input when inferring omitted cooldown.
        w = World(deepcopy(payload))
        for tower in w.weapons:
            if tower["roleType"] == "rocket" and "cooldown" not in tower:
                tower["cooldown"] = max(0, self.fired.get(tower["id"], -10) + 4 - w.round)
        self.diagnostics = []
        for uid, success in payload.get("lastRoundRoleActionResults", {}).items():
            previous = self.previous_commands.get(str(uid), {})
            if not success and previous.get("action") == "collect":
                self.failed_mines[pos(previous["targetPos"][0])] = w.round + 4
        self.w, self.ledger = w, Ledger(w)
        self.response = empty_response()
        self.reserved, self.jobs, self.purchase_names = set(), set(), set()
        model_reply = self.memory.update(w)
        assignments = self.assign_towers(w)
        allocated = {}
        if not w.day:
            for actor in w.actors:
                if self.heal(actor):
                    continue
                tower = assignments.get(actor["id"])
                if not tower:
                    self.return_home(actor)
                elif distance(pos(actor["pos"]), pos(tower["pos"])) <= 1:
                    if tower.get("cooldown", 0) == 0:
                        targets = choose_targets(tower, w.robots, w.base, w.side, allocated, deadline)
                        if targets and self.emit(tower, command("attack", targets, controllerId=str(actor["id"]))):
                            for rid, value in damage(tower, targets, w.robots).items():
                                allocated[rid] = allocated.get(rid, 0) + value
                    else:
                        self.use_upgrade(actor)
                else:
                    self.walk(actor, {pos(tower["pos"])})
        else:
            workers = [u for u in w.actors if u["roleType"] == "worker"]
            for actor in workers:
                if self.heal(actor) or self.use_upgrade(actor):
                    continue
                if self.construct(actor):
                    continue
                tower = assignments.get(actor["id"])
                home = self.home_route(actor, tower)
                if w.left <= (len(home) if home is not None else 20) + 5:
                    self.return_home(actor, tower)
                    continue
                if self.economy(actor):
                    continue
                self.return_home(actor, tower)
            for actor in (u for u in w.actors if u["roleType"] == "pioneer"):
                if self.heal(actor):
                    continue
                tower = assignments.get(actor["id"])
                home = self.home_route(actor, tower)
                if w.left <= (len(home) if home is not None else 20) + 5:
                    self.return_home(actor, tower)
                elif w.phase:
                    self.solve_task(actor, model_reply)
                elif not self.treasure(actor) and not self.accept_task(actor):
                    self.return_home(actor, tower)
        if not w.phase and not self.response["prompt"]:
            self.response["prompt"] = self.memory.news_prompt(w)
        if schema_errors(self.response):
            raise ValueError("internal response schema failure")
        self.previous_commands = deepcopy(self.response["roleCommandMap"])
        self.cache_key, self.cache = key, deepcopy(self.response)
        return self.response

    def emit(self, unit, cmd):
        issue = eligible(self.w, unit["id"], cmd, self.ledger)
        if issue:
            self.diagnostics.append({"id": unit["id"], "reason": issue})
            return False
        self.response["roleCommandMap"][str(unit["id"])] = cmd
        if cmd["action"] in ("move", "build"):
            self.reserved.add(pos(cmd["targetPos"][0]))
        if cmd["action"] == "attack" and unit["roleType"] == "rocket":
            self.fired[unit["id"]] = self.w.round
        return True

    def walk(self, actor, cells):
        route = self.w.adjacent_route(actor, cells, self.reserved)
        if route:
            return self.emit(actor, command("move", [route[0]]))
        return route == []

    def assign_towers(self, w):
        actors, towers = w.actors, w.weapons
        n = min(len(actors), len(towers))
        if not n:
            return {}
        costs = {}
        for actor in actors:
            for tower in towers:
                route = w.adjacent_route(actor, {pos(tower["pos"])})
                costs[actor["id"], tower["id"]] = len(route) if route is not None else 10000
        best, result = float("inf"), {}
        for group in permutations(actors, n):
            for gun_group in permutations(towers, n):
                cost = sum(costs[a["id"], t["id"]] for a, t in zip(group, gun_group))
                if cost < best:
                    best = cost
                    result = {a["id"]: t for a, t in zip(group, gun_group)}
        return result

    def home_route(self, actor, tower=None):
        if tower:
            return self.w.adjacent_route(actor, {pos(tower["pos"])}, self.reserved)
        if self.w.base:
            return self.w.adjacent_route(actor, footprint(self.w.base), self.reserved)
        return []

    def return_home(self, actor, tower=None):
        route = self.home_route(actor, tower)
        if route:
            self.emit(actor, command("move", [route[0]]))

    def heal(self, actor):
        return (actor["health"] < max_health(actor["roleType"]) * 0.5 and
                "Medicine" in actor.get("backpack", []) and
                self.emit(actor, command("use", name="Medicine")))

    def use_upgrade(self, actor):
        for item in actor.get("backpack", []):
            if item not in UPGRADES:
                continue
            kinds, level = UPGRADES[item]
            buildings = [u for u in self.w.ours if u["roleType"] in kinds and u.get("level") == level]
            buildings.sort(key=lambda u: (u["health"] / max_health(u["roleType"], level), u["id"]))
            for building in buildings:
                if any(distance(pos(actor["pos"]), c) == 1 for c in footprint(building)):
                    return self.emit(actor, command("use", [pos(building["pos"])], name=item))
            if buildings and self.w.day:
                return self.walk(actor, footprint(buildings[0]))
        return False

    def construct(self, actor):
        w = self.w
        if not w.base:
            return False
        if len(w.weapons) + self.ledger.new_weapons < 3 and self.ledger.gold >= 25:
            sites = sorted(build_ring(w.base, 1) - w.blocked - self.jobs - self.reserved,
                           key=lambda p: (distance(p, (20, 16)), distance(p, pos(actor["pos"])), p))
            for site in sites:
                route = w.adjacent_route(actor, {site}, self.reserved)
                if route is None or len(route) >= w.left:
                    continue
                self.jobs.add(site)
                if route:
                    return self.emit(actor, command("move", [route[0]]))
                counts = Counter(u["roleType"] for u in w.weapons)
                counts.update(c["name"] for c in self.response["roleCommandMap"].values()
                              if c["action"] == "build" and c["name"] in WEAPONS)
                name = next((k for k in self.loadout if counts[k] < self.loadout.count(k)), self.loadout[0])
                return self.emit(actor, command("build", [site], name=name))
        # Progressive wall investment; preserve two central-facing exits.
        walls = [u for u in w.ours if u["roleType"] == "wall"]
        desired_walls = min(14, 4 + 2 * ((w.round - 1) // 130))
        if len(walls) < desired_walls and "stone" in actor.get("backpack", []) and len(w.weapons) == 3:
            ring = sorted(build_ring(w.base, 2), key=lambda p: (distance(p, (20, 16)), p))
            for site in ring[2:]:
                if site in w.blocked or site in self.jobs or site in self.reserved:
                    continue
                route = w.adjacent_route(actor, {site}, self.reserved)
                if route is not None and len(route) < min(w.left - 5, 8):
                    self.jobs.add(site)
                    if route:
                        return self.emit(actor, command("move", [route[0]]))
                    return self.emit(actor, command("build", [site], name="wall"))
        return False

    def economy(self, actor):
        w, p = self.w, pos(actor["pos"])
        bag = Counter(actor.get("backpack", []))
        minerals = sum(bag[k] for k in MINERALS)
        vendors = {c for c, k in w.zones.items() if k == "vendor"}
        shops = {c for c, k in w.zones.items() if k == "weaponShop"}
        if minerals and any(distance(p, c) == 1 for c in vendors):
            name = max((k for k in MINERALS if bag[k] and k in w.vendor),
                       key=lambda k: bag[k] * w.vendor[k], default=None)
            if name:
                return self.emit(actor, command("sell", name=name, num=bag[name]))
        if minerals >= 10 or (minerals and w.left < 30):
            if self.walk(actor, vendors):
                return True
        # At most one outstanding voucher of a given name in the team.
        owned = {item for u in w.actors for item in u.get("backpack", [])} | self.purchase_names
        options = []
        if w.base and w.base.get("level", 1) < 3:
            options.append(f"StationUpgradeVoucher{w.base.get('level', 1)}")
        for gun in sorted(w.weapons, key=lambda u: (u.get("level", 1), u["roleType"] != "rocket")):
            if gun.get("level", 1) < 3:
                options.append(f"WeaponUpgradeVoucher{gun.get('level', 1)}")
        if len(w.weapons) == 3:
            for item in options:
                if item in owned or item not in w.shop or w.shop[item] > self.ledger.gold:
                    continue
                route = w.adjacent_route(actor, shops, self.reserved)
                home = self.home_route(actor)
                if route is not None and len(route) + (len(home) if home is not None else 30) + 8 < w.left:
                    self.purchase_names.add(item)
                    if route:
                        return self.emit(actor, command("move", [route[0]]))
                    return self.emit(actor, command("buy", name=item, num=1))
        capacity = actor.get("backPackCapability", 100)
        if sum(bag.values()) >= capacity:
            return self.walk(actor, vendors)
        day = (w.round - 1) // 130 + 1
        blocked = {c.get("name") for c in self.memory.blocked_minerals if isinstance(c, dict)
                   and type(c.get("startDay")) is int and type(c.get("endDay")) is int
                   and c["startDay"] <= day <= c["endDay"]}
        mines = sorted((c for c, k in w.zones.items() if k in MINERALS and k not in blocked
                        and self.failed_mines.get(c, 0) < w.round), key=lambda c: distance(p, c))
        best = None
        for mine in mines[:8]:
            route = w.adjacent_route(actor, {mine}, self.reserved)
            if route is None or len(route) + 8 >= w.left:
                continue
            price = w.vendor.get(w.zones[mine], 0)
            wall_count = sum(u["roleType"] == "wall" for u in w.ours)
            desired_walls = min(14, 4 + 2 * ((w.round - 1) // 130))
            if (w.zones[mine] == "stone" and bag["stone"] < 2 and len(w.weapons) == 3
                    and wall_count < desired_walls):
                price += 5
            travel_to_vendor = min((distance(mine, v) for v in vendors), default=30)
            value = 10 * price / (len(route) + 10 + travel_to_vendor + 1)
            if best is None or value > best[0]:
                best = value, mine, route
        if best:
            _, mine, route = best
            return self.emit(actor, command("move", [route[0]]) if route else command("collect", [mine]))
        return False

    def accept_task(self, actor):
        candidates = []
        for task in self.w.tasks:
            if not task.get("isValid"):
                continue
            anchor = pos(task["taskPosition"])
            kind = self.w.zones.get(anchor, "")
            if not kind.startswith(self.w.side):
                continue
            cells = {c for c, k in self.w.zones.items() if k == kind}
            route = self.w.adjacent_route(actor, cells, self.reserved)
            if route is None or len(route) + 15 >= self.w.left:
                continue
            reward = task.get("scoreReward", 0) + task.get("goldReward", 0)
            candidates.append((reward / (len(route) + 5), route))
        if not candidates:
            return False
        _, route = max(candidates, key=lambda v: v[0])
        return self.emit(actor, command("move", [route[0]]) if route else command("acceptTask"))

    def solve_task(self, actor, reply):
        answer = structured_answer(self.w.phase)
        if answer is None and reply and isinstance(reply.get("answer"), (str, dict, list, int, float)):
            answer = reply["answer"]
            if not isinstance(answer, str):
                answer = json.dumps(answer, ensure_ascii=False)
        if answer is not None and answer != self.memory.submitted:
            if self.emit(actor, command("submitAnswer", taskAnswer=answer)):
                self.memory.submitted = answer
                self.memory.history.append({"submitted": answer})
                return
        if reply and isinstance(reply.get("executeCmd"), str) and reply["executeCmd"]:
            # Forward only; the participant process NEVER invokes a shell.
            self.response["executeCmd"] = reply["executeCmd"]
            self.memory.history.append({"executed": reply["executeCmd"]})
        else:
            self.response["prompt"] = self.memory.task_prompt(self.w)

    def treasure(self, actor):
        t = self.memory.treasure
        if not t:
            return False
        try:
            target, items = pos(t["pos"]), t["items"]
            start, end = t["startRound"], t["endRound"]
            valid = (all(type(v) is int for v in (*target, start, end)) and inside(target)
                     and isinstance(items, list) and all(isinstance(i, str) for i in items))
        except (KeyError, TypeError):
            return False
        if not valid or self.w.round > end:
            return False
        signature = json.dumps(t, sort_keys=True)
        if signature in self.memory.failed_treasures:
            return False
        if self.w.raw.get("lastSummonTreasureResult", 0) in (1, 3, 4):
            self.memory.failed_treasures.add(signature)
            return False
        bag = Counter(actor.get("backpack", []))
        missing = Counter(items) - bag
        if missing:
            item, num = next(iter(missing.items()))
            if item not in self.w.shop or self.w.shop[item] * num > self.ledger.gold:
                return False
            shops = {c for c, k in self.w.zones.items() if k == "weaponShop"}
            if any(distance(pos(actor["pos"]), c) == 1 for c in shops):
                return self.emit(actor, command("buy", name=item, num=num))
            return self.walk(actor, shops)
        if distance(pos(actor["pos"]), target) <= 1:
            if start <= self.w.round <= end:
                success = self.emit(actor, command("summonTreasure", [target], item=items))
                if success:
                    self.memory.failed_treasures.add(signature)
                return success
            return True
        return self.walk(actor, {target})
