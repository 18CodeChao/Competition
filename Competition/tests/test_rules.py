import json
import unittest
from copy import deepcopy

from agent.audit import Ledger, eligible
from agent.combat import damage, cone_ok
from agent.policy import Agent
from agent.protocol import schema_errors, loads, empty_response
from agent.rules import build_ring, command, footprint, pos, xy, reach
from agent.world import World
from local_judge.engine import Game, make_unit, simultaneous_moves, ReplayServices


def response(uid, cmd):
    return {"roleCommandMap": {str(uid): cmd}, "prompt": "", "executeCmd": ""}


class RulesTests(unittest.TestCase):
    def test_picture_construction_masks_and_base_origin(self):
        base = make_unit(1, "station", (10, 24))
        self.assertEqual(footprint(base), {(10, 24), (11, 24), (10, 23), (11, 23)})
        self.assertEqual(len(build_ring(base, 1)), 12)
        self.assertEqual(len(build_ring(base, 2)), 20)
        self.assertIn((9, 25), build_ring(base, 1))
        self.assertIn((8, 26), build_ring(base, 2))
        self.assertNotIn((8, 26), build_ring(base, 1))

    def test_task_book_ranges_override_sample(self):
        for kind, expected in (("gatling", 3), ("railgun", 6), ("rocket", 10)):
            u = make_unit(1, kind, (4, 4))
            u["attackRange"] = 2147483647
            self.assertEqual(reach(u), expected)

    def test_railgun_energy_conservation(self):
        tower = make_unit(1, "railgun", (0, 0))
        robots = [{"id": 2, "pos": xy((1, 0)), "health": 6},
                  {"id": 3, "pos": xy((2, 0)), "health": 20}]
        self.assertEqual(damage(tower, [(3, 0)], robots), {2: 6, 3: 4})

    def test_gatling_nearest_and_cone(self):
        tower = make_unit(1, "gatling", (4, 4))
        robots = [{"id": 2, "pos": xy((5, 4)), "health": 1},
                  {"id": 3, "pos": xy((6, 4)), "health": 100}]
        self.assertEqual(damage(tower, [(7, 4)], robots), {2: 10})
        self.assertTrue(cone_ok((0, 0), [(1, 0), (0, 1)]))
        self.assertFalse(cone_ok((0, 0), [(1, 0), (-1, 1)]))

    def test_rocket_overlap_damage(self):
        tower = make_unit(1, "rocket", (0, 0), 2)
        robots = [{"id": 2, "pos": xy((5, 5)), "health": 100},
                  {"id": 3, "pos": xy((6, 6)), "health": 100},
                  {"id": 4, "pos": xy((7, 5)), "health": 100}]
        self.assertEqual(damage(tower, [(5, 5), (5, 5)], robots), {2: 40, 3: 20})

    def test_simultaneous_collision_and_chain(self):
        positions = {1: (0, 0), 2: (1, 0), 3: (2, 0)}
        moved, failed = simultaneous_moves(positions, {1: (1, 0), 2: (0, 0)}, set())
        self.assertEqual(moved, {})
        self.assertEqual(failed, {1, 2})
        moved, failed = simultaneous_moves(positions, {1: (1, 0), 2: (2, 0)}, set())
        self.assertEqual(failed, {1, 2})
        moved, failed = simultaneous_moves(positions, {1: (1, 0), 2: (2, 0), 3: (3, 0)}, set())
        self.assertEqual(len(moved), 3)
        moved, failed = simultaneous_moves(positions, {1: (1, 1), 2: (1, 1)}, set())
        self.assertEqual(failed, {1, 2})

    def test_diagonal_and_same_goal_path(self):
        g = Game(pressure=0)
        a = g.unit(10010)
        a["pos"] = xy((1, 1))
        g.zones[(1, 2)] = "stone"
        g.zones[(2, 1)] = "iron"
        w = World(g.observation("challenger"))
        self.assertEqual(w.route(w.units[10010], {(2, 2)}), [(2, 2)])
        self.assertEqual(w.route(w.units[10010], {(1, 1)}), [])

    def test_strict_protocol(self):
        self.assertTrue(schema_errors(response(1, {"action": "move"})))
        self.assertTrue(schema_errors(response(1, command("attack", [(2, 2)], controllerId=2))))
        self.assertFalse(schema_errors(response(1, command("use", name="Medicine"))))
        self.assertTrue(schema_errors(response(1, command("use", name="Bomb"))))
        with self.assertRaises(ValueError):
            loads('{"roleCommandMap":{"1":{},"1":{}}}')

    def test_start_gold_cannot_spend_sale_revenue(self):
        g = Game(pressure=0)
        g.teams["challenger"]["goldNum"] = 0
        g.unit(10010)["pos"] = xy((19, 16))
        g.unit(10010)["backpack"] = ["copper"] * 5
        g.unit(10012)["pos"] = xy((9, 23))
        g.step({"challenger": {"roleCommandMap": {
            "10010": command("sell", name="copper", num=5),
            "10012": command("build", [(9, 22)], name="rocket")}}})
        self.assertEqual(g.teams["challenger"]["goldNum"], 25)
        self.assertFalse(g.feedback["challenger"]["lastRoundRoleActionResults"]["10012"])
        self.assertEqual(g.exceptions["challenger"], 0)

    def test_shared_gold_and_controller_reservation(self):
        g = Game(pressure=0)
        g.teams["challenger"]["goldNum"] = 25
        g.unit(10010)["pos"] = xy((8, 24))
        g.unit(10012)["pos"] = xy((12, 24))
        w = World(g.observation("challenger"))
        ledger = Ledger(w)
        self.assertIsNone(eligible(w, 10010, command("build", [(9, 24)], name="rocket"), ledger))
        self.assertIsNotNone(eligible(w, 10012, command("build", [(12, 23)], name="rocket"), ledger))
        g.round = 71
        g.teams["challenger"]["roles"].append(make_unit(10040, "rocket", (9, 24)))
        w = World(g.observation("challenger"))
        ledger = Ledger(w)
        self.assertIsNone(eligible(w, 10040, command("attack", [(5, 24)], controllerId="10010"), ledger))
        self.assertIsNotNone(eligible(w, 10010, command("move", [(8, 23)]), ledger))

    def test_three_weapons_total(self):
        g = Game(pressure=0)
        g.teams["challenger"]["roles"].extend([
            make_unit(10020, "gatling", (10, 25)), make_unit(10030, "railgun", (11, 25)),
            make_unit(10040, "rocket", (12, 25))])
        g.unit(10010)["pos"] = xy((8, 24))
        w = World(g.observation("challenger"))
        self.assertIsNotNone(eligible(w, 10010, command("build", [(9, 24)], name="rocket"), Ledger(w)))

    def test_empty_attack_is_failure_not_exception_and_cooldown(self):
        g = Game(pressure=0)
        g.round = 71
        g.teams["challenger"]["roles"].append(make_unit(10040, "rocket", (10, 25)))
        shot = response(10040, command("attack", [(8, 25)], controllerId="10010"))
        g.step({"challenger": shot})
        self.assertEqual(g.exceptions["challenger"], 0)
        self.assertEqual(g.unit(10040)["cooldown"], 3)
        for expected in (2, 1, 0):
            g.step({})
            self.assertEqual(g.unit(10040)["cooldown"], expected)

    def test_lethally_hit_robot_still_attacks_before_end_damage(self):
        g = Game(pressure=0)
        g.round = 71
        g.teams["challenger"]["roles"].append(make_unit(10040, "rocket", (10, 25)))
        g.robots[30001] = {"id": 30001, "roleType": "smallRobot", "health": 20,
                           "pos": xy((9, 26)), "targetTeam": "challenger", "abnormalState": ""}
        g.step({"challenger": response(10040, command("attack", [(9, 26)], controllerId="10010"))})
        self.assertNotIn(30001, g.robots)
        self.assertEqual(g.unit(10040)["health"], 995)

    def test_mining_last_unit_shared_and_refresh(self):
        g = Game(pressure=0)
        cell = (5, 5)
        g.zones[cell], g.mines[cell] = "copper", 1
        g.unit(10010)["pos"], g.unit(10012)["pos"] = xy((4, 5)), xy((5, 4))
        g.step({"challenger": {"roleCommandMap": {str(uid): command("collect", [cell]) for uid in (10010, 10012)}}})
        self.assertEqual(g.unit(10010)["backpack"], ["copper"])
        self.assertEqual(g.unit(10012)["backpack"], ["copper"])
        self.assertNotIn(cell, g.mines)
        for s in ("challenger", "defender"):
            self.assertFalse(set(g.mines) & (build_ring(g.base(s), 1) | build_ring(g.base(s), 2)))

    def test_upgrade_restores_health_and_invalid_keeps_voucher(self):
        g = Game(pressure=0)
        g.unit(10010)["backpack"] = ["StationUpgradeVoucher1", "StationUpgradeVoucher1"]
        g.base("challenger")["health"] = 2
        use = response(10010, command("use", [(10, 24)], name="StationUpgradeVoucher1"))
        g.step({"challenger": use})
        self.assertEqual(g.base("challenger")["health"], 3000)
        g.step({"challenger": use})
        self.assertEqual(g.unit(10010)["backpack"], ["StationUpgradeVoucher1"])

    def test_respawn_day_two_after_twenty_rounds_keeps_inventory(self):
        g = Game(pressure=0)
        g.unit(10010)["health"] = 0
        g.unit(10010)["backpack"] = ["iron"]
        g.step({})
        g.round = 150
        g.prepare()
        self.assertEqual(g.unit(10010)["health"], 0)
        g.round = 151
        g.prepare()
        self.assertEqual(g.unit(10010)["health"], 220)
        self.assertEqual(g.unit(10010)["backpack"], ["iron"])

    def test_visibility(self):
        g = Game(pressure=0)
        roles = g.observation("challenger")["teamEnemy"]["roles"]
        self.assertEqual([u["roleType"] for u in roles], ["station"])
        g.unit(20010)["pos"] = xy((13, 24))
        self.assertIn(20010, [u["id"] for u in g.observation("challenger")["teamEnemy"]["roles"]])

    def test_partial_task_uses_best_answer(self):
        g = Game(pressure=0)
        g.task_specs["challenger"][0][0]["answer"] = {"a": 1, "b": 2}
        g.unit(10011)["pos"] = xy((13, 14))
        g.step({"challenger": response(10011, command("acceptTask"))})
        g.step({"challenger": response(10011, command("submitAnswer", taskAnswer='{"a":1}'))})
        g.step({"challenger": response(10011, command("submitAnswer", taskAnswer='{}'))})
        while g.task_state["challenger"]:
            g.step({})
        self.assertEqual(g.score["challenger"]["task"], 25)
        self.assertEqual(g.teams["challenger"]["goldNum"], 90)

    def test_complete_task_speed_bonus_and_cooldown(self):
        g = Game(pressure=0)
        g.unit(10011)["pos"] = xy((13, 14))
        answer = json.dumps(g.task_specs["challenger"][0][0]["answer"])
        g.step({"challenger": response(10011, command("acceptTask"))})
        g.step({"challenger": response(10011, command("submitAnswer", taskAnswer=answer))})
        self.assertEqual(g.score["challenger"]["task"], 150)
        self.assertEqual(g.task_available["challenger"][0], 33)

    def test_treasure_simultaneous_reward_and_failed_consumption(self):
        g = Game(pressure=0)
        g.treasure["start"] = 1
        for uid, cell in ((10011, (19, 10)), (20011, (21, 10))):
            g.unit(uid)["pos"] = xy(cell)
            g.unit(uid)["backpack"] = ["AcientTablet", "StarSand"]
        g.step({s: response(uid, command("summonTreasure", [(20, 10)], item=["StarSand", "AcientTablet"]))
                for s, uid in (("challenger", 10011), ("defender", 20011))})
        for side in ("challenger", "defender"):
            self.assertEqual(g.feedback[side]["lastSummonTreasureResult"], 1)
            self.assertEqual(g.score[side]["task"], 100)
        h = Game(pressure=0)
        h.unit(10011)["pos"] = xy((19, 10))
        h.unit(10011)["backpack"] = ["StarSand"]
        h.step({"challenger": response(10011, command("summonTreasure", [(20, 10)], item=["StarSand"]))})
        self.assertEqual(h.unit(10011)["backpack"], [])

    def test_llm_limit_task_exemption_and_sandbox_fixture(self):
        g = Game(pressure=0, services=ReplayServices(commands={"fixture": "[TIMEOUT]\npartial"}))
        for _ in range(4):
            g.step({"challenger": {"roleCommandMap": {}, "prompt": "query"}})
        self.assertEqual(g.feedback["challenger"]["errors"][0]["errorCode"], 5)
        self.assertEqual(g.exceptions["challenger"], 0)
        g.unit(10011)["pos"] = xy((13, 14))
        g.step({"challenger": response(10011, command("acceptTask"))})
        g.step({"challenger": {"roleCommandMap": {}, "prompt": "query", "executeCmd": "fixture"}})
        self.assertEqual(g.feedback["challenger"]["errors"], [])
        self.assertEqual(g.feedback["challenger"]["lastCmdResult"], "[TIMEOUT]\npartial")
        self.assertEqual(g.llm_counts["challenger"], 3)

    def test_five_exceptions_stop_scheduling_not_execution_failures(self):
        g = Game(pressure=0)
        for _ in range(5):
            g.step({"challenger": response(10010, {"action": "nonsense"})})
        self.assertEqual(g.exceptions["challenger"], 5)
        original = deepcopy(g.unit(10010))
        g.step({"challenger": response(10010, command("move", [(8, 24)]))})
        self.assertEqual(g.unit(10010), original)
        self.assertFalse(g.finished)

    def test_full_survival_is_550_and_not_single_base_terminal(self):
        g = Game(pressure=0, tasks_enabled=False)
        for _ in range(1300):
            g.step({})
        self.assertEqual(g.score["challenger"]["survival"], 550)
        self.assertTrue(g.finished)
        h = Game(pressure=0)
        h.base("challenger")["health"] = 0
        h.step({})
        self.assertFalse(h.finished)

    def test_agent_no_mutation_idempotent_no_private_dependency(self):
        g = Game(pressure=0)
        request = g.observation("challenger")
        original = deepcopy(request)
        agent = Agent()
        answer = agent.decide(request)
        self.assertEqual(request, original)
        self.assertEqual(agent.decide(request), answer)
        extended = deepcopy(request)
        extended["_demo"] = {"seed": 999, "answer": "poison"}
        self.assertEqual(Agent().decide(extended), answer)

    def test_bomb_and_five_round_stun(self):
        g = Game(pressure=0)
        g.round = 71
        g.robots[30001] = {"id": 30001, "roleType": "largeRobot", "health": 500,
                           "pos": xy((9, 26)), "targetTeam": "challenger", "abnormalState": ""}
        g.unit(10010)["backpack"] = ["DizzyWeapon"]
        g.unit(10012)["backpack"] = ["Bomb"]
        g.step({"challenger": {"roleCommandMap": {
            "10010": command("use", [(9, 26)], name="DizzyWeapon"),
            "10012": command("use", [(9, 26)], name="Bomb")}}})
        self.assertEqual(g.robots[30001]["health"], 400)
        self.assertEqual(g.unit(10010)["health"], 220)
        for _ in range(4):
            g.step({})
        self.assertEqual(g.round, 76)
        self.assertEqual(g.unit(10010)["health"], 220)
        self.assertEqual(g.robots[30001]["abnormalState"], "")
        before = sum(u["health"] for u in g.living("challenger"))
        g.step({})
        self.assertEqual(sum(u["health"] for u in g.living("challenger")), before - 20)

    def test_daily_summon_cap_and_next_night(self):
        g = Game(pressure=0)
        g.unit(10010)["backpack"] = ["SmallRobotSummonOrder"] * 11
        for _ in range(11):
            g.step({"challenger": response(10010, command("use", name="SmallRobotSummonOrder"))})
        self.assertEqual(g.unit(10010)["backpack"], ["SmallRobotSummonOrder"])
        self.assertEqual(g.summon_counts["challenger"], 10)
        while g.round < 71:
            g.step({})
        self.assertEqual(len(g.robots), 10)
        self.assertTrue(all(r["targetTeam"] == "defender" for r in g.robots.values()))

    def test_leave_task_settles_best_partial(self):
        g = Game(pressure=0)
        g.task_specs["challenger"][0][0]["answer"] = {"a": 1, "b": 2}
        g.unit(10011)["pos"] = xy((13, 14))
        g.step({"challenger": response(10011, command("acceptTask"))})
        g.step({"challenger": response(10011, command("submitAnswer", taskAnswer='{"a":1}'))})
        g.step({"challenger": response(10011, command("move", [(12, 14)]))})
        self.assertIsNone(g.task_state["challenger"])
        self.assertEqual(g.score["challenger"]["task"], 25)

    def test_wrong_build_zone_execution_failure_not_exception(self):
        g = Game(pressure=0)
        g.unit(10010)["pos"] = xy((8, 25))
        g.unit(10010)["backpack"] = ["stone"]
        g.step({"challenger": response(10010, command("build", [(9, 25)], name="wall"))})
        self.assertEqual(g.unit(10010)["backpack"], ["stone"])
        self.assertEqual(g.exceptions["challenger"], 0)
        self.assertFalse(g.feedback["challenger"]["lastRoundRoleActionResults"]["10010"])


if __name__ == "__main__":
    unittest.main()
