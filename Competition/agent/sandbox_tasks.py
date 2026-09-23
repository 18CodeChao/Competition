"""Source transported to the official sandbox. No filesystem/network work on import."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


HERITAGE_KEYS = {'city', 'total_count', 'world_heritage_count', 'types', 'oldest_era'}


def task_contract(document):
    if './check' in document and re.search(r'["\']token["\']\s*:', document):
        return 'checkerToken'
    if all('"' + key + '"' in document for key in HERITAGE_KEYS) and '遗产名称' in document:
        return 'heritage'
    return None


def era_interval(era):
    """Only unambiguous broad intervals. Overlapping/unknown dates defer to reasoning."""
    periods = {'旧石器时代': (-3000000, -10000), '新石器时代': (-10000, -2000),
               '商': (-1600, -1046), '周': (-1046, -256), '秦': (-221, -206),
               '汉': (-206, 220), '三国': (220, 280), '晋': (266, 420), '南北朝': (420, 589),
               '隋': (581, 618), '唐': (618, 907), '五代': (907, 960), '辽': (916, 1125),
               '宋': (960, 1279), '金': (1115, 1234), '元': (1271, 1368),
               '明': (1368, 1644), '清': (1644, 1912), '民国': (1912, 1949)}
    value = era.strip()
    if value in periods:
        return periods[value]
    if value.endswith('朝') and value[:-1] in periods:
        return periods[value[:-1]]
    year = re.fullmatch(r'(公元前|公元)?\s*(\d{1,4})年?', value)
    if year:
        y = int(year[2]) * (-1 if year[1] == '公元前' else 1)
        return y, y
    # e.g. 明清 / 辽金元明清: range, not lexicographic ordering or a guessed name.
    if value and all(c in periods for c in value):
        ranges = [periods[c] for c in value]
        return min(p[0] for p in ranges), max(p[1] for p in ranges)
    return None


def heritage_statistics(city, records, total):
    if type(total) is not int or total < 1 or len(records) != total:
        raise ValueError('全量记录数与分页总数不一致，或空数据的oldest_era语义未定义')
    fields = ('id', 'name', 'type', 'era', 'protected_level')
    if any(not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k] for k in fields)
           for row in records):
        raise ValueError('记录缺少id/name/type/era/protected_level，禁止用默认空值统计')
    if len({row['id'] for row in records}) != total:
        raise ValueError('分页出现重复id，不能确认已读取全部记录')
    stats = {'city': city, 'total_count': total,
             'world_heritage_count': sum(row['protected_level'] == '世界遗产' for row in records),
             'types': sorted({row['type'] for row in records})}
    dated = [(row, era_interval(row['era'])) for row in records]
    oldest = [row for row, span in dated if span is not None and all(
        other is row or (other_span is not None and span[1] < other_span[0])
        for other, other_span in dated)]
    evidence = {'complete': True, 'statistics': stats,
                'eras': [{'name': row['name'], 'era': row['era']} for row in records]}
    if len(oldest) == 1:
        return dict(stats, oldest_era=oldest[0]['name']), evidence
    evidence['missing'] = '年代未知、交叠或并列；仅需推理最早遗产名称，统计量已确定'
    return None, evidence


def inside_workspace(root, name):
    target = (root / name).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError('规范路径超出当前工作区')
    return target


def repair_deployment(probe, deadline):
    """Apply a narrow declarative spec. Unknown bullet requirements cause a no-write fallback."""
    document = probe['document']['text']
    path = Path(probe['document']['path'])
    cd = re.search(r'`cd\s+([^`\r\n]+)`', document)
    if not cd:
        return {'kind': 'checkerToken', 'status': 'needs_model', 'reason': '题目没有明确工作区cd路径'}
    root = (path.parent / cd[1].strip()).resolve()
    if not root.is_relative_to(path.parent.resolve()) or root == path.parent.resolve():
        return {'kind': 'checkerToken', 'status': 'needs_model', 'reason': '工作区不是题目目录的子目录'}
    spec_path = inside_workspace(root, 'spec.md')
    spec = spec_path.read_text(encoding='utf-8-sig')
    if len(spec.encode('utf-8')) > 16000:
        raise ValueError('部署规范过长，转模型分段阅读')
    probe['related'].append({'path': str(spec_path), 'text': spec, 'truncated': False})
    edits, config, unknown = [], None, []
    for line in spec.splitlines():
        clean = line.strip().replace('**', '').replace('`', '')
        heading = re.match(r'#+\s*配置文件\s+([A-Za-z0-9_./-]+)\s*$', clean)
        if heading:
            config = heading[1]
        if not clean.startswith('-'):
            continue
        clean = clean.lstrip('- ').rstrip('。')
        directory = re.fullmatch(r'([A-Za-z0-9_./-]+/)\s*必须存在[，,]\s*权限为\s*([0-7]{3})', clean)
        setting = re.fullmatch(r'第\s*(\d+)\s*行[：:]\s*(.+)', clean)
        script = re.fullmatch(r'([A-Za-z0-9_./-]+)\s*必须存在且可执行[（(]权限\s*([0-7]{3})[）)]', clean)
        if directory:
            edits.append(('directory', inside_workspace(root, directory[1]), int(directory[2], 8)))
        elif setting and config and 0 < int(setting[1]) <= 10000:
            edits.append(('line', inside_workspace(root, config), int(setting[1]), setting[2]))
        elif script:
            edits.append(('executable', inside_workspace(root, script[1]), int(script[2], 8)))
        else:
            unknown.append(clean)
    checker = inside_workspace(root, 'check')
    if unknown or not edits or any(edit[1] == checker or edit[1].name == 'spec.md' for edit in edits):
        return {'kind': 'checkerToken', 'status': 'needs_model', 'reason': '存在未识别规范或禁止修改的目标', 'unknown': unknown}
    for edit in edits:
        kind, target = edit[:2]
        if kind == 'directory':
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(edit[2])
        elif kind == 'executable':
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(exist_ok=True)
            target.chmod(edit[2])
        else:
            lines = target.read_text(encoding='utf-8').splitlines()
            if len(lines) < edit[2]:
                raise ValueError('配置文件没有规范指定行；停止并报告，不补造其它配置')
            lines[edit[2] - 1] = edit[3]
            target.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    remaining = deadline - time.monotonic()
    if remaining <= .1:
        raise ValueError('本次沙盒预算不足以执行检查器')
    # Run the original checker; CRLF transport repair in memory only, no logic/source changes.
    raw = checker.read_bytes()
    first = raw.splitlines()[0].decode('ascii', 'replace') if raw else ''
    if first in ('#!/bin/sh', '#!/bin/bash'):
        proc = subprocess.run([first[2:], '-c', raw.replace(b'\r\n', b'\n').decode('utf-8'), './check'],
                              cwd=root, capture_output=True, timeout=remaining)
    elif first in ('#!/usr/bin/env python3', '#!/usr/bin/python3'):
        proc = subprocess.run([sys.executable, str(checker)], cwd=root, capture_output=True, timeout=remaining)
    else:
        proc = subprocess.run([str(checker)], cwd=root, capture_output=True, timeout=remaining)
    result = (proc.stdout + proc.stderr).decode('utf-8', 'replace')
    if len(result.encode('utf-8')) > 16000:
        return {'kind': 'checkerToken', 'status': 'needs_model', 'reason': '检查器输出超限', 'output': result[:4000]}
    return {'kind': 'checkerToken', 'status': 'checked', 'workspace': str(root),
            'checkerResult': '[exitCode:%d]\n%s' % (proc.returncode, result),
            'repairs': [str(edit[1].relative_to(root)) for edit in edits]}


def fetch_heritage(probe, deadline, recipes=()):
    document = probe['document']['text']
    city_match = re.search(r'"city"\s*:\s*"([^"\n]+)"', document)
    docs = '\n'.join(d['text'] for d in probe['related'] if not d.get('truncated'))
    urls = re.findall(r'https?://[^\s`"<>]+', docs)
    endpoints = [urllib.parse.urlsplit(u) for u in urls if urllib.parse.urlsplit(u).query]
    if not city_match or not endpoints:
        raise ValueError('无法从当场题目/API文档提取城市与完整查询示例')
    endpoint = endpoints[0]
    if endpoint.scheme != 'http' or endpoint.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('仅调用题目文档中的本机HTTP服务')
    city = city_match[1]
    url = urllib.parse.urlunsplit((endpoint.scheme, endpoint.netloc, endpoint.path, '', ''))
    original_params = dict(urllib.parse.parse_qsl(endpoint.query))
    query_key = next((k for k in ('city', 'location') if k in original_params), None)
    auth = re.search(r'-H\s+["\']([^"\':]+):\s*([^"\']+)["\']', docs)
    if query_key is None or auth is None:
        raise ValueError('没有明确的城市查询参数或鉴权示例，不能猜测')
    header, credential = auth[1].strip(), auth[2].strip()
    credential = credential.removeprefix('Bearer ')
    for recipe in recipes:
        if recipe.get('url') == url and recipe.get('queryKey') in ('city', 'location'):
            query_key = recipe['queryKey']
            header = recipe.get('authHeader', header)
    headers = {header: ('Bearer ' if header == 'Authorization' else '') + credential}
    params = {k: v for k, v in original_params.items() if k not in ('city', 'location', 'offset', 'page')}
    params.update({query_key: city, 'limit': '100'})
    records, observed, seen_pages, offset, total = [], [], set(), 0, None
    recipe = {'url': url, 'queryKey': query_key, 'authHeader': header}
    # No redirects/proxies: response hints can change documented parameters, not destination.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for attempt in range(32):
        remaining = deadline - time.monotonic()
        if remaining < .2:
            raise ValueError('API查询已到本次时间预算，未取得全量数据')
        request = urllib.request.Request(url + '?' + urllib.parse.urlencode(params), headers=headers)
        try:
            response = opener.open(request, timeout=min(2.0, remaining))
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status, raw = response.code, response.read(262145)
        if len(raw) > 262144:
            raise ValueError('单页响应过大，不能截断JSON后计算')
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError('API返回不是对象，需要核对新结构')
        code = body.get('code', status)
        if status != 200 or code != 200 or body.get('status') == 'error':
            message = str(body.get('message', ''))
            observed.append({'httpStatus': status, 'code': code, 'message': message[:1800]})
            if code == 401 and 'Authorization' in message and 'Bearer' in message and 'Authorization' not in headers:
                headers = {'Authorization': 'Bearer ' + credential}
                recipe['authHeader'] = 'Authorization'
                continue
            missing = re.search(r'Missing required parameter:\s*[\'\"]?(city|location)\b', message, re.I)
            if code == 400 and missing and missing[1] not in params:
                params.pop(query_key, None)
                query_key = missing[1]
                params[query_key] = city
                recipe['queryKey'] = query_key
                continue
            return {'kind': 'heritage', 'status': 'needs_model', 'reason': 'API错误需进一步核对', 'observed': observed}
        data = body.get('data', {})
        page, pagination = data.get('records'), data.get('pagination')
        if not isinstance(page, list) or not isinstance(pagination, dict):
            raise ValueError('API未返回data.records和分页信息，禁止按样本推算全量')
        count, current = pagination.get('total_count'), pagination.get('offset')
        if type(count) is not int or type(current) is not int or current != offset or (total is not None and total != count):
            raise ValueError('分页游标或总数不一致')
        total = count
        if not 0 < total <= 20000 or not page:
            raise ValueError('空页或总数超限，不能认定完整')
        signature = json.dumps(page, sort_keys=True, ensure_ascii=False)
        if signature in seen_pages:
            raise ValueError('API重复返回同页，停止而不重复计数')
        seen_pages.add(signature)
        records.extend(page)
        offset += len(page)
        if offset >= total:
            answer, evidence = heritage_statistics(city, records, total)
            return {'kind': 'heritage', 'status': 'complete' if answer else 'needs_era',
                    'answer': answer, 'evidence': evidence, 'recipe': recipe, 'observed': observed,
                    'pages': len(seen_pages), 'requests': attempt + 1}
        params['offset'] = str(offset)
    raise ValueError('本次分页请求数超限，不能提交部分数据为全量')


def solve_probe(probe, recipes=()):
    if probe.get('status') != 'ok' or probe['document'].get('truncated'):
        return None
    kind = task_contract(probe['document']['text'])
    if kind is None:
        return None
    deadline = time.monotonic() + 10.0
    try:
        return repair_deployment(probe, deadline) if kind == 'checkerToken' else fetch_heritage(probe, deadline, recipes)
    except Exception as error:
        return {'kind': kind, 'status': 'needs_model', 'reason': type(error).__name__ + ': ' + str(error)}
