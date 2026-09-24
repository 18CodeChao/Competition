"""Validate the extracted submission via real HTTP using independent battle fixtures."""
import hashlib
import json
import os
from pathlib import Path
import platform
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
from agent.rules import command, xy
from test_news_recon_v8 import LEGENDS, OFFICIAL
from local_judge.engine import make_unit
from test_defense_raids_v7 import home, at, enemy_hole


def main():
    cases, output, errors = [], [], []
    archive = ROOT / 'dist/submission-v8.zip'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='competition-v8-http-') as temp:
        root = Path(temp)
        with zipfile.ZipFile(archive) as package:
            package.extractall(root)
        initial = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        child = subprocess.Popen([sys.executable, '-B', str(root / 'main.py'), str(port)],
                                 cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding='utf-8')
        readers = [threading.Thread(target=lambda: output.extend(iter(child.stdout.readline, ''))),
                   threading.Thread(target=lambda: errors.extend(iter(child.stderr.readline, '')))]
        for reader in readers:
            reader.start()
        try:
            for attempt in range(100):
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        break
                except OSError:
                    if child.poll() is not None:
                        raise RuntimeError('participant exited during startup')
                    time.sleep(.05)
            else:
                raise RuntimeError('participant startup timeout')
            call = http_policy(f'http://127.0.0.1:{port}/')
            for side, site in (('challenger', (12, 21)), ('defender', (28, 10))):
                g, _, offset = home(side, 390)
                call(g.observation(side))
                at(g, side, site)['health'] = 0
                g.round = 391
                payload = g.observation(side)
                response = call(payload)
                expected = command('build', [site], name='wall')
                assert response['roleCommandMap'][str(offset + 12)] == expected
                assert call(payload) == response
                g.step({side: response})
                assert at(g, side, site)['health'] == 1000 or any(
                    u['roleType'] == 'wall' and u['pos'] == xy(site) and u['health'] == 1000
                    for u in g.teams[side]['roles'])
                cases.append({'side': side, 'case': 'dawn-rebuild', 'action': expected, 'duplicateRequestStable': True})
            g, _, _ = home(round_no=465)
            at(g, 'challenger', (12, 21))['health'] = 450
            response = call(g.observation('challenger'))
            expected = command('use', [(12, 21)], name='WallUpgradeVoucher1')
            assert response['roleCommandMap']['10012'] == expected
            cases.append({'case': 'night-upgrade-before-destruction', 'action': expected})
            g, _, _ = home(round_no=391)
            hole = enemy_hole(g)
            g.unit(10011)['pos'] = xy((27, 10))
            response = call(g.observation('challenger'))
            assert response['roleCommandMap']['10011'] == command('move', [hole])
            cases.append({'case': 'daytime-front-breach', 'action': response['roleCommandMap']['10011']})
            g, _, _ = home(round_no=1)
            items = ['AcientTablet', 'StarSand', 'FlameBreath']
            g.unit(10011).update(pos=xy((4, 3)), backpack=list(items))
            for day, story in enumerate(LEGENDS, 1):
                g.round = (day-1)*130+1
                payload = g.observation('challenger')
                payload['worldNews'] = {'officialNews': OFFICIAL, 'folkLegends': story}
                response = call(payload)
                assert not any(c['action'] == 'summonTreasure' for c in response['roleCommandMap'].values())
            g.round = 521
            payload = g.observation('challenger'); payload['worldNews'] = {}
            response = call(payload)
            expected = command('summonTreasure', [(3, 3)], item=items)
            assert response['roleCommandMap']['10011'] == expected
            g.treasure.update(pos=(3, 3), items=items, start=521, end=590)
            g.step({'challenger': response})
            settled = g.observation('challenger')
            assert settled['lastSummonTreasureResult'] == 1
            call(settled)
            cases.append({'case': 'four-day-legend-to-fifth-day-summon', 'action': expected, 'result': 1})
            g, layout, _ = home(round_no=461)
            g.unit(20011)['pos'] = xy(layout['hub'])
            g.unit(10010)['pos'] = xy((7, 21)); g.unit(10012)['pos'] = xy((9, 19))
            for i in range(8):
                g.robots[30000+i] = dict(make_unit(30000+i, 'largeRobot', (17+i%4, 21+i//4)), targetTeam='challenger')
            response = call(g.observation('challenger'))
            attacks = [c for c in response['roleCommandMap'].values() if c['action'] == 'attack']
            assert {c['controllerId'] for c in attacks} == {'10010', '10012'}
            assert not {'10010', '10012'} & set(response['roleCommandMap'])
            cases.append({'case': 'occupied-hub-two-independent-gunners', 'attacks': attacks})
            created = sorted({p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()} - initial)
            assert not created, created
        finally:
            child.terminate()
            child.wait(timeout=10)
            for reader in readers:
                reader.join(timeout=10)
    log = ''.join(output)
    assert log.count('session=') == 1
    assert 'Traceback' not in ''.join(errors)
    report = {'scope': 'Extracted package, real HTTP, synthetic battle fixtures; not official validation',
              'python': platform.python_version(), 'hashes': source_hashes(), 'cases': cases,
              'packageSHA256': hashlib.sha256(archive.read_bytes()).hexdigest(),
              'newParticipantFiles': created, 'startupSessionCount': log.count('session='),
              'stderr': ''.join(errors)}
    (ROOT / 'reports/v8-http.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (ROOT / 'reports/v8-log-example.txt').write_text(log, encoding='utf-8')
    print(json.dumps({'cases': len(cases), 'newParticipantFiles': created, 'startupSessionCount': 1}))


if __name__ == '__main__':
    main()
