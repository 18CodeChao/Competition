from collections import Counter
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest

from agent.combat import choose_targets, target_metrics
from agent.intelligence import Intelligence
from agent.layout import plan_layout, front_walls, rear_access
from agent.policy import Agent
from agent.rules import pos, xy, distance, footprint, command
from agent.telemetry import MatchJournal, ascii_map
from agent.world import World
from local_judge.analyze import analyze
from local_judge.engine import Game, make_unit
from local_judge.profiles import observed_wave


def robot(uid, cell, hp=40, side="challenger"):
    return {"id": uid, "roleType": "smallRobot", "pos": xy(cell), "health": hp,
            "targetTeam": side, "abnormalState": ""}


class ExperienceTests(unittest.TestCase):
    def test_mirrored_cluster_has_shared_hub_and_rear_exit(self):
        game = Game(profile="observed", pressure=0)
        for side in ("challenger", "defender"):
            w = World(game.observation(side))
            layout = plan_layout(w)
            self.assertEqual(len(layout["guns"]), 3)
            self.assertTrue(all(distance(layout["hub"], p) == 1 for p in layout["guns"]))
            self.assertNotIn(layout["hub"], layout["guns"])
            self.assertEqual(len(layout["walls"]), 10)
            self.assertTrue(rear_access(layout["hub"], w.base, set(layout["guns"]) | set(layout["walls"]) | footprint(w.base)))
            x = pos(w.base["pos"])[0]
            self.assertTrue(all(p[0] > x for p in layout["walls"]) if x < 20 else all(p[0] <= x for p in layout["walls"]))

    def test_real_time_empty_center_beats_any_robot_center(self):
        gun = make_unit(1, "rocket", (2, 5))
        robots = [robot(10+i, cell) for i, cell in enumerate(((5, 4), (7, 4), (5, 6), (7, 6)))]
        targets = choose_targets(gun, robots, None, "challenger", {})
        self.assertEqual(targets, [(6, 5)])
        self.assertEqual(target_metrics(gun, targets, robots)["effectiveDamage"], 40)

    def test_level_three_joint_targets_avoid_wasted_damage(self):
        gun = make_unit(1, "rocket", (2, 5), 3)
        robots = [robot(10+i, cell, 20) for i, cell in enumerate(((5, 5), (12, 5), (19, 5)))]
        targets = choose_targets(gun, robots, None, "challenger", {})
        self.assertEqual(set(targets), {(5, 5), (12, 5), (19, 5)})
        self.assertEqual(target_metrics(gun, targets, robots)["overkill"], 0)

    def clustered_game(self):
        g = Game(profile="observed", pressure=0)
        w = World(g.observation("challenger"))
        layout = plan_layout(w)
        for i, cell in enumerate(layout["guns"]):
            g.teams["challenger"]["roles"].append(make_unit(10040+i, "rocket", cell))
        g.unit(10010)["pos"] = xy(layout["hub"])
        g.unit(10012)["pos"] = xy((3, 3))
        g.unit(10011)["pos"] = xy((13, 14))
        g.round = 71
        g.robots[30001] = robot(30001, (18, 22), 500)
        return g, layout

    def test_one_gunner_rotates_three_weapons_with_legal_cooldown(self):
        g, layout = self.clustered_game()
        agent = Agent()
        agent.gunner_id = 10010
        shots = []
        for _ in range(4):
            answer = agent.decide(g.observation("challenger"))
            attacks = [(uid, cmd) for uid, cmd in answer["roleCommandMap"].items() if cmd["action"] == "attack"]
            self.assertLessEqual(len(attacks), 1)
            if attacks:
                uid, cmd = attacks[0]
                self.assertEqual(cmd["controllerId"], "10010")
                self.assertNotIn("10010", answer["roleCommandMap"])
                shots.append(uid)
            g.step({"challenger": answer})
        self.assertEqual(len(shots), 3)
        self.assertEqual(len(set(shots)), 3)
        self.assertEqual(pos(g.unit(10010)["pos"]), layout["hub"])

    def test_night_pioneer_still_submits_task(self):
        g, _ = self.clustered_game()
        g.round = 69
        g.step({"challenger": {"roleCommandMap": {"10011": command("acceptTask")}}})
        g.round = 71
        answer = Agent().decide(g.observation("challenger"))
        self.assertEqual(answer["roleCommandMap"]["10011"]["action"], "submitAnswer")

    def test_night_path_uses_directional_risk_without_radius_exclusion(self):
        g = Game(pressure=0)
        g.round = 71
        g.unit(10012)["pos"] = xy((2, 2))
        g.robots[30001] = robot(30001, (6, 6))
        w = World(g.observation("challenger"))
        # v4: risk is a soft cost; an otherwise reachable goal is not banned by radius.
        self.assertIsNotNone(w.route(w.units[10012], {(6, 7)}))
        path = w.route(w.units[10012], {(12, 2)})
        self.assertIsNotNone(path)
        self.assertNotIn((6, 6), path)
        self.assertIsNotNone(w.route(w.units[10012], {(6, 4)}))  # Close, but behind its travel direction.

    def test_stone_batch_keeps_mining_instead_of_far_return(self):
        g, _ = self.clustered_game()
        g.round = 10
        g.unit(10010)["pos"] = xy((4, 4))
        g.unit(10010)["backpack"] = ["stone"]
        g.zones[(4, 5)] = "stone"
        g.mines[(4, 5)] = 10
        answer = Agent().decide(g.observation("challenger"))
        self.assertEqual(answer["roleCommandMap"]["10010"]["action"], "collect")

    def test_base_voucher_waits_then_heals_but_wall_fixer_cannot_heal_base(self):
        g, _ = self.clustered_game()
        g.round = 20
        g.unit(10012)["pos"] = xy((8, 22))
        g.unit(10012)["backpack"] = ["StationUpgradeVoucher1", "WallFixer"]
        agent = Agent()
        answer = agent.decide(g.observation("challenger"))
        self.assertNotEqual(answer["roleCommandMap"].get("10012", {}).get("action"), "use")
        g.base("challenger")["health"] = 500
        g.round = 21
        answer = agent.decide(g.observation("challenger"))
        self.assertEqual(answer["roleCommandMap"]["10012"]["name"], "StationUpgradeVoucher1")
        g.step({"challenger": answer})
        self.assertEqual(g.base("challenger")["health"], 3000)

    def test_enemy_memory_is_stale_not_current_visibility(self):
        g = Game(pressure=0)
        memory = Intelligence()
        gun = make_unit(20040, "rocket", (13, 24), 2)
        g.teams["defender"]["roles"].append(gun)
        memory.update(World(g.observation("challenger")))
        g.unit(10010)["pos"] = xy((0, 0))
        gun["pos"] = xy((30, 8))
        g.round = 2
        memory.update(World(g.observation("challenger")))
        self.assertFalse(memory.enemies[20040]["visible"])
        self.assertEqual(memory.enemies[20040]["lastSeen"], 1)
        self.assertEqual(memory.enemies[20040]["unit"]["level"], 2)

    def test_exact_eight_observed_waves_and_correct_base(self):
        self.assertEqual(observed_wave(1), (30, 5, 0, 0))
        self.assertEqual(observed_wave(8), (59, 29, 5, 3))
        self.assertNotEqual(observed_wave(9, "growth"), observed_wave(9, "hold8"))
        g = Game(profile="observed")
        self.assertEqual(footprint(g.base("challenger")), {(9,22),(10,22),(9,21),(10,21)})
        g.round = 71
        g.prepare()
        for side in ("challenger", "defender"):
            group = [r for r in g.robots.values() if r["targetTeam"] == side]
            self.assertEqual(Counter(r["roleType"] for r in group), {"smallRobot":30, "middleRobot":5})
            x = pos(g.base(side)["pos"])[0]
            self.assertTrue(all(pos(r["pos"])[0] > x for r in group) if x < 20 else all(pos(r["pos"])[0] < x for r in group))
        self.assertEqual(sum(len(v) for v in g.task_specs["challenger"]), 6)
        self.assertEqual(g.task_specs["challenger"][0][0]["gold"], 80)
        self.assertEqual(g.treasure["gold"], 160)

    def test_journal_ascii_replay_and_analysis(self):
        g = Game(profile="observed", pressure=0)
        request = g.observation("challenger")
        agent = Agent()
        answer = agent.decide(request)
        with tempfile.TemporaryDirectory() as folder:
            journal = MatchJournal(folder)
            journal.record(request, answer, agent.trace, 12.5)
            journal.close()
            path = next(Path(folder).glob('*.jsonl'))
            records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(records[1]["request"], request)
            self.assertEqual(records[1]["response"], answer)
            self.assertTrue(records[1]["map"].startswith("31 "))
            self.assertEqual(len(records[1]["map"].splitlines()[0][3:]), 41)
            summary, maps = analyze(path, 1, "challenger")
            self.assertEqual(summary["turns"], 1)
            self.assertEqual(summary["maxLatencyMs"], 12.5)
            self.assertEqual(len(maps), 1)

    def test_price_hoarding_needs_evidence_and_cash_reserve(self):
        g = Game(pressure=0)
        agent = Agent()
        agent.decide(g.observation("challenger"))
        agent.memory.price_forecasts = [{"name":"iron","startDay":2,"endDay":3,
            "direction":"up","confidence":.9,"evidence":"明天铁矿停采，价格上涨"}]
        agent.ledger.gold = 100
        self.assertTrue(agent.hold_mineral("iron", agent.w.units[10010]))
        agent.ledger.gold = 0
        self.assertFalse(agent.hold_mineral("iron", agent.w.units[10010]))

    def test_repair_worker_uses_voucher_from_outside_robot_range(self):
        g = Game(profile="observed", pressure=0)
        g.round = 71
        wall = make_unit(40000, "wall", (12, 22))
        wall["health"] = 300
        g.teams["challenger"]["roles"].append(wall)
        g.unit(10012)["pos"] = xy((11, 22))
        g.unit(10012)["backpack"] = ["WallUpgradeVoucher1"]
        g.robots[30001] = robot(30001, (15, 22))
        answer = Agent().decide(g.observation("challenger"))
        self.assertEqual(answer["roleCommandMap"]["10012"]["action"], "use")
        self.assertEqual(answer["roleCommandMap"]["10012"]["name"], "WallUpgradeVoucher1")

    def test_wall_procurement_upgrades_before_max_level_repair(self):
        for level, expected in ((1, "WallUpgradeVoucher1"), (3, "WallFixer")):
            with self.subTest(level=level):
                g, _ = self.clustered_game()
                g.round = 20
                g.unit(10012)["pos"] = xy((24, 20))
                wall = make_unit(40000, "wall", (12, 22), level)
                wall["health"] = 200
                g.teams["challenger"]["roles"].append(wall)
                g.teams["challenger"]["goldNum"] = 200
                agent = Agent()
                answer = agent.decide(g.observation("challenger"))
                self.assertEqual(answer["roleCommandMap"]["10012"]["name"], expected)


if __name__ == "__main__":
    unittest.main()
