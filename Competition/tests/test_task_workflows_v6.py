from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from agent.policy import Agent
from agent.protocol import schema_errors
from agent.rules import command, xy
from agent.sandbox_tasks import heritage_statistics, repair_deployment, fetch_heritage
from agent.task_tools import document_probe
from agent.tasks import TaskMemory
from agent.world import World
from local_judge.engine import Game


def heritage_doc(city='测试城'):
    return ('查询本城全部文化遗产；oldest_era应填写年代最早的遗产名称。\n'
            + json.dumps({'city': city, 'total_count': 0, 'world_heritage_count': 0,
                          'types': [], 'oldest_era': ''}, ensure_ascii=False))


def rows():
    # Independent expected totals: 4 records, 2 world heritage, 3 types, oldest is record 3.
    return [{'id': '1', 'name': '甲馆', 'type': '建筑', 'era': '清', 'protected_level': '世界遗产'},
            {'id': '2', 'name': '乙园', 'type': '园林', 'era': '明', 'protected_level': '全国重点'},
            {'id': '3', 'name': '丙址', 'type': '遗址', 'era': '旧石器时代', 'protected_level': '世界遗产'},
            {'id': '4', 'name': '丁馆', 'type': '建筑', 'era': '唐', 'protected_level': '全国重点'}]


def deployment_fixture(root, app='delta', port=8181, token='fixture-fresh'):
    workspace = root / 'ws_new'
    (workspace / 'config').mkdir(parents=True)
    (workspace / 'config' / (app + '.conf')).write_text('a\nb\nport 0\nc\nd\nname wrong\n', encoding='utf-8')
    (workspace / 'spec.md').write_text(f'''# 应用规范
## 目录要求
- logs/{app}/ 必须存在，权限为 755
## 配置文件 config/{app}.conf
- 第 3 行：`port {port}`
- 第 6 行：`name {app}-svc`
## 脚本要求
- bin/start.sh 必须存在且可执行（权限 755）
''', encoding='utf-8')
    # This is our synthetic checker, never the user's checker or pasted shell command.
    check = ('#!/usr/bin/env python3\nimport pathlib\np=pathlib.Path.cwd()\n'
             f"assert (p/'logs/{app}').is_dir()\n"
             f"lines=(p/'config/{app}.conf').read_text().splitlines()\n"
             f"assert lines[2]=='port {port}' and lines[5]=='name {app}-svc'\n"
             "assert (p/'bin/start.sh').is_file()\n"
             f"print('[ OK ] 全部通过 (6/6)\\nTOKEN: {token}')\n")
    (workspace / 'check').write_text(check, encoding='utf-8')
    task = '进入工作区：`cd ws_new/`\n运行 ./check，提交 {"token":"xxx"}'
    (root / 'task_fixture.md').write_text(task, encoding='utf-8')
    return {'status': 'ok', 'document': {'path': str(root / 'task_fixture.md'), 'text': task, 'truncated': False}, 'related': []}, workspace


class FixtureAPI:
    def __init__(self, repeat=False):
        self.requests = []; self.repeat = repeat
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                params = parse_qs(urlsplit(self.path).query)
                fixture.requests.append((dict(self.headers), params))
                if self.headers.get('Authorization') != 'Bearer fixture-only-key':
                    value = {'code': 401, 'message': "Missing 'Authorization' header. Expected format: 'Authorization: Bearer <api_key>'"}
                elif 'location' not in params:
                    value = {'code': 400, 'message': 'Missing required parameter: location'}
                else:
                    offset = int(params.get('offset', ['0'])[0])
                    if fixture.repeat: offset = 0
                    value = {'code': 200, 'data': {'records': rows()[offset:offset+2],
                             'pagination': {'total_count': 4, 'offset': offset, 'limit': 2}}}
                # Business errors deliberately use HTTP200, as many real APIs do.
                body = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args): pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)

    def __enter__(self): self.thread.start(); return self
    def __exit__(self, *args): self.server.shutdown(); self.server.server_close(); self.thread.join()
    def probe(self):
        url = f'http://127.0.0.1:{self.server.server_port}/fixture/search?city=旧城市'
        return {'status': 'ok', 'document': {'path': '/tmp/fixture/task.md', 'text': heritage_doc()},
                'related': [{'text': f'curl -H "X-API-Key: fixture-only-key" "{url}"', 'truncated': False}]}


class WorkflowV6Tests(unittest.TestCase):
    def test_user_full_log_sample_world_heritage_is_six_not_five(self):
        data = json.loads((Path(__file__).parent / 'fixtures/heritage_public_sample.json').read_text(encoding='utf-8'))['data']
        answer, _ = heritage_statistics('北京', data['records'], data['pagination']['total_count'])
        self.assertEqual(answer['total_count'], 15)
        self.assertEqual(answer['world_heritage_count'], 6)
        self.assertEqual(answer['oldest_era'], '周口店遗址')
        self.assertEqual(set(answer['types']), {'建筑', '园林', '陵墓', '军事防御', '遗址', '宗教建筑', '教育建筑', '桥梁', '城门'})

    def test_generated_probe_fetches_and_calculates_heritage_in_one_sandbox_call(self):
        with FixtureAPI() as api, tempfile.TemporaryDirectory() as directory:
            root = Path(directory); probe = api.probe()
            (root / 'task_fixture.md').write_text(probe['document']['text'], encoding='utf-8')
            (root / 'API_DOCS.md').write_text(probe['related'][0]['text'], encoding='utf-8')
            script = shlex.split(document_probe('请阅读task_fixture.md', solve=True))[2]
            result = subprocess.run([sys.executable, '-c', script], cwd=root, capture_output=True,
                                    text=True, encoding='utf-8', timeout=15, check=True)
            execution = json.loads(result.stdout)['taskProbe']['taskExecution']
            self.assertEqual(execution['status'], 'complete')
            self.assertEqual(execution['answer']['world_heritage_count'], 2)
            self.assertEqual(execution['requests'], 4)
            self.assertEqual(len(list(root.iterdir())), 2)  # API workflow creates no files.

    def test_recipe_is_promoted_only_after_observed_success(self):
        for wrong in (False, True):
            m = TaskMemory(); m.phase = 'fixture'; m.submitted_round = 10
            m.candidate_recipe = {'url': 'http://localhost/fixture', 'queryKey': 'location', 'authHeader': 'Authorization'}
            p = Game(pressure=0).observation('challenger'); p.update(roundNo=11, phaseTask='')
            p['lastRoundRoleActionResults'] = {'10011': True}
            p['errors'] = [{'errorCode': 2}] if wrong else []
            m.update(World(p))
            self.assertEqual(len(m.api_recipes), 0 if wrong else 1)

    def test_statistics_use_all_records_not_sample_and_exact_types(self):
        answer, evidence = heritage_statistics('新城市', rows(), 4)
        self.assertEqual(answer, {'city': '新城市', 'total_count': 4, 'world_heritage_count': 2,
                                 'types': ['园林', '建筑', '遗址'], 'oldest_era': '丙址'})
        self.assertTrue(evidence['complete'])

    def test_incomplete_duplicate_or_missing_fields_never_produce_statistics(self):
        for records in (rows()[:1], rows()[:3] + [rows()[0]], [dict(r) for r in rows()]):
            if len(records) == 4 and records[-1]['id'] == '4': records[-1].pop('protected_level')
            with self.assertRaises(ValueError): heritage_statistics('城', records, 4)

    def test_era_ambiguity_keeps_exact_statistics_for_model(self):
        data = rows(); data[2]['era'] = '史前，具体年代不详'
        answer, evidence = heritage_statistics('城', data, 4)
        self.assertIsNone(answer)
        self.assertEqual(evidence['statistics']['world_heritage_count'], 2)
        self.assertEqual(len(evidence['eras']), 4)

    def test_api_corrects_real_error_hints_fetches_all_pages_and_reuses_recipe(self):
        with FixtureAPI() as api:
            result = fetch_heritage(api.probe(), time.monotonic() + 5)
            self.assertEqual(result['answer']['world_heritage_count'], 2)
            self.assertEqual(result['answer']['oldest_era'], '丙址')
            self.assertEqual(result['requests'], 4)  # 401,400,page1,page2, one sandbox command.
            self.assertEqual(result['pages'], 2)
            self.assertEqual(api.requests[-1][1]['location'], ['测试城'])
            second = fetch_heritage(api.probe(), time.monotonic() + 5, [result['recipe']])
            self.assertEqual(second['requests'], 2)

    def test_repeated_api_page_is_not_silently_double_counted(self):
        with FixtureAPI(repeat=True) as api:
            with self.assertRaisesRegex(ValueError, '分页游标'):
                fetch_heritage(api.probe(), time.monotonic() + 5)

    def test_non_loopback_document_destination_is_not_called(self):
        probe = {'document': {'text': heritage_doc()}, 'related': [{'text': 'curl -H "X-Key: fixture" "http://outside.invalid/api?city=x"'}]}
        with self.assertRaisesRegex(ValueError, '本机HTTP'):
            fetch_heritage(probe, time.monotonic() + 5)

    def test_generated_sandbox_probe_solves_deployment_from_spec_without_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); _, workspace = deployment_fixture(root, app='delta', port=8181)
            original = (workspace / 'check').read_bytes()
            script = shlex.split(document_probe('请阅读task_fixture.md', solve=True))[2]
            proc = subprocess.run([sys.executable, '-c', script], cwd=root, capture_output=True,
                                  text=True, encoding='utf-8', timeout=15, check=True,
                                  env=dict(__import__('os').environ, PYTHONIOENCODING='utf-8'))
            probe = json.loads(proc.stdout)['taskProbe']
            self.assertEqual(probe['taskExecution']['status'], 'checked')
            self.assertIn('TOKEN: fixture-fresh', probe['taskExecution']['checkerResult'])
            self.assertEqual((workspace / 'check').read_bytes(), original)
            self.assertIn('spec.md', probe['related'][0]['path'])

    def test_crlf_shell_checker_is_run_with_original_path_without_rewriting_file(self):
        with tempfile.TemporaryDirectory() as temp:
            probe, workspace = deployment_fixture(Path(temp))
            raw = b'#!/bin/sh\r\nprintf "fixture"\r\n'
            (workspace / 'check').write_bytes(raw)
            fake = subprocess.CompletedProcess([], 0, b'[ OK ] all passed\nTOKEN: fixture-token\n', b'')
            with patch('agent.sandbox_tasks.subprocess.run', return_value=fake) as run:
                repair_deployment(probe, time.monotonic() + 5)
                args = run.call_args.args[0]
                self.assertEqual(args[:2], ['/bin/sh', '-c'])
                self.assertEqual(args[-1], './check')
                self.assertNotIn('\r', args[2])
            self.assertEqual((workspace / 'check').read_bytes(), raw)

    def test_unknown_spec_does_not_partially_apply_repairs(self):
        with tempfile.TemporaryDirectory() as temp:
            probe, workspace = deployment_fixture(Path(temp))
            with (workspace / 'spec.md').open('a', encoding='utf-8') as f: f.write('- 安装未知插件\n')
            result = repair_deployment(probe, time.monotonic() + 5)
            self.assertEqual(result['status'], 'needs_model')
            self.assertFalse((workspace / 'logs').exists())
            self.assertIn('port 0', (workspace / 'config/delta.conf').read_text())

    def test_task_point_flow_submits_token_on_first_result_round(self):
        g = Game(pressure=0); g.unit(10011)['pos'] = xy((13, 14))
        g.task_specs['challenger'][0][0]['description'] = '请阅读task_fixture.md'
        a = Agent()
        accept = a.decide(g.observation('challenger'))
        self.assertEqual(accept['roleCommandMap']['10011']['action'], 'acceptTask')
        g.step({'challenger': accept})
        reading = a.decide(g.observation('challenger'))
        self.assertEqual(reading['prompt'], '')
        with tempfile.TemporaryDirectory() as temp:
            probe, _ = deployment_fixture(Path(temp))
        probe['taskExecution'] = {'kind': 'checkerToken', 'checkerResult': '[exitCode:0]\n[ OK ] 全部通过 (6/6)\nTOKEN: fresh-only'}
        p = g.observation('challenger'); p['roundNo'] += 1
        p['lastCmdResult'] = '[exitCode:0]\n' + json.dumps({'taskProbe': probe})
        response = a.decide(p)
        answer = response['roleCommandMap']['10011']['taskAnswer']
        self.assertIsInstance(answer, str)
        self.assertEqual(json.loads(answer), {'token': 'fresh-only'})
        self.assertEqual(response['prompt'], '')
        self.assertEqual(schema_errors(response), [])

    def test_fallback_checker_auto_submission_without_model_opt_in(self):
        g = Game(pressure=0); p = g.observation('challenger'); p['roundNo'] = 93
        p['phaseTask'] = 'fixture'; p['lastCmdResult'] = '[exitCode:0]\n[ OK ] 全部通过 (6/6)\nTOKEN: fresh-check'
        m = TaskMemory(); m.phase = 'fixture'; m.contract = 'checkerToken'
        m.executed_round = 92; m.last_command = 'cd current-workspace && ./check'
        m.update(World(p))
        self.assertEqual(json.loads(m.command_answer), {'token': 'fresh-check'})
        self.assertEqual(json.loads(m.normalize_answer('wrong-token')), {'token': 'fresh-check'})
        m.verified_token_answer = None
        self.assertIsNone(m.normalize_answer({'token': 'unobserved-token'}))

    def test_old_phase_stale_or_failed_checker_not_accepted(self):
        for age, phase, result in ((91, 'fixture', '[exitCode:0]\n[ OK ] 全部通过\nTOKEN: old'),
                                   (92, 'new-task', '[exitCode:0]\n[ OK ] 全部通过\nTOKEN: old'),
                                   (92, 'fixture', '[exitCode:1]\n[ OK ] 全部通过\nTOKEN: bad')):
            m = TaskMemory(); m.phase = 'fixture'; m.contract = 'checkerToken'; m.executed_round = age
            m.last_command = './check'
            p = Game(pressure=0).observation('challenger'); p.update(roundNo=93, phaseTask=phase, lastCmdResult=result)
            m.update(World(p)); self.assertIsNone(m.command_answer)

    def test_model_cannot_override_verified_counts_or_submit_sample_based_guess(self):
        m = TaskMemory(); m.contract = 'heritage'
        self.assertIsNone(m.normalize_answer({'oldest_era': '丙址', 'world_heritage_count': 999}))
        _, m.heritage_evidence = heritage_statistics('城', rows(), 4)
        answer = json.loads(m.normalize_answer({'oldest_era': '丙址', 'world_heritage_count': 999, 'types': ['瞎猜']}))
        self.assertEqual(answer['world_heritage_count'], 2)
        self.assertEqual(answer['types'], ['园林', '建筑', '遗址'])
        self.assertIsNone(m.normalize_answer({'oldest_era': '旧石器时代'}))

    def test_full_api_result_can_recover_model_fallback_and_submit_without_extra_reasoning(self):
        m = TaskMemory(); m.phase = 'fixture'; m.contract = 'heritage'; m.executed_round = 10
        m.task_documents = {'document': {'text': heritage_doc()}}
        p = Game(pressure=0).observation('challenger'); p.update(roundNo=11, phaseTask='fixture')
        p['lastCmdResult'] = '[exitCode:0]\n' + json.dumps({'code': 200, 'data': {'records': rows(), 'pagination': {'offset': 0, 'total_count': 4}}})
        m.update(World(p))
        self.assertEqual(json.loads(m.command_answer)['world_heritage_count'], 2)


if __name__ == '__main__': unittest.main()
