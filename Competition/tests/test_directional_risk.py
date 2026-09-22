import unittest
from agent.world import World
from agent.rules import xy
from local_judge.engine import Game


def make_world(side='challenger', cell=(18, 22)):
    g = Game(profile='observed', pressure=0)
    g.round = 71
    g.robots[30001] = {'id': 30001, 'roleType': 'smallRobot', 'pos': xy(cell), 'health': 40,
                       'targetTeam': side, 'abnormalState': ''}
    return g, World(g.observation(side))


class DirectionalRiskTests(unittest.TestCase):
    def test_left_base_risk_in_front_not_behind_or_far_side(self):
        _, w = make_world()
        self.assertIn((17, 22), w.danger_now)
        self.assertNotIn((19, 22), w.danger)
        self.assertNotIn((18, 25), w.danger)
        self.assertGreater(w.robot_risk[(17, 22)], w.robot_risk[(15, 22)])

    def test_right_base_mirrors_direction(self):
        _, w = make_world('defender', (20, 10))
        self.assertIn((21, 10), w.danger_now)
        self.assertNotIn((19, 10), w.danger)

    def test_stunned_robot_has_no_motion_risk(self):
        g, _ = make_world()
        g.robots[30001]['abnormalState'] = 'dizzy'
        self.assertEqual(World(g.observation('challenger')).robot_risk, {})

    def test_observed_simulator_does_not_hit_off_path_worker_in_range(self):
        g, _ = make_world()
        g.unit(10012)['pos'] = xy((19, 22))
        intents, attacks = g._robot_intents()
        self.assertNotIn(10012, [uid for uid, _ in attacks])
        self.assertIn(30001, intents)

    def test_observed_simulator_hits_worker_blocking_next_step(self):
        g, _ = make_world()
        intents, _ = g._robot_intents()
        next_cell = intents[30001]
        g.unit(10012)['pos'] = xy(next_cell)
        intents, attacks = g._robot_intents()
        self.assertEqual(attacks, [(10012, 5)])
        self.assertNotIn(30001, intents)
