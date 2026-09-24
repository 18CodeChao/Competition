"""Experience-driven strategy, constrained by R01-R07. No simulator imports."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import time

from .audit import Ledger
from .combat import choose_targets, damage, target_metrics, defensive_robot
from .intelligence import Intelligence
from .layout import plan_layout, front_walls
from .protocol import empty_response, schema_errors
from .rules import (WEAPONS, MINERALS, UPGRADES, pos, distance, footprint, neighbours,
                    command, max_health, ROBOT_STATS)
from .world import World
from .tactics import FieldTactics


class AdaptiveStrategy(FieldTactics):
    def decide(self, payload):
        started = time.perf_counter()
        deadline = started + 2.5
        key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if self.cache_key == key:
            return deepcopy(self.cache)
        bases = [u for u in payload["teamOur"]["roles"] if u["roleType"] == "station"]
        identity = (payload["teamOur"].get("teamId"), payload["teamOur"]["type"],
                    tuple(pos(b["pos"]) for b in bases))
        round_no = int(payload["roundNo"])
        if identity != self.identity or round_no < self.last_round:
            self.reset()
        self.identity, self.last_round = identity, round_no
        w = World(deepcopy(payload))
        for tower in w.weapons:
            if tower["roleType"] == "rocket" and "cooldown" not in tower:
                tower["cooldown"] = max(0, self.fired.get(tower["id"], -10) + 4 - w.round)
        self.diagnostics, self.events = [], []
        for uid, success in payload.get("lastRoundRoleActionResults", {}).items():
            previous = self.previous_commands.get(str(uid), {})
            if not success and previous.get("action") == "collect":
                self.failed_mines[pos(previous["targetPos"][0])] = w.round + 4
            if not success and previous.get("action") == "move":
                self.failed_moves[(int(uid), pos(previous["targetPos"][0]))] = w.round + 2
        self.failed_moves = {key: expiry for key, expiry in self.failed_moves.items() if expiry >= w.round}
        w.move_avoid = self.failed_moves
        self.w, self.ledger = w, Ledger(w)
        self.response = empty_response()
        self.reserved, self.jobs, self.purchase_names = set(), set(), set()
        self.maintenance_targets = set()
        self.intelligence.update(w)
        if self.layout is None:
            self.layout = plan_layout(w)
        hub = self.layout["hub"]
        model_reply = self.memory.update(w)
        if self.memory.news_updated:
            self.events.append({'kind': 'treasureInference', 'plan': self.memory.treasure_plan})
        self.gunner = self.select_gunner(hub)
        self.wall_builder = self.select_builder()
        self.w.gunner_id = self.gunner
        hub = self.operator_hub = self.operator_post()
        self.w.protected_hub = hub
        self.prepare_support()
        self.prepare_tactics()
        if not w.day:
            self.fight(deadline)
        ordered = sorted(w.actors, key=lambda a: (a["id"] != self.gunner, a['roleType'] != 'pioneer', a["id"]))
        for actor in ordered:
            uid = actor["id"]
            if uid in self.ledger.used:
                continue
            if not w.day and uid == self.gunner:
                # Firing already ran before every non-combat action. Never retreat or leave the hub.
                if hub and pos(actor["pos"]) != hub:
                    self.walk_exact(actor, hub, caution=None)
                else:
                    self.heal(actor) or self.use_upgrade(actor)
                continue
            if actor["roleType"] == "pioneer" and w.phase:
                # Keep the task alive: no shopping/repair trip and no generic retreat out of range.
                task_area = self.memory.task_area(w)
                if self.escape(actor, allowed=task_area):
                    self.events.append({'kind': 'taskSidestep', 'role': uid, 'reason': '仅在任务范围内避让机器人前进格'})
                if uid not in self.ledger.used:
                    self.heal(actor)
                self.solve_task(actor, model_reply)
                continue
            if actor['roleType'] == 'pioneer':
                self.pioneer_turn(actor)
                continue
            if self.guard_operator(actor) or self.emergency_rebuild(actor):
                continue
            if self.support_turn(actor):
                continue
            if self.day_no < 4 and self.breaches and self.reserve_stone(actor):
                continue
            if actor['id'] == self.wall_builder and self.day_no >= 4:
                if self.use_upgrade(actor, defense_only=True) or self.maintenance_duty(actor):
                    continue
                # Acquire reserve stone before optional purchases/mining; do not
                # let expensive weapon upgrades consume the maintenance budget.
                if self.reserve_stone(actor) or self.buy_defense_stock(actor):
                    continue
                if self.boss_pressure(actor) or self.interference(actor):
                    continue
            raiding = uid in self.raid_assignments or uid in self.raiders
            if not w.day and not raiding and pos(actor["pos"]) not in w.danger_now and self.use_upgrade(actor):
                continue
            if self.escape(actor) or self.heal(actor):
                continue
            if w.day and uid == self.gunner and hub:
                path = w.route(actor, {hub}, self.reserved, caution=None)
                if w.left <= (len(path) if path is not None else 25) + 8:
                    self.walk_exact(actor, hub, caution=None)
                    continue
            if not raiding and self.use_upgrade(actor):
                continue
            if actor["roleType"] == "worker":
                if w.day and self.construct(actor):
                    continue
                if actor['id'] != self.gunner and (self.boss_pressure(actor) or self.interference(actor)):
                    continue
                # Only the designated gunner returns before dusk. Workers/pioneer stay productive.
                if w.day and uid == self.gunner and hub:
                    path = w.route(actor, {hub}, self.reserved)
                    if w.left <= (len(path) if path is not None else 25) + 4:
                        self.walk_exact(actor, hub)
                        continue
                if self.economy(actor):
                    continue
            # Keep spare units off the hub and out of the rear passage.
            if w.base:
                rear = {p for p in neighbours(pos(w.base["pos"])) if p not in self.layout["guns"] and p != hub}
                path = w.route(actor, rear, self.reserved)
                if path:
                    self.emit(actor, command("move", [path[0]]))
        if not w.phase and not self.response["prompt"]:
            self.response["prompt"] = self.memory.news_prompt(w)
        if schema_errors(self.response):
            raise ValueError("internal response schema failure")
        self.trace = {"strategy": "platform-v8", "elapsedMs": (time.perf_counter() - started) * 1000,
                      "layout": deepcopy(self.layout), "gunner": self.gunner, "wallBuilder": self.wall_builder,
                      'operatorPost': hub, 'breaches': sorted(self.breaches),
                      'supportGunner': self.support_gunner, 'supportPost': self.support_post,
                      'raidAssignments': deepcopy(self.raid_assignments), 'blockingBreach': self.blocking_breach,
                      "wallStockTarget": self.stone_target(),
                      "defensiveRobots": [r["id"] for r in w.robots if defensive_robot(r, w.base, w.side)],
                      "ignoredRobots": [r["id"] for r in w.robots if not defensive_robot(r, w.base, w.side)],
                      "taskState": self.memory.diagnostics(w), "events": deepcopy(self.events),
                      "auditRejections": deepcopy(self.diagnostics),
                      "enemyMemory": deepcopy(self.intelligence.enemies),
                      "offenseAssessment": self.intelligence.offense_assessment(w),
                      "prices": deepcopy(self.intelligence.prices),
                      "damagePerRound": dict(self.intelligence.damage_per_round)}
        self.previous_commands = deepcopy(self.response["roleCommandMap"])
        self.previous_actor_hp = {a['id']: a['health'] for a in w.actors}
        self.cache_key, self.cache = key, deepcopy(self.response)
        return self.response

    def select_builder(self):
        workers = [u for u in self.w.actors if u["roleType"] == "worker" and u["id"] != self.gunner]
        if not workers:
            return None
        return max(workers, key=lambda a: (a.get("backpack", []).count("stone"), -a["id"]))["id"]

    def select_gunner(self, hub):
        workers = [u for u in self.w.actors if u["roleType"] == "worker"]
        if not workers:
            self.gunner_id = None
            return None
        if any(u["id"] == self.gunner_id for u in workers):
            return self.gunner_id
        self.gunner_id = min(workers, key=lambda u: (pos(u["pos"]) != hub,
            "stone" in u.get("backpack", []), distance(pos(u["pos"]), hub) if hub else 0, u["id"]))["id"]
        return self.gunner_id

    def walk_exact(self, actor, target, caution=True):
        path = self.w.route(actor, {target}, self.reserved, caution=caution)
        if path:
            return self.emit(actor, command("move", [path[0]]))
        return path == []

    def escape(self, actor, allowed=None):
        hurt = allowed is not None and actor['health'] < self.previous_actor_hp.get(actor['id'], actor['health'])
        if actor["id"] == self.gunner or self.w.day or (pos(actor["pos"]) not in self.w.danger_now and not hurt):
            return False
        p = pos(actor["pos"])
        def risk(cell):
            observed_risk = sum(ROBOT_STATS.get(r['roleType'], (0, 5, 0))[1]
                                for r in self.w.robots if r.get('abnormalState') != 'dizzy'
                                and distance(cell, pos(r['pos'])) <= 3) if hurt else 0
            return self.w.robot_risk.get(cell, 0) + self.w.enemy_fire_risk(cell) / 10 + observed_risk
        options = [q for q in neighbours(p) if q not in self.w.blocked | self.reserved
                   and q != self.layout["hub"] and (allowed is None or q in allowed)]
        if options:
            best = min(options, key=lambda q: (risk(q), distance(q, self.layout["hub"]) if self.layout["hub"] else 0, q))
            if risk(best) < risk(p):
                self.events.append({"kind": "retreat", "role": actor["id"], "riskBefore": risk(p), "riskAfter": risk(best)})
                return self.emit(actor, command("move", [best]))
        return False

    def fight(self, deadline):
        allocated = {}
        for uid in (self.gunner, self.support_gunner):
            if uid is not None:
                self.fire_controller(uid, deadline, allocated)

    def fire_controller(self, controller, deadline, allocated):
        w = self.w
        gunner = w.units.get(controller)
        if not gunner:
            self.events.append({"kind": "noGunner", "reason": "no living worker"})
            return
        ready = [t for t in w.weapons if t.get("cooldown", 0) == 0 and t['id'] not in self.ledger.used
                 and distance(pos(gunner["pos"]), pos(t["pos"])) <= 1]
        choices = []
        for tower in ready:
            targets = choose_targets(tower, w.robots, w.base, w.side, allocated, deadline)
            if not targets:
                continue
            metrics = target_metrics(tower, targets, w.robots)
            if metrics["effectiveDamage"]:
                choices.append((metrics["effectiveDamage"] + 40 * metrics["predictedKills"], tower, targets, metrics))
        choices.sort(key=lambda c: (-c[0], c[1]["id"]))
        for _, tower, targets, metrics in choices:
            if gunner and distance(pos(gunner["pos"]), pos(tower["pos"])) <= 1:
                if self.emit(tower, command("attack", targets, controllerId=str(gunner["id"]))):
                    self.events.append({"kind": "volley", "tower": tower["id"], "controller": gunner["id"],
                                        "targets": targets, **metrics})
                    for rid, dealt in damage(tower, targets, w.robots).items():
                        allocated[rid] = allocated.get(rid, 0) + dealt
                    break
        if controller not in self.ledger.used:
            self.events.append({"kind": "gunnerIdle", "role": controller,
                                "reason": "no reachable ready weapon" if not ready else "no permitted target",
                                "cooldowns": {t["id"]: t.get("cooldown", 0) for t in w.weapons}})

    def missing_walls(self):
        present = {pos(u["pos"]) for u in self.w.ours if u["roleType"] == "wall"}
        return set(self.layout["walls"]) - present

    def stone_target(self):
        reserve = 3 if (self.w.round - 1) // 130 + 1 >= 4 else 0
        return min(10, len(self.missing_walls()) + reserve)

    def construct(self, actor):
        w = self.w
        if not w.base or not w.day:
            return False
        if len(w.weapons) + self.ledger.new_weapons < 3 and self.ledger.gold >= 25:
            for site in sorted(set(self.layout["guns"]) - w.blocked - self.jobs - self.reserved,
                               key=lambda p: (distance(p, pos(actor["pos"])), p)):
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
        missing = self.missing_walls() - self.jobs - self.reserved
        stones = actor.get("backpack", []).count("stone")
        if not missing or not stones or actor["id"] != self.wall_builder:
            return False
        path_home = self.home_route(actor)
        travel = len(path_home) if path_home is not None else 30
        # Construction is day-only: budget return + every placement + local movement.
        deadline = travel + 2 * len(missing) + 5
        full_batch = stones >= min(10, len(missing))
        already_home = min(distance(pos(actor["pos"]), p) for p in missing) <= 2
        if not full_batch and w.left > deadline and not already_home:
            return False
        for site in sorted(missing - w.blocked, key=lambda p: (distance(p, pos(actor["pos"])), p)):
            path = w.adjacent_route(actor, {site}, self.reserved)
            if path is not None and len(path) < w.left:
                self.jobs.add(site)
                self.events.append({"kind": "wallBatch", "stock": stones, "missing": len(missing),
                                    "daylightRemaining": w.left, "constructionBudget": deadline})
                return self.emit(actor, command("move", [path[0]]) if path else command("build", [site], name="wall"))
        return False

    def use_upgrade(self, actor, defense_only=False):
        w = self.w
        all_candidates = []
        for item in sorted(set(actor.get("backpack", []))):
            if item not in UPGRADES and item != "WallFixer":
                continue
            for building in w.ours:
                if building["id"] in self.maintenance_targets:
                    continue
                kind, level = building["roleType"], building.get("level", 1)
                if defense_only and kind not in ('station', 'wall'):
                    continue
                if item == "WallFixer":
                    if kind != "wall":
                        continue
                elif kind not in UPGRADES[item][0] or level != UPGRADES[item][1]:
                    continue
                maximum = max_health(kind, level)
                route = (self.repair_route(actor, building) if kind in ('wall', 'station') else
                         w.adjacent_route(actor, footprint(building), self.reserved))
                if not w.day and actor["id"] == self.gunner and route:
                    continue
                travel = len(route) if route is not None else 0
                incoming = self.incoming_damage(building, travel)
                threshold = max(.55 * maximum, (travel + 3) * incoming + 1)
                # Weapons gain damage/range immediately. Base/wall vouchers retain their healing option.
                needed = ((building['health'] < maximum and building['health'] <= threshold)
                          or (item in UPGRADES and incoming * (travel + 1) >= building['health'])
                          or (kind in WEAPONS and item in UPGRADES))
                if not needed or (item == "WallFixer" and building["health"] == maximum):
                    continue
                if route is not None:
                    # Select endangered building first, then upgrade before fixer
                    # for that same target, regardless of backpack order.
                    urgency = building['health'] / max(1, incoming)
                    priority = (0 if kind == 'station' and building['health'] <= max(.35 * maximum, (travel + 2) * incoming)
                                else 2 if kind in WEAPONS else 1)
                    all_candidates.append((priority, urgency, len(route), building['id'], item == 'WallFixer', item, building, route, threshold, incoming))
        if all_candidates:
            _, _, _, _, _, item, building, route, threshold, incoming = min(all_candidates, key=lambda c: c[:6])
            self.maintenance_targets.add(building['id'])
            self.events.append({'kind': 'maintenance', 'role': actor['id'], 'building': building['id'],
                                'item': item, 'hp': building['health'], 'threshold': threshold,
                                'incomingEstimate': incoming, 'travelRounds': len(route)})
            if route:
                return self.emit(actor, command('move', [route[0]]))
            return self.emit(actor, command('use', [pos(building['pos'])], name=item))
        return False

    def hold_mineral(self, name, actor):
        if self.blocking_breach:
            return False
        day = (self.w.round - 1) // 130 + 1
        if self.ledger.gold < 30 or len(actor.get("backpack", [])) >= actor.get("backPackCapability", 100) - 10:
            return False
        if self.w.base and self.w.base["health"] < .6 * max_health("station", self.w.base.get("level", 1)):
            return False
        for forecast in self.memory.price_forecasts:
            if (isinstance(forecast, dict) and forecast.get("name") == name and forecast.get("direction") == "up"
                    and isinstance(forecast.get("confidence"), (float, int)) and forecast["confidence"] >= .8
                    and type(forecast.get("startDay")) is int and day < forecast["startDay"] <= day + 2
                    and isinstance(forecast.get("evidence"), str) and forecast["evidence"]):
                return True
        return False

    def procure(self, actor):
        w = self.w
        if actor['id'] == self.wall_builder and self.day_no >= 4:
            if self.buy_defense_stock(actor):
                return True
            # Keep available money for personal maintenance stock/BOSS pressure.
            bag = Counter(actor.get('backpack', []))
            if self.blocking_breach or any(bag[n] < count for n, count in self.defense_stock(actor)):
                return False
        if self.blocking_breach:
            return False
        shops = {c for c, k in w.zones.items() if k == "weaponShop"}
        # Buy upgrades/repair stock before undertaking another long mining trip.
        owned = {item for u in w.actors for item in u.get("backpack", [])} | self.purchase_names
        options = []
        if w.base and w.base.get("level", 1) < 3:
            base_item = f"StationUpgradeVoucher{w.base.get('level', 1)}"
        else:
            base_item = None
        for gun in sorted(w.weapons, key=lambda u: (u.get("level", 1), u["id"])):
            if gun.get("level", 1) < 3:
                options.append(f"WeaponUpgradeVoucher{gun.get('level', 1)}")
        if base_item:
            options.insert(0 if w.base["health"] < .35 * max_health("station", w.base.get("level", 1)) else len(options), base_item)
        damaged_walls = [u for u in w.ours if u["roleType"] == "wall" and u["health"] < .7 * max_health("wall", u.get("level", 1))]
        if damaged_walls:
            weakest = min(damaged_walls, key=lambda u: u["health"] / max_health("wall", u.get("level", 1)))
            options.insert(0 if weakest["health"] < .25 * max_health("wall", weakest.get("level", 1)) else len(options), f"WallUpgradeVoucher{weakest.get('level', 1)}" if weakest.get("level", 1) < 3 else "WallFixer")
        if actor["health"] < 150:
            options.insert(0, "Medicine")
        if len(w.weapons) == 3:
            for item in options:
                if item in owned or item not in w.shop or w.shop[item] > self.ledger.gold:
                    continue
                if actor["id"] == self.gunner:
                    continue
                route = w.adjacent_route(actor, shops, self.reserved)
                if route is not None and len(route) + 2 < w.horizon:
                    if route:
                        return self.emit(actor, command("move", [route[0]]))
                    self.purchase_names.add(item)
                    self.events.append({"kind": "purchase", "role": actor["id"], "item": item})
                    return self.emit(actor, command("buy", name=item, num=1))
        return False

    def economy(self, actor):
        w, p = self.w, pos(actor["pos"])
        bag = Counter(actor.get("backpack", []))
        vendors = {c for c, k in w.zones.items() if k == "vendor"}
        shops = {c for c, k in w.zones.items() if k == "weaponShop"}
        target = self.stone_target() if actor["id"] == self.wall_builder else 0
        stone_needed = max(0, target - bag["stone"])
        # Keep a wall-building batch; do not liquidate it when passing the vendor.
        sellable = {k: max(0, bag[k] - (target if k == "stone" else 0)) for k in MINERALS
                    if k in w.vendor and not self.hold_mineral(k, actor)}
        minerals = sum(sellable.values())
        if minerals and any(distance(p, c) == 1 for c in vendors):
            name = max(sellable, key=lambda k: sellable[k] * w.vendor[k])
            return self.emit(actor, command("sell", name=name, num=sellable[name]))
        if self.procure(actor):
            return True
        if minerals >= (5 if self.blocking_breach else 15) or (minerals and sum(bag.values()) >= actor.get("backPackCapability", 100) - 5):
            if self.walk(actor, vendors):
                return True
        if sum(bag.values()) >= actor.get("backPackCapability", 100):
            return False
        day = (w.round - 1) // 130 + 1
        blocked = {c.get("name") for c in self.memory.blocked_minerals if isinstance(c, dict)
                   and type(c.get("startDay")) is int and type(c.get("endDay")) is int
                   and c["startDay"] <= day <= c["endDay"]}
        mines = sorted((c for c, k in w.zones.items() if k in MINERALS and k not in blocked
                        and self.failed_mines.get(c, 0) < w.round), key=lambda c: distance(p, c))
        best = None
        for mine in mines[:10]:
            route = w.adjacent_route(actor, {mine}, self.reserved)
            if route is None:
                continue
            mineral = w.zones[mine]
            price = w.vendor.get(mineral, 0)
            if any(f.get('name') == mineral and f.get('direction') == 'up'
                   and type(f.get('startDay')) is int and day < f['startDay'] <= day + 2
                   and isinstance(f.get('confidence'), (int, float)) and f['confidence'] >= .8
                   for f in self.memory.price_forecasts if isinstance(f, dict)):
                price *= 1.5  # Planning preference only; actual sale uses vendorShopList.
            if mineral == "stone" and stone_needed:
                price += 15
            travel = min((distance(mine, v) for v in vendors), default=30)
            value = 10 * price / (len(route) + 10 + travel + 1)
            if best is None or value > best[0]:
                best = value, mine, route
        if best:
            _, mine, route = best
            self.events.append({"kind": "mine", "role": actor["id"], "mineral": w.zones[mine],
                                "target": mine, "stock": bag[w.zones[mine]], "stoneBatch": target})
            return self.emit(actor, command("move", [route[0]]) if route else command("collect", [mine]))
        return False

    def accept_task(self, actor):
        # User strategy: ten complete night turns must pass before accepting, not a game rule.
        if not self.w.day and (self.w.round - 1) % 130 < 80:
            return False
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
            if route is not None:
                if self.w.day and len(route) + task.get('timeoutRounds', 15) + 1 > self.w.left:
                    continue  # Avoid carrying a newly accepted task through the spawn window.
                if not self.w.day and not route and (pos(actor['pos']) in self.w.danger_now
                                                     or self.w.enemy_fire_risk(pos(actor['pos']))):
                    continue
                reward = task.get("scoreReward", 0) + task.get("goldReward", 0)
                candidates.append((reward / (len(route) + 5), route, task))
        if not candidates:
            return False
        _, route, task = max(candidates, key=lambda v: v[0])
        success = self.emit(actor, command("move", [route[0]]) if route else command("acceptTask"))
        if success and not route:
            self.memory.accepted = (self.w.round, dict(task))
        return success
