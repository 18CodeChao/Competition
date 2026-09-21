"""Independent regressions for user platform observations, 2026-09-22."""
from copy import deepcopy
import io
import json
import unittest

from agent.combat import choose_targets, damage
from agent.layout import plan_layout
from agent.policy import Agent
from agent.rules import xy, pos, command
from agent.tasks import TaskMemory, verified_command_answer
from agent.telemetry import StreamJournal, read_records
from agent.world import World
from local_judge.engine import Game, make_unit


def robot(uid, x, y, side="challenger", hp=500):
    return {"id": uid, "roleType": "largeRobot", "pos": xy((x, y)), "health": hp,
            "targetTeam": side, "abnormalState": ""}


def stationed():
    g = Game(profile="observed", pressure=0)
    g.round = 71
    for i, cell in enumerate(((9, 20), (8, 20), (8, 22))):
        g.teams["challenger"]["roles"].append(make_unit(10040 + i, "rocket", cell, 3))
    g.unit(10010)["pos"] = xy((8, 21))
    g.unit(10012)["pos"] = xy((4, 4))
    g.unit(10011)["pos"] = xy((13, 14))
    return g


class PlatformTests(unittest.TestCase):
    def test_exact_layout_both_sides(self):
        g = Game(profile="observed", pressure=0)
        for side, hub, guns in (("challenger", (8, 21), {(9, 20), (8, 20), (8, 22)}),
                                ("defender", (32, 10), {(31, 11), (32, 11), (32, 9)})):
            layout = plan_layout(World(g.observation(side)))
            self.assertEqual(layout["hub"], hub)
            self.assertEqual(set(layout["guns"]), guns)

    def test_filters_opponent_even_with_global_range_and_larger_cluster(self):
        gun = make_unit(1, "rocket", (8, 20), 3)
        base = make_unit(2, "station", (9, 22))
        enemies = [robot(30 + i, 25 + i % 3, 5 + i // 3, "defender") for i in range(9)]
        own = robot(9, 17, 22)
        targets = choose_targets(gun, enemies + [own], base, "challenger", {})
        hits = damage(gun, targets, enemies + [own])
        self.assertTrue(targets)
        self.assertTrue(all(p[1] >= 15 for p in targets))
        self.assertEqual(set(hits), {9})
        self.assertEqual(choose_targets(gun, enemies, base, "challenger", {}), [])

    def test_boundary_splash_and_target_team(self):
        for base_cell, side, own_y, forbidden_y in (((9, 22), "challenger", 15, 14),
                                                    ((30, 10), "defender", 15, 16)):
            base = make_unit(1, "station", base_cell)
            gun = make_unit(2, "rocket", (8, 20), 3)
            other = "defender" if side == "challenger" else "challenger"
            robots = [robot(3, 20, own_y, side), robot(4, 20, forbidden_y, other),
                      robot(5, 21, own_y, other)]
            targets = choose_targets(gun, robots, base, side, {})
            self.assertTrue(targets)
            self.assertEqual(set(damage(gun, targets, robots)), {3})

    def test_gunner_fires_under_direct_attack_before_healing_or_upgrading(self):
        g = stationed()
        g.unit(10010)["health"] = 20
        g.unit(10010)["backpack"] = ["Medicine", "StationUpgradeVoucher1"]
        next(u for u in g.teams["challenger"]["roles"] if u["roleType"] == "station")["health"] = 100
        g.robots[30001] = robot(30001, 6, 21)
        agent = Agent()
        answer = agent.decide(g.observation("challenger"))["roleCommandMap"]
        attacks = [c for c in answer.values() if c["action"] == "attack"]
        self.assertEqual(len(attacks), 1)
        self.assertEqual(attacks[0]["controllerId"], "10010")
        self.assertNotIn("10010", answer)
        self.assertFalse(any(e["kind"] == "retreat" and e["role"] == 10010 for e in agent.events))

    def test_cooldown_holds_position_and_never_recruits_pioneer(self):
        g = stationed()
        g.robots[30001] = robot(30001, 6, 21)
        for u in g.teams["challenger"]["roles"]:
            if u["roleType"] == "rocket":
                u["cooldown"] = 2
        answer = Agent().decide(g.observation("challenger"))["roleCommandMap"]
        self.assertNotIn("10010", answer)
        g.unit(10010)["health"] = g.unit(10012)["health"] = 0
        g.unit(10011)["pos"] = xy((8, 21))
        for u in g.teams["challenger"]["roles"]:
            if u["roleType"] == "rocket":
                u["cooldown"] = 0
        answer = Agent().decide(g.observation("challenger"))["roleCommandMap"]
        self.assertFalse(any(c["action"] == "attack" for c in answer.values()))

    def test_gunner_return_can_cross_risk_margin(self):
        g = stationed()
        g.unit(10010)["pos"] = xy((7, 21))
        g.robots[30001] = robot(30001, 4, 21)
        for u in g.teams["challenger"]["roles"]:
            if u["roleType"] == "rocket":
                u["cooldown"] = 2
        answer = Agent().decide(g.observation("challenger"))["roleCommandMap"]
        self.assertEqual(pos(answer["10010"]["targetPos"][0]), (8, 21))

    def test_occupied_task_takes_priority_over_remote_maintenance(self):
        g = stationed()
        g.round = 69
        g.step({"challenger": {"roleCommandMap": {"10011": command("acceptTask")}}})
        g.unit(10011)["backpack"] = ["StationUpgradeVoucher1"]
        next(u for u in g.teams["challenger"]["roles"] if u["roleType"] == "station")["health"] = 100
        g.round = 71
        g.robots[30001] = robot(30001, 13, 17)
        answer = Agent().decide(g.observation("challenger"))["roleCommandMap"]
        self.assertEqual(answer["10011"]["action"], "submitAnswer")

    def test_command_direct_answer_rejects_error_truncation_and_untagged(self):
        self.assertEqual(verified_command_answer('[exitCode:0]\n{"competitionAnswer":{"answer":42}}'), '{"answer": 42}')
        for text in ('[exitCode:1]\n{"competitionAnswer":42}', '[TIMEOUT]\n{"competitionAnswer":42}',
                     '[exitCode:0]\n{"answer":42}', '[exitCode:0]\n{"competitionAnswer":42}\n[TRUNCATED]'):
            self.assertIsNone(verified_command_answer(text))

    def test_direct_sandbox_answer_pipeline_saves_one_llm_round(self):
        g = stationed()
        g.round = 1
        g.task_specs["challenger"][0][0]["description"] = "Read the documented sandbox fixture."
        g.task_specs["challenger"][0][0]["answer"] = {"answer": 42}
        g.step({"challenger": {"roleCommandMap": {"10011": command("acceptTask")}}})
        a = Agent()
        response = a.decide(g.observation("challenger"))
        g.services.prompts[response["prompt"]] = json.dumps({"executeCmd": "fixture", "answerFromCommand": True, "skill": "validated API SOP"})
        g.services.commands["fixture"] = '[exitCode:0]\n{"competitionAnswer":{"answer":42}}'
        g.step({"challenger": response})
        response = a.decide(g.observation("challenger"))
        self.assertEqual(a.memory.skills, [])
        g.step({"challenger": response})
        response = a.decide(g.observation("challenger"))
        self.assertEqual(response["roleCommandMap"]["10011"]["action"], "submitAnswer")
        self.assertEqual(response["prompt"], "")
        g.step({"challenger": response})
        a.decide(g.observation("challenger"))
        self.assertEqual(a.memory.skills, ["validated API SOP"])

    def test_rejected_structured_answer_does_not_override_corrected_model_answer(self):
        g = stationed()
        g.round = 1
        g.step({"challenger": {"roleCommandMap": {"10011": command("acceptTask")}}})
        a = Agent()
        first = a.decide(g.observation("challenger"))
        payload = g.observation("challenger")
        payload["roundNo"] += 1
        payload["errors"] = [{"errorCode": 2, "description": "wrong answer"}]
        payload["lastRoundRoleActionResults"] = {"10011": True}
        second = a.decide(payload)
        self.assertTrue(second["prompt"])
        payload["roundNo"] += 1
        payload["errors"] = []
        payload["llmResp"] = '{"answer":"corrected"}'
        third = a.decide(payload)
        self.assertEqual(third["roleCommandMap"]["10011"]["taskAnswer"], "corrected")

    def test_platform_stream_roundtrip_prefix_chunk_loss_and_no_files(self):
        output = io.StringIO()
        journal = StreamJournal(output, chunk_chars=256)
        payload = stationed().observation("challenger")
        response = Agent().decide(payload)
        journal.record(payload, response, {"example": "中文" * 5000}, 1.5)
        journal.close()
        lines = output.getvalue().splitlines()
        stats = {}
        records = list(read_records(io.StringIO("\n".join("[platform] " + line for line in lines)), stats))
        self.assertEqual(records[-1]["request"], payload)
        self.assertEqual(records[-1]["response"], response)
        self.assertIn("map", records[-1])
        self.assertEqual(stats["incompleteEvents"], 0)
        stats = {}
        list(read_records(io.StringIO("\n".join(lines[:-1])), stats))
        self.assertEqual(stats["incompleteEvents"], 1)

    def test_enemy_global_rocket_is_soft_risk_not_impassable_world(self):
        g = stationed()
        payload = g.observation("challenger")
        payload["teamEnemy"]["roles"].append(make_unit(900, "rocket", (20, 20), 3))
        w = World(payload)
        self.assertGreater(w.enemy_fire_risk((4, 5)), 0)
        self.assertIsNotNone(w.route(w.units[10012], {(4, 5)}))

    def test_failed_move_replans_instead_of_repeating_collision(self):
        g = stationed()
        g.round = 1
        a = Agent()
        first = a.decide(g.observation("challenger"))
        uid, cmd = next((uid, c) for uid, c in first["roleCommandMap"].items() if c["action"] == "move")
        payload = g.observation("challenger")
        payload["roundNo"] = 2
        payload["lastRoundRoleActionResults"] = {uid: False}
        second = a.decide(payload)["roleCommandMap"].get(uid)
        self.assertTrue(second is None or second.get("targetPos") != cmd["targetPos"])


if __name__ == "__main__":
    unittest.main()
