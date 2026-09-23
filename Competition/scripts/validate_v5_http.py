"""Local validation only; starts the real server, captures stdout in memory, checks no log files."""
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_judge.__main__ import run, source_hashes


def main():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    output, errors = [], []
    with tempfile.TemporaryDirectory(prefix='competition-v5-http-') as directory:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        child = subprocess.Popen([sys.executable, '-B', str(ROOT / 'main.py'), str(port)], cwd=directory,
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding='utf-8')
        def drain(stream, target):
            target.extend(iter(stream.readline, ''))
        readers = [threading.Thread(target=drain, args=(child.stdout, output)),
                   threading.Thread(target=drain, args=(child.stderr, errors))]
        for reader in readers:
            reader.start()
        try:
            for _ in range(80):
                if child.poll() is not None:
                    raise RuntimeError('server stopped: ' + ''.join(errors))
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.05)
            else:
                raise RuntimeError('server startup timeout')
            games = [run(881, opponent='v4', swapped=side, limit=130,
                         url=f'http://127.0.0.1:{port}/') for side in (False, True)]
            created = [str(p.relative_to(directory)) for p in Path(directory).rglob('*')]
        finally:
            child.terminate()
            child.wait(timeout=10)
            for reader in readers:
                reader.join(timeout=10)
    log = ''.join(output)
    assert log.count('session=') == 1
    assert len(re.findall(r'^Round \d+：$', log, re.M)) == 260
    assert log.count('\nRequest：\n') == 260
    assert log.count('\nResponse：\n') == 260
    assert not created, created
    assert 'Traceback' not in ''.join(errors), ''.join(errors)
    assert all(not g['transportErrors'] and not any(g['exceptions'].values()) for g in games)
    report = {'scope': 'Local HTTP validation; not official task validation', 'python': platform.python_version(),
              'hashes': source_hashes(), 'seed': 881, 'rounds': 260, 'games': games, 'createdFiles': created,
              'startupSessionCount': log.count('session='), 'stdoutBytes': len(log.encode('utf-8')),
              'stdoutLines': len(log.splitlines()), 'stderr': ''.join(errors)}
    (ROOT / 'reports/v5-http.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    blocks = log.split('----------------------------------\n')
    sample = blocks[0] + ''.join('----------------------------------\n' + b for b in blocks[1:]
                               if re.match(r'Round (1|2|7|8|9|10|11|71|80|81)：\n', b))
    (ROOT / 'reports/v5-log-example.txt').write_text(sample, encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('rounds', 'createdFiles', 'startupSessionCount', 'stdoutBytes', 'stdoutLines')}))


if __name__ == '__main__':
    main()
