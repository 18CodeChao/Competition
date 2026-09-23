"""Strategy v2: observed-wave tactics and cross-round task adapter."""
from collections import Counter
import json

from .audit import eligible
from .rules import WEAPONS, pos, distance, footprint, command, max_health, inside
from .tasks import TaskMemory, structured_answer, usable_answer
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
        self.failed_moves = {}
        self.previous_commands = {}
        self.intelligence = Intelligence()
        self.layout = None
        self.gunner_id = None
        self.trace = {}
        self.previous_actor_hp = {}


    def emit(self, unit, cmd):
        if (self.w.phase and unit['roleType'] == 'pioneer' and cmd['action'] == 'move'
                and pos(cmd['targetPos'][0]) not in self.memory.task_area(self.w)):
            self.diagnostics.append({'id': unit['id'], 'reason': '任务期间禁止移出已领取任务点范围'})
            return False
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
        probe = self.memory.probe_command(self.w)
        if probe:
            self.response["executeCmd"] = probe
            self.memory.history.append({"stage": "readTaskDocument", "command": probe})
            self.events.append({"kind": "taskProbe", "role": actor["id"]})
            return
        answer = self.memory.command_answer or self.memory.deferred_answer
        if reply and not reply.get('executeCmd') and isinstance(reply.get("answer"), (str, dict, list, int, float)):
            answer = reply["answer"]
            if not isinstance(answer, str):
                answer = json.dumps(answer, ensure_ascii=False)
        if answer is None:
            answer = structured_answer(self.w.phase)
        if answer is not None and not usable_answer(answer):
            self.memory.history.append({'localFeedback': '拒绝空值、占位或诊断答案；请读取缺失资料并继续求解', 'rejectedCandidate': answer})
            self.events.append({'kind': 'answerBlocked', 'reason': '答案仅含占位值或诊断信息', 'answer': answer})
            self.memory.deferred_answer = None
            answer = None
        if answer is not None and answer != self.memory.submitted and answer not in self.memory.rejected:
            self.memory.deferred_answer = answer
            if actor['id'] not in self.ledger.used and self.emit(actor, command("submitAnswer", taskAnswer=answer)):
                self.memory.deferred_answer = None
                self.memory.submitted = answer
                self.memory.submitted_round = self.w.round
                self.memory.history.append({"submitted": answer})
                self.events.append({"kind": "taskSubmit", "role": actor["id"], "answer": answer})
                return
            if actor['id'] in self.ledger.used:
                return  # Keep the validated candidate while sidestepping/healing this turn.
        if reply and isinstance(reply.get("executeCmd"), str) and reply["executeCmd"]:
            cmd = reply["executeCmd"]
            self.memory.command_repeats = self.memory.command_repeats + 1 if cmd == self.memory.last_command else 1
            self.memory.last_command = cmd
            if self.memory.command_repeats > 2:
                self.memory.history.append({"localFeedback": "重复命令已运行两次，请利用已有结果或修正方案"})
                self.response["prompt"] = self.memory.task_prompt(self.w)
                return
            # Forward only; the participant process NEVER invokes a shell.
            self.response["executeCmd"] = reply["executeCmd"]
            self.memory.command_answer_pending = reply.get("answerFromCommand") is True
            self.memory.command_format = reply.get("answerFormat")
            self.memory.history.append({"executed": reply["executeCmd"]})
        else:
            self.response["prompt"] = self.memory.task_prompt(self.w)

    def treasure(self, actor):
        t = self.memory.treasure
        if not t or self.memory.treasure_done:
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
                    self.memory.treasure_pending = (self.w.round, signature)
                return success
            return True
        return self.walk(actor, {target})
