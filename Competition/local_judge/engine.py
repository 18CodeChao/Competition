"""Two-team simultaneous local engine. Unknown rules are listed in docs/IMPLEMENTATION.md."""
from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction
import json
import random

from agent.audit import Ledger, eligible
from agent.combat import damage
from agent.protocol import schema_errors, empty_response
from agent.rules import (WIDTH, HEIGHT, ACTORS, WEAPONS, MINERALS, SHOP, ROBOT_STATS,
                         SUMMONS, UPGRADES, pos, xy, distance, footprint, build_ring,
                         neighbours, max_health, reach, unit_distance, daylight)
from agent.world import World
from .profiles import observed_wave

SIDES = ("challenger", "defender")


def make_unit(uid, kind, p, level=1):
    unit = {"id": uid, "roleType": kind, "pos": xy(p), "health": max_health(kind, level),
            "attackPower": 0, "attackRange": 0, "backPackCapability": 0, "backpack": []}
    if kind in ACTORS:
        unit["backPackCapability"] = 100 if kind == "worker" else 40
    else:
        unit["level"] = level
    if kind in WEAPONS:
        unit["attackRange"] = reach(unit)
        unit["attackPower"] = 20 if kind == "rocket" else 10 * level
        unit["cooldown"] = 0
    return unit


def simultaneous_moves(positions, intents, static):
    """Resolve same-destination, swaps and cascading stationary blockers jointly."""
    failed = {uid for uid, target in intents.items() if target in static}
    destinations = defaultdict(list)
    for uid, target in intents.items():
        destinations[target].append(uid)
    for ids in destinations.values():
        if len(ids) > 1:
            failed.update(ids)
    occupants = {p: uid for uid, p in positions.items()}
    for uid, target in intents.items():
        other = occupants.get(target)
        if other is not None and intents.get(other) == positions[uid]:
            failed.update((uid, other))
    changed = True
    while changed:
        changed = False
        for uid, target in intents.items():
            if uid in failed:
                continue
            other = occupants.get(target)
            if other is not None and (other not in intents or other in failed):
                failed.add(uid)
                changed = True
    return {uid: target for uid, target in intents.items() if uid not in failed}, failed


class ReplayServices:
    """Explicit test double. Never executes submitted shell on this computer."""
    def __init__(self, commands=None, prompts=None):
        self.commands = commands or {}
        self.prompts = prompts or {}

    def command(self, text):
        output = self.commands.get(text, "[JUDGER_ERROR]\nLocal sandbox is not configured; command was not executed.")
        raw = output.encode("utf-8")
        if len(raw) > 65536:
            output = raw[:65536].decode("utf-8", errors="ignore") + "\n[TRUNCATED]"
        return output

    def prompt(self, text):
        return self.prompts.get(text, "")


class Game:
    def __init__(self, seed=1, pressure=1, services=None, tasks_enabled=True,
                 profile="legacy", unknown_waves="hold8"):
        if pressure < 0:
            raise ValueError("pressure must be nonnegative")
        self.seed, self.pressure = seed, pressure
        if profile not in ("legacy", "observed"):
            raise ValueError("unknown scenario profile")
        self.profile, self.unknown_waves = profile, unknown_waves
        self.rng = random.Random(seed)
        self.round = 1
        self.services = services or ReplayServices()
        self.teams = {}
        self.zones = {(20, 16): "vendor", (25, 20): "weaponShop",
                      (14, 14): "challengerTaskPoint1", (17, 17): "challengerTaskPoint2",
                      (16, 17): "challengerTaskPoint2", (23, 14): "defenderTaskPoint1",
                      (26, 17): "defenderTaskPoint2", (27, 17): "defenderTaskPoint2"}
        self.robots = {}
        self.robot_id = 30000
        self.cooldowns, self.stuns, self.revivals = {}, {}, {}
        self.destroyed_at = {s: None for s in SIDES}
        self.score = {s: {"task": Fraction(0), "kill": Fraction(0), "survival": Fraction(0)} for s in SIDES}
        self.exceptions = {s: 0 for s in SIDES}
        self.stopped = {s: False for s in SIDES}
        self.failures = {s: 0 for s in SIDES}
        self.action_counts = {s: Counter() for s in SIDES}
        self.task_state = {s: None for s in SIDES}
        self.task_available = {s: [1, 1] for s in SIDES}
        self.task_cursor = {s: [0, 0] for s in SIDES}
        self.task_specs = {s: [[], []] for s in SIDES}
        self.feedback = {s: self._blank_feedback() for s in SIDES}
        self.llm_counts = {s: 0 for s in SIDES}
        self.summon_counts = {s: 0 for s in SIDES}
        self.queued_summons = {s: [] for s in SIDES}
        self.vendor_prices = {"stone": 1, "iron": 3, "copper": 5}
        self.closed_minerals = set()
        self.news = {"officialNews": "今日无重大新闻", "folkLegends": ""}
        self.treasure = {"pos": (20, 10), "items": ["AcientTablet", "StarSand"],
                         "start": 261, "end": 650, "score": 100,
                         "gold": 160 if profile == "observed" else 100, "empty": False}
        self.last_events = []
        for index, side in enumerate(SIDES):
            prefix = (index + 1) * 10000
            base = ((9, 22) if profile == "observed" else (10, 24)) if index == 0 else (30, 10)
            x, y = base
            units = [make_unit(prefix + 13, "station", base),
                     make_unit(prefix + 10, "worker", (x - 1, y)),
                     make_unit(prefix + 11, "pioneer", (x - 1, y - 1)),
                     make_unit(prefix + 12, "worker", (x + 2, y))]
            self.teams[side] = {"type": side, "teamId": str(index + 1), "teamName": side,
                                "goldNum": 75, "roles": units}
            if tasks_enabled:
                for slot in (0, 1):
                    for _ in range(3 if profile == "observed" else 4):
                        values = [self.rng.randint(1, 99) for _ in range(4 + slot)]
                        description = json.dumps({"operation": "sum", "values": values, "answerKey": "answer"})
                        self.task_specs[side][slot].append({"description": description,
                            "answer": {"answer": sum(values)}, "timeout": 20, "score": 50,
                            "gold": 80 if profile == "observed" else 30})
        self.mines = {}
        for mineral in MINERALS:
            for _ in range(4):
                self._spawn_mine(mineral)
        self.prepare()

    @staticmethod
    def _blank_feedback():
        return {"lastRoundRoleActionResults": {}, "lastSummonTreasureResult": 0,
                "llmResp": "", "lastCmdResult": "", "errors": []}

    def living(self, side):
        return [u for u in self.teams[side]["roles"] if u["health"] > 0]

    def base(self, side):
        return next(u for u in self.teams[side]["roles"] if u["roleType"] == "station")

    def unit(self, uid):
        for side in SIDES:
            for u in self.teams[side]["roles"]:
                if u["id"] == uid:
                    return u
        return self.robots.get(uid)

    def all_occupied(self):
        cells = set(self.zones)
        for side in SIDES:
            for u in self.living(side):
                cells.update(footprint(u))
        cells.update(pos(r["pos"]) for r in self.robots.values())
        return cells

    def _spawn_mine(self, mineral):
        forbidden = self.all_occupied()
        for side in SIDES:
            forbidden |= footprint(self.base(side)) | build_ring(self.base(side), 1) | build_ring(self.base(side), 2)
        available = [(x, y) for x in range(WIDTH) for y in range(HEIGHT) if (x, y) not in forbidden]
        if available:
            cell = self.rng.choice(available)
            self.zones[cell] = mineral
            self.mines[cell] = 10

    def _spawn_robot(self, side, kind):
        base = self.base(side)
        occupied = self.all_occupied()
        candidates = [(x, y) for x in range(WIDTH) for y in range(HEIGHT)
                      if 9 <= unit_distance((x, y), base) <= 12 and (x, y) not in occupied]
        if not candidates:
            return
        self.robot_id += 1
        robot = {"id": self.robot_id, "roleType": kind, "pos": xy(self.rng.choice(candidates)),
                 "health": ROBOT_STATS[kind][0], "targetTeam": side, "abnormalState": ""}
        self.robots[self.robot_id] = robot

    def prepare(self):
        offset = (self.round - 1) % 130
        day = (self.round - 1) // 130 + 1
        if offset == 0:
            self.robots.clear()
            self.stuns.clear()
            self.llm_counts = {s: 0 for s in SIDES}
            self.summon_counts = {s: 0 for s in SIDES}
            # Explicit fixture, not a claim about official news distribution.
            self.closed_minerals = {"iron"} if day in (2, 3) else set()
            self.vendor_prices["iron"] = 6 if day in (2, 3) else 3
            self.news = {"officialNews": ("铁矿明日起停采两天，期间收购价上涨。" if day == 1 else
                         "今日铁矿暂停采集。" if day in (2, 3) else "今日矿产供应正常。"),
                         "folkLegends": ("祭坛位于(20,10)，第3天至第5天开放，献祭AcientTablet和StarSand。"
                                         if day == 2 else "")}
        if offset == 70:
            for side in SIDES:
                if self.profile == "observed":
                    counts = observed_wave(day, self.unknown_waves)
                    kinds = [kind for kind, count in zip(ROBOT_STATS, counts) for _ in range(count * self.pressure)]
                    self._spawn_formation(side, kinds + self.queued_summons[side])
                else:
                    kinds = (["smallRobot"] * (2 + 2 * day) + ["middleRobot"] * (day // 2)
                             + ["largeRobot"] * (day // 4) + ["bossRobot"] * (day // 7)) * self.pressure
                    for kind in kinds + self.queued_summons[side]:
                        self._spawn_robot(side, kind)
                self.queued_summons[side] = []
        for side in SIDES:
            for u in self.teams[side]["roles"]:
                if u["roleType"] in WEAPONS:
                    u["cooldown"] = max(0, self.cooldowns.get(u["id"], 0) - self.round)
                if u["id"] in self.revivals and self.round >= self.revivals[u["id"]] and self.base(side)["health"] > 0:
                    cells = sorted(build_ring(self.base(side), 1) - self.all_occupied())
                    if cells:
                        u["pos"], u["health"] = xy(cells[0]), max_health(u["roleType"])
                        del self.revivals[u["id"]]
            active = self.task_state[side]
            if active and self.round - active["accepted"] > active["spec"]["timeout"]:
                self._end_task(side, False, self.round - 1)
                self.feedback[side]["errors"].append({"errorCode": 1, "description": "task timeout"})
        for robot in self.robots.values():
            robot["abnormalState"] = "dizzy" if self.stuns.get(robot["id"], 0) > self.round else ""

    def _spawn_formation(self, side, kinds):
        """Column bands approximate the supplied example; precise spawn samples remain unknown."""
        base_x, base_y = pos(self.base(side)["pos"])
        sign = 1 if base_x < 20 else -1
        x = base_x + (13 if sign == 1 else -12)
        occupied = self.all_occupied()
        for kind in reversed(tuple(ROBOT_STATS)):
            remaining = kinds.count(kind)
            while remaining and 0 <= x < WIDTH:
                rows = [y for y in range(max(0, base_y - 6), min(HEIGHT, base_y + 6)) if (x, y) not in occupied]
                self.rng.shuffle(rows)
                for y in rows[:remaining]:
                    self.robot_id += 1
                    self.robots[self.robot_id] = {"id": self.robot_id, "roleType": kind,
                        "pos": xy((x, y)), "health": ROBOT_STATS[kind][0], "targetTeam": side, "abnormalState": ""}
                    occupied.add((x, y))
                    remaining -= 1
                x += sign
            if remaining:
                raise ValueError("formation exhausted map; reduce experimental pressure")

    def observation(self, side):
        enemy = SIDES[1 - SIDES.index(side)]
        ours = self.living(side)
        vision = {p for u in ours for p in footprint(u)}
        visible = []
        for u in self.living(enemy):
            if u["roleType"] in ("station", "wall") or any(
                distance(p, q) <= 4 for p in footprint(u) for q in vision
            ):
                visible.append(u)
        team = deepcopy(self.teams[side])
        # A09: interface is int; retain exact fractions internally and in reports.
        team["totalScore"] = int(sum(self.score[side].values()))
        tasks = []
        for slot in (0, 1):
            kind = f"{side}TaskPoint{slot + 1}"
            cells = sorted(c for c, k in self.zones.items() if k == kind)
            cursor = self.task_cursor[side][slot]
            specs = self.task_specs[side][slot]
            spec = specs[cursor] if cursor < len(specs) else {"score": 0, "gold": 0, "timeout": 20}
            remaining = max(0, self.task_available[side][slot] - self.round)
            tasks.append({"taskType": f"自进化类{slot + 1}", "taskPosition": xy(cells[0]),
                          "coldDownRounds": remaining, "scoreReward": spec["score"],
                          "goldReward": spec["gold"], "timeoutRounds": spec["timeout"],
                          "isValid": remaining == 0 and cursor < len(specs) and self.task_state[side] is None})
        team["playerTasks"] = tasks
        active = self.task_state[side]
        result = {"roundNo": self.round,
                  "mapInfo": {"width": WIDTH, "height": HEIGHT,
                              "zones": [{"pos": xy(p), "neutralType": k} for p, k in sorted(self.zones.items())]},
                  "teamOur": team, "teamEnemy": {"roles": deepcopy(visible)},
                  "robot": {"roles": deepcopy(list(self.robots.values()))},
                  "phaseTask": active["spec"]["description"] if active else "",
                  "worldNews": dict(self.news),
                  "vendorShopList": [{"name": n, "price": p} for n, p in self.vendor_prices.items()],
                  "weaponShopList": [{"name": n, "price": p} for n, p in SHOP.items()]}
        result.update(deepcopy(self.feedback[side]))
        return result

    def _end_task(self, side, complete, finished):
        active = self.task_state[side]
        if not active:
            return
        spec = active["spec"]
        if complete:
            elapsed = max(1, finished - active["accepted"])
            self.score[side]["task"] += spec["score"] + Fraction(5 * spec["timeout"], elapsed)
        else:
            self.score[side]["task"] += spec["score"] * active["best"]
        self.teams[side]["goldNum"] += int(spec["gold"] * active["best"])
        slot = active["slot"]
        self.task_cursor[side][slot] += 1
        self.task_available[side][slot] = finished + 31
        self.task_state[side] = None

    def _task_action(self, side, actor, cmd):
        if cmd["action"] == "acceptTask":
            for slot in (0, 1):
                kind = f"{side}TaskPoint{slot + 1}"
                cells = {c for c, k in self.zones.items() if k == kind}
                cursor = self.task_cursor[side][slot]
                if (self.round >= self.task_available[side][slot] and cursor < len(self.task_specs[side][slot])
                        and any(distance(pos(actor["pos"]), c) == 1 for c in cells)):
                    self.task_state[side] = {"accepted": self.round, "slot": slot, "actor": actor["id"],
                        "spec": self.task_specs[side][slot][cursor], "best": Fraction(0), "cells": cells}
                    return True
            return False
        active = self.task_state[side]
        if not active:
            return False
        try:
            answer = json.loads(cmd["taskAnswer"])
        except ValueError:
            answer = {}
        expected = active["spec"]["answer"]
        correct = sum(isinstance(answer, dict) and k in answer and answer[k] == v for k, v in expected.items())
        rate = Fraction(correct, len(expected))
        active["best"] = max(active["best"], rate)
        if rate == 1:
            self._end_task(side, True, self.round)
        else:
            self.feedback[side]["errors"].append({"errorCode": 2, "description": "answer incomplete or incorrect"})
        return True

    def _new_building(self, side, kind, cell):
        team = self.teams[side]
        existing = next((u for u in self.living(side) if pos(u["pos"]) == cell), None)
        if existing:
            team["roles"].remove(existing)
        prefix = (SIDES.index(side) + 1) * 10000
        start = {"gatling": prefix + 20, "railgun": prefix + 30, "rocket": prefix + 40,
                 "wall": 40000 + SIDES.index(side) * 1000}[kind]
        occupied_ids = {u["id"] for u in team["roles"] if u["health"] > 0}
        uid = start
        while uid in occupied_ids:
            uid += 1
        team["roles"] = [u for u in team["roles"] if u["id"] != uid]
        team["roles"].append(make_unit(uid, kind, cell))
        self.cooldowns.pop(uid, None)

    def _robot_intents(self):
        intents, attacks = {}, []
        static = set(self.zones)
        buildings = [u for side in SIDES for u in self.living(side) if u["roleType"] not in ACTORS]
        for u in buildings:
            static.update(footprint(u))
        for rid, robot in sorted(self.robots.items()):
            if self.stuns.get(rid, 0) > self.round:
                continue
            side = robot["targetTeam"]
            base = self.base(side)
            if base["health"] <= 0:
                continue
            p = pos(robot["pos"])
            if self.profile == 'observed':
                # User feedback 2026-09-23: advance to base, hit obstructing units only.
                # Tie-break and base firing priority remain explicit local assumptions.
                if unit_distance(p, base) <= 3:
                    attacks.append((base['id'], ROBOT_STATS[robot['roleType']][1]))
                    continue
                options = [q for q in neighbours(p) if q not in self.zones and unit_distance(q, base) < unit_distance(p, base)]
                if options:
                    target = min(options, key=lambda q: (unit_distance(q, base), q))
                    blocker = next((u for s in SIDES for u in self.living(s) if target in footprint(u)), None)
                    if blocker:
                        attacks.append((blocker['id'], ROBOT_STATS[robot['roleType']][1]))
                    else:
                        intents[rid] = target
                continue
            # A03: prefer nearby defending units, then greedy motion toward the base.
            victims = [u for u in self.living(side) if unit_distance(p, u) <= 3]
            if victims:
                target = min(victims, key=lambda u: (unit_distance(p, u), u["roleType"] != "station", u["id"]))
                attacks.append((target["id"], ROBOT_STATS[robot["roleType"]][1]))
            else:
                options = [q for q in neighbours(p) if q not in static]
                if options:
                    target = min(options, key=lambda q: (unit_distance(q, base), q))
                    if unit_distance(target, base) < unit_distance(p, base):
                        intents[rid] = target
        return intents, attacks

    def step(self, responses):
        if self.finished:
            raise RuntimeError("game has ended")
        self.last_events = []
        observations = {s: self.observation(s) for s in SIDES}
        active_before = {s: self.task_state[s] is not None for s in SIDES}
        self.feedback = {s: self._blank_feedback() for s in SIDES}
        accepted = []
        for side in SIDES:
            if self.exceptions[side] >= 5 or self.stopped[side]:
                continue
            response = responses.get(side, empty_response())
            issues = schema_errors(response)
            if issues:
                self.exceptions[side] += 1
                self.feedback[side]["errors"].append({"errorCode": 4, "description": "; ".join(issues)})
                continue
            w, ledger = World(observations[side]), None
            ledger = Ledger(w)
            ledger.summons = self.summon_counts[side]
            for key, cmd in response["roleCommandMap"].items():
                uid = int(key)
                reason = eligible(w, uid, cmd, ledger)
                self.feedback[side]["lastRoundRoleActionResults"][key] = reason is None
                if reason:
                    self.failures[side] += 1
                    self.last_events.append({"team": side, "id": uid, "failure": reason})
                else:
                    accepted.append((side, uid, cmd))
                    self.action_counts[side][cmd["action"]] += 1
            prompt = response.get("prompt", "")
            if prompt:
                if active_before[side] or self.llm_counts[side] < 3:
                    if not active_before[side]:
                        self.llm_counts[side] += 1
                    self.feedback[side]["llmResp"] = self.services.prompt(prompt)
                else:
                    self.feedback[side]["errors"].append({"errorCode": 5, "description": "daily LLM limit"})
            cmd = response.get("executeCmd", "")
            if cmd:
                self.feedback[side]["lastCmdResult"] = (self.services.command(cmd) if active_before[side] else
                    "[JUDGER_ERROR]\nexecuteCmd requires an active task")
        robot_damage = {s: defaultdict(int) for s in SIDES}
        fired = set()
        intents, collected, treasure_attempts = {}, Counter(), []
        for side, uid, cmd in accepted:
            u, action, team = self.unit(uid), cmd["action"], self.teams[side]
            target = pos(cmd["targetPos"][0]) if cmd.get("targetPos") else None
            name = cmd.get("name", "")
            bag = u["backpack"]
            success = True
            if action == "move":
                intents[uid] = target
            elif action == "attack":
                hits = damage(u, [pos(p) for p in cmd["targetPos"]], list(self.robots.values()))
                for rid, amount in hits.items():
                    robot_damage[side][rid] += amount
                success = bool(hits)
                if u["roleType"] == "rocket":
                    self.cooldowns[uid] = self.round + 4
                    fired.add(uid)
            elif action == "build":
                if name == "wall":
                    bag.remove("stone")
                else:
                    team["goldNum"] -= 25
                self._new_building(side, name, target)
            elif action == "remove":
                wall = next(v for v in self.living(side) if v["roleType"] == "wall" and pos(v["pos"]) == target)
                wall["health"] = 0
            elif action == "collect":
                mineral = self.zones.get(target)
                if mineral in self.closed_minerals:
                    success = False
                else:
                    bag.append(mineral)
                    collected[target] += 1
            elif action == "sell":
                num = cmd.get("num", 1)
                for _ in range(num):
                    bag.remove(name)
                team["goldNum"] += self.vendor_prices[name] * num
            elif action == "buy":
                num = cmd.get("num", 1)
                bag.extend([name] * num)
                team["goldNum"] -= SHOP[name] * num
            elif action == "drop":
                bag.remove(name)
            elif action in ("acceptTask", "submitAnswer"):
                success = self._task_action(side, u, cmd)
            elif action == "summonTreasure":
                for item in cmd["item"]:
                    bag.remove(item)
                treasure_attempts.append((side, target, cmd["item"]))
            elif action == "use":
                if name in UPGRADES or name == "WallFixer":
                    building = next(v for v in self.living(side) if target in footprint(v))
                    if name in UPGRADES:
                        # Another role may already have upgraded this building this round.
                        if building["level"] != UPGRADES[name][1]:
                            success = False
                        else:
                            building["level"] += 1
                            if building["roleType"] in WEAPONS:
                                building["attackRange"] = reach(building)
                    if success:
                        building["health"] = max_health(building["roleType"], building["level"])
                elif name == "Medicine":
                    u["health"] = max_health(u["roleType"])
                elif name in ("Bomb", "DizzyWeapon"):
                    for rid, robot in self.robots.items():
                        if distance(target, pos(robot["pos"])) <= 1:
                            if name == "Bomb":
                                robot_damage[side][rid] += 100
                            else:
                                self.stuns[rid] = self.round + 5
                elif name in SUMMONS:
                    enemy = SIDES[1 - SIDES.index(side)]
                    self.queued_summons[enemy].append(SUMMONS[name])
                    self.summon_counts[side] += 1
                if success:
                    bag.remove(name)
            if not success:
                self.feedback[side]["lastRoundRoleActionResults"][str(uid)] = False
                self.failures[side] += 1
                self.last_events.append({"team": side, "id": uid, "failure": "execution had no effect"})
        for side, target, items in treasure_attempts:
            t = self.treasure
            if t["empty"]:
                code = 4
            elif target != t["pos"] or not t["start"] <= self.round <= t["end"]:
                code = 2
            elif Counter(items) != Counter(t["items"]):
                code = 3
            else:
                code = 1
                self.score[side]["task"] += t["score"]
                self.teams[side]["goldNum"] += t["gold"]
            self.feedback[side]["lastSummonTreasureResult"] = code
        if any(self.feedback[s]["lastSummonTreasureResult"] == 1 for s in SIDES):
            self.treasure["empty"] = True
        robot_intents, attacks = self._robot_intents()
        intents.update(robot_intents)
        movers = {u["id"]: u for side in SIDES for u in self.living(side) if u["roleType"] in ACTORS}
        movers.update(self.robots)
        positions = {uid: pos(u["pos"]) for uid, u in movers.items()}
        static = set(self.zones)
        for side in SIDES:
            for u in self.living(side):
                if u["roleType"] not in ACTORS:
                    static.update(footprint(u))
        moved, failed = simultaneous_moves(positions, intents, static)
        for uid, cell in moved.items():
            movers[uid]["pos"] = xy(cell)
        for side in SIDES:
            for uid in failed:
                if str(uid) in self.feedback[side]["lastRoundRoleActionResults"]:
                    self.feedback[side]["lastRoundRoleActionResults"][str(uid)] = False
                    self.failures[side] += 1
        # Damage is applied only now: lethally hit robots still acted this round.
        for uid, amount in attacks:
            self.unit(uid)["health"] -= amount
        for rid, robot in list(self.robots.items()):
            total = sum(robot_damage[s][rid] for s in SIDES)
            robot["health"] -= total
            if robot["health"] <= 0:
                contributions = {s: robot_damage[s][rid] for s in SIDES}
                high = max(contributions.values())
                winners = [s for s in SIDES if contributions[s] == high]
                for side in winners:
                    self.score[side]["kill"] += Fraction(ROBOT_STATS[robot["roleType"]][2], len(winners))
                del self.robots[rid]
        for side in SIDES:
            for u in self.teams[side]["roles"]:
                if u["health"] <= 0:
                    u["health"] = 0
                    if u["roleType"] in ACTORS and u["id"] not in self.revivals:
                        self.revivals[u["id"]] = ((self.round - 1) // 130 + 1) * 130 + 21
                    if u["roleType"] == "station" and self.destroyed_at[side] is None:
                        self.destroyed_at[side] = self.round
            active = self.task_state[side]
            if active:
                pioneer = self.unit(active["actor"])
                if pioneer["health"] <= 0 or not any(distance(pos(pioneer["pos"]), c) == 1 for c in active["cells"]):
                    self._end_task(side, False, self.round)
            if self.round % 130 == 0 and self.base(side)["health"] > 0:
                self.score[side]["survival"] += 10 * (self.round // 130)
        refresh = []
        for cell, count in collected.items():
            self.mines[cell] -= count
            if self.mines[cell] <= 0:
                refresh.append(self.zones.pop(cell))
                del self.mines[cell]
        self.round += 1
        for mineral in refresh:
            self._spawn_mine(mineral)
        if not self.finished:
            self.prepare()

    @property
    def finished(self):
        return (self.round > 1300 or all(self.base(s)["health"] <= 0 for s in SIDES)
                or all(self.exceptions[s] >= 5 for s in SIDES))

    def result(self):
        scores = {s: float(sum(self.score[s].values())) for s in SIDES}
        dead = self.destroyed_at
        winner = None
        if self.finished:
            if all(dead[s] is not None for s in SIDES) and dead[SIDES[0]] != dead[SIDES[1]]:
                winner = max(SIDES, key=lambda s: dead[s])
            elif sum(dead[s] is not None for s in SIDES) == 1:
                # A08: survivor wins; not fully enumerated by the task book.
                winner = next(s for s in SIDES if dead[s] is None)
            elif scores[SIDES[0]] != scores[SIDES[1]]:
                winner = max(SIDES, key=lambda s: scores[s])
            else:
                winner = "draw"
        return {"seed": self.seed, "pressure": self.pressure, "rounds": self.round - 1,
                "profile": self.profile,
                "unknownDay9And10": self.unknown_waves if self.profile == "observed" else "legacy formula",
                "complete": self.finished, "localWinner": winner, "scores": scores,
                "scoreBreakdown": {s: {k: float(v) for k, v in self.score[s].items()} for s in SIDES},
                "baseHealth": {s: self.base(s)["health"] for s in SIDES},
                "destroyedAt": dict(dead), "exceptions": dict(self.exceptions),
                "executionFailures": dict(self.failures), "actions": {s: dict(self.action_counts[s]) for s in SIDES}}
