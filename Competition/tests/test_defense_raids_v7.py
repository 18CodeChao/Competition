"""Independent, hand-specified regressions for user wall/blockade observations."""
from copy import deepcopy
import unittest

from agent.policy import Agent
from agent.rules import build_ring, command, distance, pos, xy
from agent.layout import plan_layout
from agent.world import World
from local_judge.engine import Game, make_unit


STOCK = ['stone'] * 3 + ['WallUpgradeVoucher1'] * 2 + ['WallUpgradeVoucher2', 'WallFixer', 'WallFixer', 'StationUpgradeVoucher1']


def home(side='challenger', round_no=391):
    g = Game(profile='observed', pressure=0, tasks_enabled=False)
    g.round = round_no
    offset = 10000 if side == 'challenger' else 20000
    layout = plan_layout(World(g.observation(side)))
    g.unit(offset + 10)['pos'] = xy(layout['hub'])
    g.unit(offset + 12)['pos'] = xy((11, 21) if side == 'challenger' else (29, 10))
    g.unit(offset + 12)['backpack'] = list(STOCK)
    g.unit(offset + 11)['pos'] = xy((20, 13))
    g.teams[side]['goldNum'] = 0
    for i, cell in enumerate(layout['guns']):
        g.teams[side]['roles'].append(make_unit(offset + 40 + i, 'rocket', cell, 3))
    for i, cell in enumerate(layout['walls']):
        g.teams[side]['roles'].append(make_unit(offset + 50 + i, 'wall', cell))
    return g, layout, offset


def at(g, side, cell):
    return next(u for u in g.teams[side]['roles'] if pos(u['pos']) == cell)


def enemy_hole(g, own='challenger'):
    other = 'defender' if own == 'challenger' else 'challenger'
    cell = (28, 10) if own == 'challenger' else (12, 21)
    for i, y in enumerate((cell[1] - 1, cell[1] + 1)):
        g.teams[other]['roles'].append(make_unit(25000 + i, 'wall', (cell[0], y)))
    return cell


class DefenseRaidTests(unittest.TestCase):
    def test_upgrade_route_does_not_oscillate_toward_farthest_equal_hp_gun(self):
        g, _, _ = home(round_no=1)
        # This caused repeated left/right walks instead of upgrading or mining.
        g.unit(10012).update(pos=xy((11, 20)), backpack=['WeaponUpgradeVoucher1'])
        for u in g.teams['challenger']['roles']:
            if u['roleType'] == 'rocket':
                u.update(level=1, health=1000)
        a = Agent()
        actions = []
        for _ in range(8):
            response = a.decide(g.observation('challenger'))
            actions.append(response['roleCommandMap'].get('10012', {}))
            g.step({'challenger': response})
        self.assertTrue(any(c.get('action') == 'use' and c.get('name') == 'WeaponUpgradeVoucher1' for c in actions))

    def test_dawn_rebuild_both_sides_before_weapon_or_batch(self):
        for side, site in (('challenger', (12, 21)), ('defender', (28, 10))):
            with self.subTest(side=side):
                g, layout, offset = home(side, 390)
                a = Agent()
                a.decide(g.observation(side))
                at(g, side, site)['health'] = 0
                g.unit(offset + 40)['health'] = 0
                g.unit(offset + 12)['backpack'] = ['stone']
                g.round = 391
                cmd = a.decide(g.observation(side))['roleCommandMap'][str(offset + 12)]
                self.assertEqual(cmd, command('build', [site], name='wall'))
                self.assertFalse(a.diagnostics)

    def test_known_destroyed_wall_is_urgent_before_fourth_day(self):
        g, _, _ = home(round_no=130)
        a = Agent(); a.decide(g.observation('challenger'))
        at(g, 'challenger', (12, 21))['health'] = 0
        g.unit(10012)['backpack'] = ['stone']
        g.round = 131
        self.assertEqual(a.decide(g.observation('challenger'))['roleCommandMap']['10012']['action'], 'build')

    def test_reconnect_day_four_detects_missing_wall_without_history(self):
        g, _, _ = home()
        at(g, 'challenger', (12, 21))['health'] = 0
        self.assertEqual(Agent().decide(g.observation('challenger'))['roleCommandMap']['10012'], command('build', [(12, 21)], name='wall'))

    def test_upgrade_before_fixer_regardless_of_bag_order(self):
        g, _, _ = home(round_no=465)
        wall = at(g, 'challenger', (12, 21)); wall['health'] = 450
        g.unit(10012)['backpack'] = ['WallFixer', 'WallUpgradeVoucher1', 'stone', 'stone', 'stone']
        response = Agent().decide(g.observation('challenger'))
        self.assertEqual(response['roleCommandMap']['10012'], command('use', [(12, 21)], name='WallUpgradeVoucher1'))
        g.step({'challenger': response})
        self.assertEqual((wall['level'], wall['health']), (2, 1500))

    def test_damage_forecast_upgrades_before_fixed_hp_threshold(self):
        g, _, _ = home(round_no=465)
        at(g, 'challenger', (12, 21))['health'] = 800
        payload = g.observation('challenger')
        payload['robot']['roles'] = [dict(make_unit(30000+i, 'bossRobot', (15, 18+i)), targetTeam='challenger') for i in range(7)]
        a = Agent(); cmd = a.decide(payload)['roleCommandMap']['10012']
        self.assertEqual(cmd['name'], 'WallUpgradeVoucher1')
        self.assertEqual(cmd['action'], 'use')
        event = next(e for e in a.events if e['kind'] == 'maintenance' and e['role'] == 10012)
        self.assertGreater(event['threshold'], 800)

    def test_max_level_wall_uses_fixer(self):
        g, _, _ = home(round_no=465)
        wall = at(g, 'challenger', (12, 21)); wall.update(level=3, health=400)
        self.assertEqual(Agent().decide(g.observation('challenger'))['roleCommandMap']['10012']['name'], 'WallFixer')

    def test_full_wall_can_upgrade_when_forecast_is_lethal(self):
        g, _, _ = home(round_no=465)
        for u in g.teams['challenger']['roles']:
            if u['roleType'] == 'wall' and pos(u['pos']) != (12, 21):
                u.update(level=3, health=2000)
        payload = g.observation('challenger')
        payload['robot']['roles'] = [dict(make_unit(30000+i, 'bossRobot', (13+i%4, 18+i//4)), targetTeam='challenger') for i in range(26)]
        cmd = Agent().decide(payload)['roleCommandMap']['10012']
        self.assertEqual(cmd, command('use', [(12, 21)], name='WallUpgradeVoucher1'))

    def test_worker_stays_inside_at_night_and_no_night_build(self):
        g, _, _ = home(round_no=461)
        at(g, 'challenger', (12, 21))['health'] = 0
        a = Agent(); cmds = a.decide(g.observation('challenger'))['roleCommandMap']
        self.assertFalse(any(c['action'] == 'build' for c in cmds.values()))
        cmd = cmds.get('10012')
        if cmd:
            self.assertEqual(cmd['action'], 'move')
            self.assertIn(pos(cmd['targetPos'][0]), a.maintenance_posts())

    def test_enemy_occupied_breach_base_upgrade_has_priority(self):
        g, _, _ = home(round_no=465)
        at(g, 'challenger', (12, 21))['health'] = 0
        g.unit(20011)['pos'] = xy((12, 21))
        g.base('challenger')['health'] = 350
        answer = Agent().decide(g.observation('challenger'))['roleCommandMap']['10012']
        self.assertEqual(answer, command('use', [(9, 22)], name='StationUpgradeVoucher1'))

    def test_stock_is_bought_for_repair_worker_not_shared(self):
        g, _, _ = home()
        g.unit(10012).update(pos=xy((24, 20)), backpack=['stone']*3)
        g.unit(10011)['backpack'] = ['WallUpgradeVoucher1']*3
        g.teams['challenger']['goldNum'] = 70
        cmd = Agent().decide(g.observation('challenger'))['roleCommandMap']['10012']
        self.assertEqual(cmd, command('buy', name='WallUpgradeVoucher1', num=2))

    def test_low_budget_still_buys_affordable_fixer(self):
        g, _, _ = home()
        g.unit(10012).update(pos=xy((24, 20)), backpack=['stone']*3)
        g.teams['challenger']['goldNum'] = 15
        cmd = Agent().decide(g.observation('challenger'))['roleCommandMap']['10012']
        self.assertEqual(cmd, command('buy', name='WallFixer', num=1))

    def test_three_stones_not_sold_when_wall_complete(self):
        g, _, _ = home()
        g.unit(10012).update(pos=xy((19, 16)), backpack=STOCK + ['copper']*5)
        cmd = Agent().decide(g.observation('challenger'))['roleCommandMap']['10012']
        self.assertEqual((cmd['action'], cmd['name'], cmd['num']), ('sell', 'copper', 5))

    def test_reserve_stone_mining_before_optional_weapon_upgrade(self):
        g, _, _ = home()
        g.unit(10012).update(pos=xy((14, 20)), backpack=[])
        g.zones[(15, 20)] = 'stone'
        g.teams['challenger']['goldNum'] = 200
        cmd = Agent().decide(g.observation('challenger'))['roleCommandMap']['10012']
        self.assertEqual(cmd, command('collect', [(15, 20)]))

    def test_pioneer_occupies_only_front_hole_and_withdraws_at_dusk(self):
        for side in ('challenger', 'defender'):
            g, _, offset = home(side)
            hole = enemy_hole(g, side)
            g.unit(offset+11)['pos'] = xy((hole[0]-1 if side == 'challenger' else hole[0]+1, hole[1]))
            a = Agent(); response = a.decide(g.observation(side))
            self.assertEqual(response['roleCommandMap'][str(offset+11)], command('move', [hole]))
            g.unit(offset+11)['pos'] = xy(hole); g.round += 1
            self.assertNotIn(str(offset+11), a.decide(g.observation(side))['roleCommandMap'])
            self.assertTrue(a.blocking_breach)
            g.round = 459  # Day four has two daylight rounds left.
            cmd = a.decide(g.observation(side))['roleCommandMap'][str(offset+11)]
            self.assertEqual(cmd['action'], 'move')
            self.assertNotEqual(pos(cmd['targetPos'][0]), hole)

    def test_top_bottom_gap_is_not_a_front_breach(self):
        g, _, _ = home()
        for i, cell in enumerate(((29, 12), (31, 12))):
            g.teams['defender']['roles'].append(make_unit(25000+i, 'wall', cell))
        a = Agent(); a.decide(g.observation('challenger'))
        self.assertFalse(a.blocking_breach)
        self.assertNotEqual(a.raid_kind, 'frontBreach')

    def test_valid_task_cancels_blockade_and_phase_prevents_raiding(self):
        g, _, _ = home()
        hole = enemy_hole(g); g.unit(10011)['pos'] = xy(hole)
        a = Agent(); a.decide(g.observation('challenger'))
        self.assertTrue(a.blocking_breach)
        payload = g.observation('challenger'); payload['roundNo'] += 1
        payload['teamOur']['playerTasks'] = [{'isValid': True, 'taskPosition': xy((14, 14)), 'timeoutRounds': 10}]
        cmd = a.decide(payload)['roleCommandMap']['10011']
        self.assertEqual(cmd['action'], 'move')
        self.assertFalse(a.raid_assignments)
        payload['roundNo'] += 1; payload['phaseTask'] = '正在进行任务'
        a.decide(payload)
        self.assertFalse(a.raid_assignments)

    def test_visible_shared_enemy_operator_position_is_targeted(self):
        g, _, _ = home()
        g.unit(10011)['pos'] = xy((33, 10))
        for i, cell in enumerate(((31, 11), (32, 11), (32, 9))):
            g.teams['defender']['roles'].append(make_unit(25000+i, 'rocket', cell))
        # Move the enemy workers away from the shared post in this fixture.
        g.unit(20010)['pos'] = xy((36, 6)); g.unit(20012)['pos'] = xy((36, 7))
        a = Agent(); response = a.decide(g.observation('challenger'))
        self.assertEqual(a.raid_kind, 'operatorBlock')
        self.assertEqual(response['roleCommandMap']['10011'], command('move', [(32, 10)]))

    def test_two_exit_ring_requires_home_safety_and_worker_return_budget(self):
        g, _, _ = home(round_no=261)
        ring = build_ring(g.base('defender'), 2)
        gaps = {(28, 10), (33, 10)}
        for i, cell in enumerate(sorted(ring - gaps)):
            g.teams['defender']['roles'].append(make_unit(25000+i, 'wall', cell))
        g.unit(10011)['pos'] = xy((27, 10)); g.unit(10012)['pos'] = xy((34, 10))
        g.unit(20010)['pos'] = xy((36, 6)); g.unit(20012)['pos'] = xy((36, 7))
        a = Agent(); a.decide(g.observation('challenger'))
        self.assertEqual(a.raid_kind, 'twoExits')
        self.assertEqual(set(a.raid_assignments.values()), gaps)
        at(g, 'challenger', (12, 21))['health'] = 0
        g.round += 1; a.decide(g.observation('challenger'))
        self.assertNotIn(10012, a.raid_assignments)

    def test_boss_only_after_actual_occupation_and_after_defense_budget(self):
        g, _, _ = home()
        hole = enemy_hole(g)
        g.unit(10011)['pos'] = xy(hole)
        g.unit(10012)['pos'] = xy((24, 20))
        g.teams['challenger']['goldNum'] = 200
        a = Agent(); response = a.decide(g.observation('challenger'))
        self.assertEqual(response['roleCommandMap']['10012'], command('buy', name='BossRobotSummonOrder', num=1))
        g.unit(10012)['backpack'].append('BossRobotSummonOrder'); g.round += 1
        response = a.decide(g.observation('challenger'))
        self.assertEqual(response['roleCommandMap']['10012'], command('use', name='BossRobotSummonOrder'))
        g.round += 1
        self.assertNotEqual(a.decide(g.observation('challenger'))['roleCommandMap'].get('10012'), command('use', name='BossRobotSummonOrder'))
        g2, _, _ = home(); enemy_hole(g2)
        g2.unit(10012)['pos'] = xy((24, 20)); g2.teams['challenger']['goldNum'] = 200
        a2 = Agent(); a2.decide(g2.observation('challenger'))
        self.assertFalse(a2.blocking_breach)
        self.assertFalse(any(c.get('name') == 'BossRobotSummonOrder' for c in a2.response['roleCommandMap'].values()))

    def test_boss_purchase_cannot_spend_maintenance_reserve(self):
        g, _, _ = home()
        g.unit(10011)['pos'] = xy(enemy_hole(g))
        g.unit(10012).update(pos=xy((24, 20)), backpack=['stone']*3)
        g.teams['challenger']['goldNum'] = 200
        a = Agent(); a.decide(g.observation('challenger'))
        self.assertTrue(a.blocking_breach)
        self.assertNotEqual(a.response['roleCommandMap']['10012'].get('name'), 'BossRobotSummonOrder')

    def test_occupied_own_hub_has_reachable_alternative_and_can_fire(self):
        g, layout, _ = home(round_no=461)
        g.unit(10010)['pos'] = xy((7, 21)); g.unit(20011)['pos'] = xy(layout['hub'])
        a = Agent(); response = a.decide(g.observation('challenger'))
        self.assertNotEqual(a.operator_hub, layout['hub'])
        self.assertNotIn(a.operator_hub, {pos(u['pos']) for u in a.w.enemies})
        self.assertTrue(any(distance(a.operator_hub, p) == 1 for p in layout['guns']))
        g.unit(10010)['pos'] = xy(a.operator_hub); g.round += 1
        g.robots[30001] = dict(make_unit(30001, 'largeRobot', (18, 22)), targetTeam='challenger')
        response = a.decide(g.observation('challenger'))
        self.assertTrue(any(c['action'] == 'attack' and c['controllerId'] == '10010' for c in response['roleCommandMap'].values()))

    def test_gunner_guards_hub_by_day_when_enemy_approaches(self):
        g, layout, _ = home()
        g.unit(20011)['pos'] = xy((6, 21))
        response = Agent().decide(g.observation('challenger'))
        self.assertNotIn('10010', response['roleCommandMap'])

    def test_destroyed_base_does_not_crash_maintenance(self):
        g, _, _ = home(round_no=465)
        g.base('challenger')['health'] = 0
        at(g, 'challenger', (12, 21))['health'] = 100
        Agent().decide(g.observation('challenger'))


if __name__ == '__main__':
    unittest.main()
