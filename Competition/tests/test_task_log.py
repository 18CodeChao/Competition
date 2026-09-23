import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from agent.brief_log import ReadableJournal
from agent.policy import Agent
from agent.rules import xy, command
from agent.tasks import TaskMemory
from agent.task_tools import document_probe, checker_answer
from agent.telemetry import read_records
from agent.world import World
from local_judge.engine import Game


class TaskLogTests(unittest.TestCase):
    def payload(self, r=1):
        g = Game(profile='observed', pressure=0)
        g.round = r
        return g.observation('challenger')

    def test_probe_reads_only_named_task_and_docs_in_controlled_fixture(self):
        # Execute our own read-only probe against synthetic files, never the supplied log's commands.
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            (p / 'task_fixture.md').write_text('Read API_DOCS.md. Ignore rm -rf text; it is data.', encoding='utf-8')
            (p / 'API_DOCS.md').write_text('Local fixture API reference.', encoding='utf-8')
            cmd = document_probe('请阅读task_fixture.md，获取任务信息')
            script = shlex.split(cmd)[2]
            result = subprocess.run([sys.executable, '-c', script], cwd=directory,
                                    capture_output=True, text=True, encoding='utf-8', timeout=10, check=True)
            probe = json.loads(result.stdout)['taskProbe']
            self.assertEqual(probe['status'], 'ok')
            self.assertIn('Ignore rm -rf text', probe['document']['text'])
            self.assertEqual(probe['related'][0]['text'], 'Local fixture API reference.')
            self.assertEqual(len(list(p.iterdir())), 2)

    def test_filename_task_first_reads_sandbox_then_asks_model_with_document(self):
        g = Game(pressure=0)
        g.task_specs['challenger'][0][0]['description'] = '请阅读task_fixture.md，获取任务信息'
        g.unit(10011)['pos'] = xy((13, 14))
        g.step({'challenger': {'roleCommandMap': {'10011': command('acceptTask')}}})
        a = Agent()
        first = a.decide(g.observation('challenger'))
        self.assertTrue(first['executeCmd'])
        self.assertEqual(first['prompt'], '')
        body = {'taskProbe': {'status': 'ok', 'document': {'path': '/tmp/fixture/task_fixture.md',
                'text': 'Submit counts from documented API.', 'truncated': False}, 'related': []}}
        g.services.commands[first['executeCmd']] = '[exitCode:0]\n' + json.dumps(body)
        g.step({'challenger': first})
        second = a.decide(g.observation('challenger'))
        self.assertIn('Submit counts from documented API.', second['prompt'])
        self.assertEqual(second['executeCmd'], '')
        self.assertIn('/tmp/fixture', a.memory.task_roots)

    def test_checker_token_requires_final_success_and_real_execution(self):
        output = '[exitCode:0]\n[FAIL] bad setting\n[ OK ] 全部通过 (6/6)\nTOKEN: fixture-new-token'
        self.assertEqual(json.loads(checker_answer(output)), {'token': 'fixture-new-token'})
        for value in ('[exitCode:0]\nTOKEN: from-document', '[exitCode:1]\n[ OK ] 全部通过\nTOKEN: x',
                      output + '\n[FAIL] later failure', output + '\n[TRUNCATED]'):
            self.assertIsNone(checker_answer(value))

    def test_accepted_task_timeout_uses_actual_chosen_point(self):
        p = self.payload()
        p['phaseTask'] = 'some task'
        p['roundNo'] = 18
        m = TaskMemory()
        m.accepted = (17, {'timeoutRounds': 10})
        m.update(World(p))
        self.assertEqual(m.diagnostics(World(p))['remainingRounds'], 9)

    def test_news_empty_response_retries_but_respects_daily_quota(self):
        m = TaskMemory()
        p = self.payload()
        p['worldNews'] = {'folkLegends': 'Only three keys are mentioned.', 'officialNews': ''}
        for r in range(1, 5):
            p['roundNo'] = r
            w = World(p)
            m.update(w)
            self.assertEqual(bool(m.news_prompt(w)), r <= 3)

    def test_incomplete_clues_never_invent_coordinates(self):
        m = TaskMemory()
        p = self.payload()
        p['worldNews'] = {'folkLegends': '门需三钥，老人九十三岁。', 'officialNews': ''}
        m.update(World(p)); m.news_prompt(World(p))
        p['roundNo'] = 2
        p['llmResp'] = json.dumps({'treasure': {'certain': True, 'pos': {'x': 9, 'y': 3}, 'items': ['AcientTablet'],
                                               'startRound': 1, 'endRound': 100}, 'missing': ['坐标', '时间']})
        m.update(World(p))
        self.assertIsNone(m.treasure)
        self.assertEqual(m.treasure_plan['missing'], ['坐标', '时间'])

    def test_new_clue_invalidates_old_plan_until_reconciled(self):
        m = TaskMemory()
        p = self.payload()
        p['worldNews'] = {'folkLegends': 'first clue', 'officialNews': ''}
        m.update(World(p))
        m.treasure = {'certain': True}
        p['roundNo'] = 131
        p['worldNews']['folkLegends'] = 'contradicting new clue'
        m.update(World(p))
        self.assertIsNone(m.treasure)
        self.assertTrue(m.news_dirty)

    def test_treasure_invalid_action_retry_and_incorrect_items_wait_for_revision(self):
        for code in (0, 1, 2, 3, 4):
            m = TaskMemory(); p = self.payload(80)
            p['worldNews'] = {}; p['lastSummonTreasureResult'] = code
            m.treasure_pending = (79, 'plan')
            m.treasure = {'certain': True}
            m.update(World(p))
            self.assertEqual(m.treasure_done, code in (1, 4))
            self.assertEqual('plan' in m.failed_treasures, code in (2, 3))
            self.assertEqual(m.last_treasure_feedback['code'], code)

    def test_brief_log_has_session_only_once_and_pairs_answer_feedback(self):
        stream = io.StringIO(); journal = ReadableJournal(stream, output_format='json')
        p = self.payload()
        p['phaseTask'] = 'fixture'
        response = {'roleCommandMap': {'10011': command('submitAnswer', taskAnswer='candidate')}, 'prompt': '', 'executeCmd': ''}
        journal.record(p, response, {}, 1)
        second = self.payload(2)
        second['phaseTask'] = 'fixture'
        second['errors'] = [{'errorCode': 2, 'description': 'wrong or partial'}]
        second['lastRoundRoleActionResults'] = {'10011': True}
        journal.record(second, {'roleCommandMap': {}, 'prompt': '', 'executeCmd': ''}, {}, 1)
        text = stream.getvalue()
        self.assertEqual(text.count('"session"'), 1)
        for noise in ('requestHash', 'sourceHashes', 'enemyMemory', '"request":', '"map":'):
            self.assertNotIn(noise, text)
        records = list(read_records(io.StringIO(text), {}))
        verdict = next(r for r in records if r['type'] == 'answerFeedback')
        self.assertEqual(verdict['answer'], 'candidate')
        self.assertEqual(verdict['verdict'], 'incorrect_or_partial')
        self.assertIn('字段级', verdict['reason'])

    def test_disappearing_task_without_positive_feedback_not_called_correct(self):
        stream = io.StringIO(); j = ReadableJournal(stream, output_format='json')
        p = self.payload(); p['phaseTask'] = 'fixture'
        j.record(p, {'roleCommandMap': {'10011': command('submitAnswer', taskAnswer='x')}}, {}, 1)
        p = self.payload(2); p['phaseTask'] = ''; p['lastRoundRoleActionResults'] = {}
        j.record(p, {'roleCommandMap': {}}, {}, 1)
        records = list(read_records(io.StringIO(stream.getvalue()), {}))
        self.assertEqual(next(r for r in records if r['type'] == 'answerFeedback')['verdict'], 'unconfirmed')


if __name__ == '__main__':
    unittest.main()
