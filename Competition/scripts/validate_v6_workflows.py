"""Test extracted upload package over HTTP using synthetic sandbox tasks, never pasted commands."""
import json
import os
from pathlib import Path
import platform
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from local_judge.__main__ import http_policy, source_hashes
from local_judge.engine import Game
from agent.rules import xy
from test_task_workflows_v6 import deployment_fixture, FixtureAPI


def main():
    stdout, stderr, cases = [], [], []
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='competition-v6-http-') as temp:
        parent = Path(temp); participant = parent / 'participant'; participant.mkdir()
        with zipfile.ZipFile(ROOT / 'dist/submission-v6.zip') as package:
            package.extractall(participant)
        initial = {p.relative_to(participant).as_posix() for p in participant.rglob('*') if p.is_file()}
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        child = subprocess.Popen([sys.executable, '-B', str(participant / 'main.py'), str(port)],
                                 cwd=participant, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding='utf-8')
        def drain(stream, lines): lines.extend(iter(stream.readline, ''))
        readers = [threading.Thread(target=drain, args=(child.stdout, stdout)),
                   threading.Thread(target=drain, args=(child.stderr, stderr))]
        for reader in readers: reader.start()
        try:
            for _ in range(80):
                if child.poll() is not None: raise RuntimeError(''.join(stderr))
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1): break
                except OSError: time.sleep(.05)
            else: raise RuntimeError('server startup timeout')
            call = http_policy(f'http://127.0.0.1:{port}/')
            for kind, side, uid, cell in [('B', 'challenger', 10011, (13, 14)), ('A', 'defender', 20011, (24, 14))]:
                sandbox = parent / kind; sandbox.mkdir()
                with FixtureAPI() as api:
                    if kind == 'B':
                        _, ws = deployment_fixture(sandbox, app='omega', port=7654, token='fixture-omega')
                        checker = (ws / 'check').read_bytes()
                        expected = {'token': 'fixture-omega'}
                    else:
                        probe = api.probe()
                        (sandbox / 'task_fixture.md').write_text(probe['document']['text'], encoding='utf-8')
                        (sandbox / 'API_DOCS.md').write_text(probe['related'][0]['text'], encoding='utf-8')
                        expected = {'city': '测试城', 'total_count': 4, 'world_heritage_count': 2,
                                    'types': ['园林', '建筑', '遗址'], 'oldest_era': '丙址'}
                    game = Game(pressure=0); game.unit(uid)['pos'] = xy(cell)
                    game.task_specs[side][0][0].update(description='请阅读task_fixture.md，获取任务信息', answer=expected)
                    accept = call(game.observation(side))
                    assert accept['roleCommandMap'][str(uid)]['action'] == 'acceptTask'
                    game.step({side: accept})
                    probe_response = call(game.observation(side))
                    assert probe_response['prompt'] == '' and probe_response['executeCmd']
                    invocation = shlex.split(probe_response['executeCmd'])
                    assert invocation[:2] == ['python3', '-c']  # Our generated script only, no copied log shell.
                    begin = time.perf_counter()
                    executed = subprocess.run([sys.executable, '-c', invocation[2]], cwd=sandbox, env=env,
                                              capture_output=True, text=True, encoding='utf-8', timeout=15, check=True)
                    duration = time.perf_counter() - begin
                    result = '[exitCode:0]\n' + executed.stdout
                    game.services.commands[probe_response['executeCmd']] = result
                    game.step({side: probe_response})
                    submission = call(game.observation(side))
                    answer = submission['roleCommandMap'][str(uid)]['taskAnswer']
                    assert isinstance(answer, str) and json.loads(answer) == expected
                    assert not submission['prompt'] and not submission['executeCmd']
                    submitted_round = game.round
                    game.step({side: submission})
                    settled = game.observation(side)
                    assert not settled['phaseTask'] and not settled['errors']
                    call(settled)  # Also test platform log pairing after completion.
                    if kind == 'B': assert (ws / 'check').read_bytes() == checker
                    cases.append({'kind': kind, 'side': side, 'acceptedRound': 1, 'submittedRound': submitted_round,
                                  'settledRound': game.round, 'taskLLMCalls': 0, 'sandboxCalls': 1,
                                  'sandboxMs': round(duration * 1000, 2), 'answer': json.loads(answer),
                                  'taskErrors': settled['errors'], 'apiRequests': len(api.requests)})
            created = sorted({p.relative_to(participant).as_posix() for p in participant.rglob('*') if p.is_file()} - initial)
            assert not created
        finally:
            child.terminate(); child.wait(timeout=10)
            for reader in readers: reader.join(timeout=10)
    log = ''.join(stdout)
    assert log.count('session=') == 1 and 'Traceback' not in ''.join(stderr)
    report = {'scope': 'Extracted package HTTP with synthetic tasks; not official platform validation',
              'python': platform.python_version(), 'hashes': source_hashes(), 'cases': cases,
              'newParticipantFiles': created, 'startupSessionCount': log.count('session='),
              'stdoutBytes': len(log.encode('utf-8')), 'stderr': ''.join(stderr)}
    (ROOT / 'reports/v6-workflows-http.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    blocks = log.split('----------------------------------\n')
    sample = blocks[0] + ''.join('----------------------------------\n' + b for b in blocks[1:] if b.startswith(('Round 3：', 'Round 4：')))
    (ROOT / 'reports/v6-answer-log-example.txt').write_text(sample, encoding='utf-8')
    print(json.dumps({'cases': cases, 'newParticipantFiles': created}, ensure_ascii=False))


if __name__ == '__main__': main()
