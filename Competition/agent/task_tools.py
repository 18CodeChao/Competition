"""Build a read-only command for the official sandbox; never run it in the agent."""
import json
import re
import shlex


def task_filename(description):
    match = re.search(r"(?<![A-Za-z0-9_/.-])((?:/tmp/)?[A-Za-z0-9_./-]+\.(?:md|txt|rst))(?![A-Za-z0-9_.])", description)
    return match.group(1) if match else None


def document_probe(description, known_roots=()):
    name = task_filename(description)
    if not name:
        return None
    # The filename is JSON data embedded in Python source, then POSIX-shell quoted.
    # Search only current task location, previously observed task directories and /tmp.
    script = '''import json, os, pathlib, time, sys
sys.stdout.reconfigure(encoding='utf-8')
name, learned = INPUT
started = time.monotonic()
cwd = pathlib.Path.cwd()
roots = [pathlib.Path(p) for p in learned if str(p).startswith('/tmp/')]
roots += [cwd, pathlib.Path('/tmp')]
found = []
requested = pathlib.Path(name)
if '..' not in requested.parts:
    for root in roots:
        candidate = requested if requested.is_absolute() else root / requested
        if candidate.is_file() and (not candidate.is_absolute() or str(candidate).startswith('/tmp/') or cwd != pathlib.Path('/')):
            found.append(candidate.resolve())
    if not found:
        visited = 0
        for root in roots:
            if root == pathlib.Path('/'):
                continue
            for directory, dirs, files in os.walk(root, followlinks=False):
                visited += 1
                dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not pathlib.Path(directory, d).is_symlink())
                if len(pathlib.Path(directory).relative_to(root).parts) >= 6:
                    dirs[:] = []
                if requested.name in files:
                    found.append(pathlib.Path(directory, requested.name).resolve())
                if visited >= 6000 or time.monotonic() - started > 3:
                    break
            if visited >= 6000 or time.monotonic() - started > 3:
                break
found = sorted(set(found), key=str)
result = {'status': 'missing', 'requested': name}
if len(found) > 1:
    result = {'status': 'ambiguous', 'candidates': [str(p) for p in found[:12]]}
elif found:
    path = found[0]
    def read(p, limit):
        with p.open('rb') as stream:
            data = stream.read(limit + 1)
        return {'path': str(p), 'text': data[:limit].decode('utf-8', 'replace'), 'truncated': len(data) > limit}
    result = {'status': 'ok', 'document': read(path, 20000), 'related': []}
    for filename in ('API_DOCS.md', 'spec.md', 'README.md'):
        p = path.parent / filename
        if p.is_file() and p != path:
            result['related'].append(read(p, 6000))
print(json.dumps({'taskProbe': result}, ensure_ascii=False))
'''.replace("INPUT", repr((name, list(known_roots))))
    return "python3 -c " + shlex.quote(script)


def sandbox_body(result):
    if not result.startswith("[exitCode:0]\n") or "[TRUNCATED]" in result:
        return None
    return result.split("\n", 1)[1]


def checker_answer(result):
    """Only for an explicitly requested token checker format, not arbitrary document tokens."""
    body = sandbox_body(result)
    if body is None:
        return None
    lines = body.splitlines()
    success = [i for i, s in enumerate(lines) if re.search(r"\[\s*OK\s*\].*(?:全部通过|all\s+passed)", s, re.I)]
    if not success or any('[FAIL]' in s for s in lines[success[-1] + 1:]):
        return None
    tokens = [m.group(1) for s in lines[success[-1] + 1:]
              if (m := re.fullmatch(r"\s*TOKEN\s*[:=]\s*([A-Za-z0-9_-]+)\s*", s))]
    return json.dumps({'token': tokens[0]}) if len(tokens) == 1 else None
