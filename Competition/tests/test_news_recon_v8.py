"""Independent user-story expectations and mirrored tactical regressions."""
import json
import unittest

from agent.news import local_treasure
from agent.policy import Agent
from agent.rules import SHOP, build_ring, command, distance, footprint, pos, xy
from agent.tasks import TaskMemory
from agent.world import World
from local_judge.engine import Game, make_unit
from test_defense_raids_v7 import home, enemy_hole, at


OFFICIAL = "矿业管理局紧急通报：北部铁矿区昨夜发生严重矿井塌方事故，主巷道结构受损，部分作业面被掩埋。安全监察部门已下达通知：为保障矿工安全，矿区将于明日全面停工，进行巷道加固和主矿脉修复。矿区领班表示：'今天浅层矿面还能抢采一些，明天的全面停工不可避免。'工程队评估：类似规模的塌方事故，修复工程通常需要2天左右才能完成并恢复采集。"
LEGENDS = [
    '村里最年长的采药人昨天过世了，活了九十三岁。他孙子在整理遗物时发现了一本发霉的笔记，最后一页潦草地写着：西部有一石门，门需三钥。下面还画了一个圆圈，圆圈里画了三道杠。家里人看不懂，打算当废纸烧了。南边渡口的船夫抱怨最近水位下降得厉害，船底已经刮了三次礁石了。',
    '集市上来了个倒卖古董的外乡人，说话油腔滑调，摆了一地稀奇玩意儿。有人问他见没见过会发光的石头，他撇嘴说：你说的是那种灰白色、上面刻着歪歪扭扭字儿的石板吧？我在武器商店老板那儿见过一块，老板说这东西摆在店里好几个月了，根本没人问津，打算当镇纸用了。又有人问光之尘是什么，他想了想：是不是一小袋银白色的粉末，摸起来凉丝丝的，在黑处自己会发光的那种？那也是武器商店从我这收走的！最邪门的是一个水晶瓶子，里头封着一团橙红色的雾，晃一晃还会自己亮——老板说三样东西是同一个怪人卖给他的，卖完人就再没出现过。',
    '祠堂的老祭司在给学徒讲古的时候，提到一段先祖传说：上古时代，有一位大匠师在洪水中保住了全村人的性命，死后被葬在一座地下石殿中。石殿的门由三道封印共同镇守——真言之印需以铭文石板叩问；明光之印需以不灭之光显形；焚天之印需以纯净之火灼烧。三道封印缺一不可，必须同时解开，石殿方能开启。学徒问这石殿在哪，老祭司摇头说年代太久远，没人记得了。',
    '一个猎人在追踪一只受伤的鹿时，意外在原点之北三公里、之东三公里附近发现了一片被藤蔓完全覆盖的石壁。他清理藤蔓后看到一扇约两人高的石门，门上凿有三个凹槽——一个方方正正的浅槽，刚好能嵌进一块石板；一个圆形的浅碟状，像用来盛放粉末的；一个小孔，孔壁有烧焦的痕迹。猎人试着用刀撬了撬门，纹丝不动。他说最奇怪的是，石门周围的草木长得特别茂盛，跟别处的枯草完全不一样。回村后他跟老人描述，没一个人听说过那里有门。老人掐指一算，叹道：这等三封石门，唯待第五日白昼方可松动。',
]


def feed_legends(memory=None):
    m = memory or TaskMemory(); g = Game(pressure=0)
    for day, story in enumerate(LEGENDS, 1):
        g.round = (day-1)*130+1
        p = g.observation('challenger'); p['worldNews'] = {'folkLegends': story}
        m.update(World(p))
    return m


class NewsReconV8Tests(unittest.TestCase):
    def test_official_closure_dates_repeat_without_sliding_or_affecting_treasure(self):
        g = Game(pressure=0); m = TaskMemory()
        for r in (1, 2, 131, 261, 391):
            g.round = r; p = g.observation('challenger')
            p['worldNews'] = {'officialNews': OFFICIAL}
            m.update(World(p))
        self.assertEqual(len(m.official_news), 1)
        self.assertEqual([(c['name'], c['startDay'], c['endDay']) for c in m.blocked_minerals], [('iron', 2, 3)])
        self.assertEqual(m.price_forecasts[0]['direction'], 'up')
        self.assertIsNone(m.treasure)

    def test_mining_holding_and_reopening_use_current_vendor_prices(self):
        for day, collecting in ((1, True), (2, False), (3, False), (4, True)):
            g, _, _ = home(round_no=(day-1)*130+1)
            g.unit(10010)['pos'] = xy((19, 16))
            g.unit(10010)['backpack'] = ['iron'] * 6
            g.teams['challenger']['goldNum'] = 60
            a = Agent(); p = g.observation('challenger'); p['worldNews'] = {}
            a.decide(p)
            a.memory = feed_legends()  # Not needed for mining; exercises channel independence.
            a.memory.blocked_minerals = [{'name': 'iron', 'startDay': 2, 'endDay': 3}]
            a.memory.price_forecasts = [{'name': 'iron', 'startDay': 2, 'endDay': 3, 'direction': 'up', 'confidence': .95, 'evidence': OFFICIAL}]
            self.assertEqual(a.hold_mineral('iron', a.w.units[10010]), day == 1)
            # Independent rule boundary check; actual trade never overwrites prices.
            a.w.zones = {(18, 16): 'iron'}; a.w.vendor = {'iron': 17}
            a.w.actors = [a.w.units[10010]]; a.w.weapons = []
            a.w.units[10010]['backpack'] = []
            from agent.audit import Ledger
            a.ledger = Ledger(a.w); a.response['roleCommandMap'] = {}; a.reserved = set()
            a.economy(a.w.units[10010])
            self.assertEqual(a.response['roleCommandMap'].get('10010', {}).get('action') == 'collect', collecting)
            self.assertEqual(a.w.vendor['iron'], 17)

    def test_four_days_produce_exact_plan_without_llm_or_incidental_age_numbers(self):
        m = TaskMemory(); g = Game(pressure=0)
        for day, text in enumerate(LEGENDS, 1):
            g.round = 130*(day-1)+1; p = g.observation('challenger')
            p['worldNews'] = {'folkLegends': text, 'officialNews': OFFICIAL}
            m.update(World(p))
            if day < 4:
                self.assertIsNone(m.treasure)
        t = m.treasure
        self.assertEqual(t['pos'], xy((3, 3)))
        self.assertEqual(t['items'], ['AcientTablet', 'StarSand', 'FlameBreath'])
        self.assertEqual((t['startRound'], t['endRound']), (521, 590))
        self.assertEqual(len(m.folk_legends), 4)
        self.assertEqual(len(m.official_news), 1)

    def test_coordinates_dates_and_items_not_hardcoded_to_example(self):
        stories = list(LEGENDS)
        stories[-1] = stories[-1].replace('之北三公里、之东三公里', '之北八公里、之东六公里').replace('第五日', '第七日')
        plan = local_treasure([{'text': s} for s in stories], SHOP)['treasure']
        self.assertEqual(plan['pos'], xy((6, 8)))
        self.assertEqual((plan['startRound'], plan['endRound']), (781, 850))
        shop = dict(SHOP); del shop['StarSand']
        self.assertIsNone(local_treasure([{'text': s} for s in stories], shop)['treasure'])

    def test_new_official_news_does_not_erase_complete_legend_plan(self):
        m = feed_legends(); before = m.treasure
        g = Game(pressure=0); g.round = 392
        p = g.observation('challenger'); p['worldNews'] = {'officialNews': OFFICIAL, 'folkLegends': ''}
        m.update(World(p))
        self.assertEqual(m.treasure, before)
        self.assertIn('folkLegends', m.news_prompt(World(p)))

    def test_conflicting_or_negative_clue_requires_further_reasoning(self):
        plan = local_treasure([{'text': s} for s in LEGENDS + ['石门不在原点之北三公里、之东三公里，先前说法是谣言。']], SHOP)
        self.assertIsNone(plan['treasure'])
        self.assertTrue(plan['conflicts'])

    def test_unpaired_summon_feedback_is_not_a_task_availability_switch(self):
        for code in range(5):
            m = feed_legends(); before = m.treasure
            g = Game(pressure=0); g.round = 392
            p = g.observation('challenger'); p['lastSummonTreasureResult'] = code; p['worldNews'] = {}
            m.update(World(p))
            self.assertEqual(m.treasure, before)
            self.assertFalse(m.treasure_done)

    def test_unsubstantiated_model_evidence_and_wrong_channel_are_rejected(self):
        m = TaskMemory(); g = Game(pressure=0); p = g.observation('challenger')
        p['worldNews'] = {'officialNews': LEGENDS[-1], 'folkLegends': LEGENDS[0]}
        m.update(World(p)); m.news_prompt(World(p))
        p['roundNo'] += 1
        p['llmResp'] = json.dumps({'treasure': {'certain': True, 'pos': xy((3, 3)), 'items': ['AcientTablet'], 'startRound': 521, 'endRound': 590,
            'evidence': {'position': LEGENDS[-1], 'time': LEGENDS[-1], 'items': 'invented'}}, 'missing': [], 'conflicts': []})
        m.update(World(p))
        self.assertIsNone(m.treasure)

    def test_treasure_departure_budgets_shop_then_three_purchases_and_travel(self):
        g, _, _ = home(round_no=482)
        g.unit(10011)['pos'] = xy((33, 10)); g.teams['challenger']['goldNum'] = 100
        a = Agent(); p = g.observation('challenger'); p['worldNews'] = {}; a.decide(p)
        a.memory = feed_legends(); g.round += 1
        p = g.observation('challenger'); p['worldNews'] = {}
        answer = a.decide(p)
        self.assertEqual(answer['roleCommandMap']['10011']['action'], 'move')
        self.assertTrue(any(e['kind'] == 'treasureJourney' for e in a.events))
        self.assertFalse(a.raid_assignments)

    def test_correct_summon_only_inside_window_and_with_exact_item_set(self):
        for r, action in ((520, None), (521, 'summonTreasure'), (590, 'summonTreasure'), (591, None)):
            g, _, _ = home(round_no=r); g.unit(10011)['pos'] = xy((4, 3))
            g.unit(10011)['backpack'] = ['AcientTablet', 'StarSand', 'FlameBreath', 'Medicine']
            a = Agent(); a.decide(g.observation('challenger')); a.memory = feed_legends()
            p = g.observation('challenger'); p['worldNews'] = {}
            a.cache_key = None
            answer = a.decide(p)['roleCommandMap'].get('10011', {})
            if action:
                self.assertEqual(answer['action'], action)
                self.assertEqual(answer['item'], ['AcientTablet', 'StarSand', 'FlameBreath'])
            else:
                self.assertNotEqual(answer.get('action'), 'summonTreasure')

    def test_first_day_scouts_before_accepting_and_then_returns_to_tasks(self):
        for side, uid, task in (('challenger', 10011, (13, 14)), ('defender', 20011, (24, 14))):
            g = Game(profile='observed', pressure=0); g.unit(uid)['pos'] = xy(task)
            a = Agent(); answer = a.decide(g.observation(side))
            self.assertEqual(a.raid_kind, 'firstScout')
            self.assertEqual(answer['roleCommandMap'][str(uid)]['action'], 'move')
            g.round = 36
            answer = a.decide(g.observation(side))
            self.assertEqual(answer['roleCommandMap'][str(uid)]['action'], 'acceptTask')

    def test_active_task_always_preempts_first_scout(self):
        g = Game(profile='observed', pressure=0); g.unit(10011)['pos'] = xy((13, 14))
        g.step({'challenger': {'roleCommandMap': {'10011': command('acceptTask')}}})
        a = Agent(); a.decide(g.observation('challenger'))
        self.assertFalse(a.raid_assignments)
        self.assertTrue(a.w.phase)

    def test_cooldown_is_not_permanently_finished_or_a_cross_map_raid(self):
        g, _, _ = home(round_no=150)
        g.unit(10011)['pos'] = xy((13, 14)); enemy_hole(g)
        p = g.observation('challenger')
        p['teamOur']['playerTasks'] = [{'isValid': False, 'coldDownRounds': 15, 'taskPosition': xy((14, 14))}]
        a = Agent(); a.decide(p)
        self.assertFalse(a.raid_assignments)
        p['roundNo'] += 1; p['teamOur']['playerTasks'][0].update(isValid=True, coldDownRounds=0)
        self.assertEqual(a.decide(p)['roleCommandMap']['10011']['action'], 'acceptTask')

    def test_single_exit_has_priority_over_weapon_hub(self):
        g, _, _ = home()
        ring = build_ring(g.base('defender'), 2); gap = (28, 10)
        for i, cell in enumerate(sorted(ring - {gap})):
            g.teams['defender']['roles'].append(make_unit(25000+i, 'wall', cell))
        g.unit(10011)['pos'] = xy((27, 10))
        for i, cell in enumerate(((31, 11), (32, 11), (32, 9))):
            g.teams['defender']['roles'].append(make_unit(26000+i, 'rocket', cell))
        a = Agent(); a.decide(g.observation('challenger'))
        self.assertEqual(a.raid_kind, 'oneExit')
        self.assertEqual(a.raid_assignments[10011], gap)

    def test_remembered_shared_weapon_position_precedes_front_breach(self):
        g, _, _ = home(); g.unit(10011)['pos'] = xy((33, 10))
        g.unit(20010)['pos'] = xy((36, 5)); g.unit(20012)['pos'] = xy((36, 6))
        for i, cell in enumerate(((31, 11), (32, 11), (32, 9))):
            g.teams['defender']['roles'].append(make_unit(26000+i, 'rocket', cell))
        a = Agent(); a.decide(g.observation('challenger'))
        self.assertEqual(len(a.known_enemy_guns()), 3)
        enemy_hole(g); g.round = 411; g.unit(10011)['pos'] = xy((24, 10))
        a.decide(g.observation('challenger'))
        self.assertEqual(a.raid_kind, 'operatorBlock')
        self.assertEqual(a.raid_assignments[10011], (32, 10))
        g.unit(10011)['pos'] = xy((33, 10)); g.round += 1
        for u in g.teams['defender']['roles']:
            if u['roleType'] == 'rocket': u['health'] = 0
        a.decide(g.observation('challenger'))
        self.assertEqual(a.known_enemy_guns(), [])

    def test_dusk_departure_uses_actual_rear_path_both_sides(self):
        for side in ('challenger', 'defender'):
            g, _, offset = home(side, 453)  # Eight rounds of daylight left.
            hole = enemy_hole(g, side); g.unit(offset+11)['pos'] = xy(hole)
            a = Agent(); answer = a.decide(g.observation(side))
            self.assertTrue(a.raid_safety)
            self.assertEqual(a.raid_kind, 'rearWait')
            self.assertEqual(answer['roleCommandMap'][str(offset+11)]['action'], 'move')
            self.assertIn(a.raid_assignments[offset+11], a.enemy_rear_cells())

    def test_near_enemy_robot_cancels_hole_even_if_attacking_other_team(self):
        for side in ('challenger', 'defender'):
            g, _, offset = home(side, 480); hole = enemy_hole(g, side)
            other = 'defender' if side == 'challenger' else 'challenger'
            g.unit(offset+11)['pos'] = xy(hole)
            cell = (27, 10) if side == 'challenger' else (13, 21)
            g.robots[30001] = dict(make_unit(30001, 'largeRobot', cell), targetTeam=other)
            a = Agent(); response = a.decide(g.observation(side))
            self.assertTrue(a.raid_safety)
            self.assertNotEqual(a.raid_kind, 'frontBreach')
            cmd = response['roleCommandMap'].get(str(offset+11), {})
            if cmd.get('action') == 'move':
                self.assertNotIn(pos(cmd['targetPos'][0]), a.w.danger_now)

    def test_night_opening_stays_rear_and_clear_late_night_can_block(self):
        g, _, _ = home(round_no=461); hole = enemy_hole(g)
        g.unit(10011)['pos'] = xy((33, 10))
        a = Agent(); a.decide(g.observation('challenger'))
        self.assertTrue(a.raid_safety)
        self.assertEqual(a.raid_kind, 'rearWait')
        g.round = 480; a.decide(g.observation('challenger'))
        self.assertEqual(a.raid_kind, 'frontBreach')
        self.assertEqual(a.raid_assignments[10011], hole)

    def test_blocked_home_hub_recalls_second_worker_and_allows_two_shots(self):
        g, layout, _ = home(round_no=461)
        g.unit(20011)['pos'] = xy(layout['hub']); g.unit(10010)['pos'] = xy((7, 21))
        a = Agent(); response = a.decide(g.observation('challenger'))
        self.assertEqual(a.support_gunner, 10012)
        self.assertEqual(response['roleCommandMap']['10012']['action'], 'move')
        g.unit(10010)['pos'] = xy(a.operator_hub); g.unit(10012)['pos'] = xy(a.support_post)
        g.round += 1
        for i in range(8):
            g.robots[30000+i] = dict(make_unit(30000+i, 'largeRobot', (17+i%4, 21+i//4)), targetTeam='challenger')
        response = a.decide(g.observation('challenger'))
        attacks = [c for c in response['roleCommandMap'].values() if c['action'] == 'attack']
        self.assertEqual({c['controllerId'] for c in attacks}, {'10010', '10012'})
        self.assertNotIn('10010', response['roleCommandMap'])
        self.assertNotIn('10012', response['roleCommandMap'])
        self.assertFalse(a.diagnostics)


if __name__ == '__main__':
    unittest.main()
