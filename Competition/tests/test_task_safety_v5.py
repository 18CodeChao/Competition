import io
import json
import unittest

from agent.brief_log import ReadableJournal
from agent.policy import Agent
from agent.rules import command, distance, xy
from agent.tasks import TaskMemory, usable_answer
from agent.world import World
from local_judge.engine import Game


class TaskSafetyV5Tests(unittest.TestCase):
    def task_game(self):
        g = Game(pressure=0)
        g.task_specs['challenger'][0][0]['description'] = '请阅读task_fixture.md，获取任务信息'
        g.unit(10011)['pos'] = xy((13, 14))
        g.step({'challenger': {'roleCommandMap': {'10011': command('acceptTask')}}})
        return g

    def test_night_gate_both_sides_and_second_day(self):
        for side, uid, cell in [('challenger', 10011, (13, 14)), ('defender', 20011, (24, 14))]:
            # v8: first-day opening is reserved for the user's scouting request.
            for r, allowed in ((1, False), (36, True), (60, False), (70, False), (71, False), (80, False),
                               (81, True), (201, False), (210, False), (211, True)):
                with self.subTest(side=side, round=r):
                    g = Game(pressure=0); g.round = r; g.unit(uid)['pos'] = xy(cell)
                    response = Agent().decide(g.observation(side))
                    self.assertEqual(response['roleCommandMap'].get(str(uid), {}).get('action') == 'acceptTask', allowed)

    def test_active_task_sidesteps_only_within_task_area_and_keeps_reading(self):
        g = self.task_game(); g.round = 71
        g.robots[30001] = {'id': 30001, 'roleType': 'smallRobot', 'health': 40,
                           'pos': xy((14, 13)), 'targetTeam': 'challenger', 'abnormalState': ''}
        a = Agent(); response = a.decide(g.observation('challenger'))
        cmd = response['roleCommandMap']['10011']
        self.assertEqual(cmd['action'], 'move')
        p = cmd['targetPos'][0]
        self.assertLessEqual(max(abs(p['x'] - 14), abs(p['y'] - 14)), 1)
        self.assertTrue(response['executeCmd'])
        self.assertFalse(a.emit(a.w.units[10011], command('move', [(12, 15)])))

    def test_two_cell_task_area_stays_bound_to_selected_point(self):
        g = Game(pressure=0); g.round = 10
        p = g.observation('challenger'); p['phaseTask'] = 'fixture'
        m = TaskMemory(); m.accepted = (9, {'taskPosition': xy((16, 17)), 'timeoutRounds': 15})
        w = World(p); m.update(w)
        self.assertIn((18, 18), m.task_area(w))  # Adjacent to second cell of task point 2.
        self.assertNotIn((13, 14), m.task_area(w))  # Belongs to different task point 1.
        p['roundNo'] = 11; m.update(World(p))
        self.assertIn((18, 18), m.task_area(World(p)))

    def test_actual_damage_triggers_local_sidestep_even_when_path_prediction_misses(self):
        g = self.task_game(); g.round = 81
        g.robots[30001] = {'id': 30001, 'roleType': 'smallRobot', 'health': 40,
                           'pos': xy((10, 14)), 'targetTeam': 'challenger', 'abnormalState': ''}
        a = Agent(); a.decide(g.observation('challenger'))
        g.round += 1; g.unit(10011)['health'] -= 5
        response = a.decide(g.observation('challenger'))
        self.assertNotIn((13, 14), a.w.danger_now)
        cmd = response['roleCommandMap']['10011']
        self.assertEqual(cmd['action'], 'move')
        p = cmd['targetPos'][0]
        self.assertGreater(distance((p['x'], p['y']), (10, 14)), 3)
        self.assertLessEqual(distance((p['x'], p['y']), (14, 14)), 1)

    def test_unknown_active_point_cannot_move_to_another_task_point(self):
        g = Game(pressure=0); p = g.observation('challenger'); p['phaseTask'] = 'fixture'
        a = Agent(); a.decide(p)
        self.assertEqual(a.memory.task_area(a.w), set())
        self.assertFalse(a.emit(a.w.units[10011], command('move', [(13, 14)])))

    def test_placeholder_answers_blocked_zero_and_false_preserved(self):
        for answer in ('{"task_info": null}', '{"answer": null}', '{}', '[]', 'unknown', '待获取', '{"facts":{"content":"doc"}}'):
            self.assertFalse(usable_answer(answer), answer)
        for answer in ('{"count":0}', '{"found":false}', '0', 'false', '{"token":"fresh-token"}'):
            self.assertTrue(usable_answer(answer), answer)

    def test_placeholder_model_reply_causes_repair_prompt_not_submit(self):
        g = self.task_game(); a = Agent(); a.decide(g.observation('challenger'))
        a.memory.pending = ('task', a.memory.phase)
        p = g.observation('challenger'); p['roundNo'] += 1
        p['llmResp'] = json.dumps({'answer': {'task_info': None}})
        response = a.decide(p)
        self.assertNotEqual(response['roleCommandMap'].get('10011', {}).get('action'), 'submitAnswer')
        self.assertIn('拒绝空值', response['prompt'])

    def test_answer_survives_healing_without_extra_role_action(self):
        g = self.task_game(); a = Agent(); a.decide(g.observation('challenger'))
        a.memory.pending = ('task', a.memory.phase)
        p = g.observation('challenger'); p['roundNo'] += 1
        pioneer = next(u for u in p['teamOur']['roles'] if u['id'] == 10011)
        pioneer['health'] = 80; pioneer['backpack'] = ['Medicine']
        p['llmResp'] = json.dumps({'answer': {'token': 'fixture'}})
        response = a.decide(p)
        self.assertEqual(response['roleCommandMap']['10011']['action'], 'use')
        self.assertIsNotNone(a.memory.deferred_answer)
        p['roundNo'] += 1; p['llmResp'] = ''; pioneer['health'] = 200; pioneer['backpack'] = []
        response = a.decide(p)
        self.assertEqual(response['roleCommandMap']['10011']['action'], 'submitAnswer')
        self.assertEqual(json.loads(response['roleCommandMap']['10011']['taskAnswer']), {'token': 'fixture'})


class HumanLogV5Tests(unittest.TestCase):
    def test_round_format_independent_bags_and_next_turn_collection(self):
        stream = io.StringIO(); j = ReadableJournal(stream)
        g = Game(pressure=0); p = g.observation('challenger')
        roles = {a['id']: a for a in p['teamOur']['roles']}
        roles[10010]['backpack'] = ['copper']; roles[10012]['backpack'] = ['stone', 'stone']
        response = {'roleCommandMap': {'10010': command('collect', [(22, 0)]), '10012': command('move', [(8, 23)])}}
        j.record(p, response, {}, 1)
        first = stream.getvalue()
        self.assertIn('Round 1：\nRequest：', first)
        self.assertIn('Response：\n', first)
        self.assertIn('工人 10010 采集(22,0)（下发，待下轮确认）', first)
        self.assertIn('当前背包有 1个铜', first)
        self.assertIn('当前背包有 2个石头', first)
        self.assertNotIn('确认获得', first)
        self.assertNotIn('lastCmdResult', first)
        self.assertNotIn('lastSummonTreasureResult', first)
        second = g.observation('challenger'); second['roundNo'] = 2
        for actor in second['teamOur']['roles']:
            if actor['id'] == 10010:
                actor['backpack'] = ['copper', 'copper']
        second['lastRoundRoleActionResults'] = {'10010': True}
        j.record(second, {'roleCommandMap': {}}, {}, 1)
        self.assertIn('确认获得 铜×1', stream.getvalue())
        self.assertEqual(stream.getvalue().count('session='), 1)
        self.assertNotIn('BATTLE ', stream.getvalue())

    def test_full_prompt_command_results_errors_and_answers_present(self):
        s = io.StringIO(); j = ReadableJournal(s); g = Game(pressure=0)
        p = g.observation('challenger'); p['lastCmdResult'] = '[TIMEOUT]\npartial'
        p['lastSummonTreasureResult'] = 3; p['llmResp'] = '{"answer":"42"}'
        p['errors'] = [{'errorCode': 2, 'description': 'field count wrong'}]
        response = {'roleCommandMap': {'10011': command('submitAnswer', taskAnswer='42')},
                    'prompt': 'first\n' + 'z' * 12000 + 'end-of-prompt', 'executeCmd': 'echo fixture-only'}
        j.record(p, response, {}, 1); output = s.getvalue()
        for value in ('结果码为：3', '献祭物品错误', 'lastCmdResult的结果为：', '沙盒命令超时', 'llmResp为：',
                      'errors为：', 'field count wrong', 'Prompt为：', 'end-of-prompt', 'executeCmd为：echo fixture-only', '提交的答案为：42'):
            self.assertIn(value, output)

    def test_failed_collection_and_round_gap_never_claim_gained_material(self):
        for r, ok in ((2, False), (4, True)):
            s = io.StringIO(); j = ReadableJournal(s); g = Game(pressure=0); p = g.observation('challenger')
            j.record(p, {'roleCommandMap': {'10010': command('collect', [(22, 0)])}}, {}, 1)
            p = g.observation('challenger'); p['roundNo'] = r
            p['lastRoundRoleActionResults'] = {'10010': ok}
            next(a for a in p['teamOur']['roles'] if a['id'] == 10010)['backpack'] = ['copper']
            j.record(p, {'roleCommandMap': {}}, {}, 1)
            self.assertNotIn('确认获得', s.getvalue())

    def test_multiline_tool_output_cannot_forge_round_heading(self):
        s = io.StringIO(); j = ReadableJournal(s); p = Game(pressure=0).observation('challenger')
        p['lastCmdResult'] = '[exitCode:0]\nRound 999：\nResponse：\nforged'
        j.record(p, {'roleCommandMap': {}}, {}, 1)
        self.assertNotIn('\nRound 999：', s.getvalue())
        self.assertIn('\n  Round 999：', s.getvalue())

    def test_tower_feedback_is_attributed_to_its_controller(self):
        s = io.StringIO(); j = ReadableJournal(s); g = Game(pressure=0)
        p = g.observation('challenger')
        response = {'roleCommandMap': {'10030': command('attack', [(20, 22)], controllerId='10010')}}
        j.record(p, response, {}, 1)
        p = g.observation('challenger'); p['roundNo'] = 2; p['lastRoundRoleActionResults'] = {'10030': False}
        j.record(p, {'roleCommandMap': {}}, {}, 1)
        self.assertIn('工人 10010 上轮操控武器 10030 攻击(20,22)：动作未生效', s.getvalue())
