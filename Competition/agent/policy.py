"""Strategy v2: observed-wave tactics and cross-round task adapter."""
from collections import Counter
import json

from .audit import eligible
from .rules import WEAPONS, pos, distance, footprint, command, max_health, inside
from .tasks import TaskMemory, structured_answer
from .strategy import AdaptiveStrategy
from .intelligence import Intelligence


class Agent(AdaptiveStrategy):
    def __init__(self, loadout=("rocket", "rocket", "rocket")):
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
        self.intelligence = Intelligence()
        self.layout = None
        self.gunner_id = None
        self.trace = {}


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
