"""Human-oriented stdout records. No raw request dumps or per-turn session/hash noise."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import uuid
from .rules import ACTORS, pos
from .telemetry import ascii_map


class EventJournal:
    def __init__(self, stream=None, map_every=0):
        self.stream = stream if stream is not None else sys.stdout
        self.map_every = max(0, map_every)
        self.last = {}
        self.sequence = 0
        digest = hashlib.sha256()
        for p in sorted(Path(__file__).parent.glob('*.py')):
            digest.update(p.name.encode())
            digest.update(p.read_bytes())
        self.emit({'type': 'start', 'session': uuid.uuid4().hex[:12], 'version': 'v6', 'code': digest.hexdigest()[:12]})

    def emit(self, record):
        text = json.dumps(record, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if len(text) <= 2600:
            self.stream.write('BATTLE ' + text + '\n')
        else:
            self.sequence += 1
            parts = [text[i:i + 1800] for i in range(0, len(text), 1800)]
            for part, data in enumerate(parts):
                self.stream.write('BATTLE_PART ' + json.dumps({'event': self.sequence, 'part': part,
                    'parts': len(parts), 'data': data}, ensure_ascii=False) + '\n')
        self.stream.flush()

    def record(self, payload, response, trace, elapsed_ms):
        team, r = payload['teamOur'], payload['roundNo']
        key = team['teamId'], team['type']
        old = self.last.get(key)
        if old and old['request']['roundNo'] >= r:
            if old['request'] == payload and old['response'] == response:
                return
            old = None
        before, commands = (old['request'], old['response']['roleCommandMap']) if old else ({}, {})
        if before and before['roundNo'] != r - 1:
            commands = {}  # Feedback belongs to r-1, never attribute a gap to an older submission.
        errors = payload.get('errors', [])
        feedback = payload.get('lastRoundRoleActionResults', {})
        def event(kind, **fields):
            self.emit({'type': kind, 'round': r, 'side': team['type'], **fields})
        roles = []
        for actor in team['roles']:
            if actor['roleType'] not in ACTORS:
                continue
            uid = str(actor['id'])
            action = response['roleCommandMap'].get(uid)
            if action is None:
                gun = next(((tower, c) for tower, c in response['roleCommandMap'].items()
                            if c.get('controllerId') == uid), None)
                action = {'action': 'attack', 'tower': gun[0], 'targets': gun[1]['targetPos']} if gun else {'action': 'hold'}
            role = {'id': actor['id'], 'kind': actor['roleType'], 'pos': list(pos(actor['pos'])),
                    'hp': actor['health'], 'bag': dict(Counter(actor.get('backpack', []))), 'action': action}
            if uid in feedback:
                role['last'] = {'action': commands.get(uid, {}).get('action'), 'ok': feedback[uid]}
            roles.append(role)
        event('turn', gold=team.get('goldNum'), score=team.get('totalScore'),
              base=[{'hp': u['health'], 'level': u.get('level', 1)} for u in team['roles'] if u['roleType'] == 'station'],
              roles=roles, task=trace.get('taskState', {}),
              failures=[{'id': uid, 'command': commands.get(str(uid)), 'reason': '动作未生效；具体原因以判题器说明为准'}
                        for uid, ok in feedback.items() if ok is False])
        if errors:
            event('judgeErrors', errors=errors)
        if payload.get('phaseTask') != before.get('phaseTask'):
            event('taskChanged', description=payload.get('phaseTask', ''), task=trace.get('taskState', {}))
        if payload.get('worldNews') != before.get('worldNews') and any(payload.get('worldNews', {}).values()):
            event('news', content=payload['worldNews'])
        for uid, cmd in commands.items():
            if cmd['action'] == 'submitAnswer':
                codes = {e.get('errorCode') for e in errors}
                alive = any(str(u['id']) == uid and u['health'] > 0 for u in team['roles'])
                if 2 in codes:
                    verdict, reason = 'incorrect_or_partial', '答案错误或未全对；判题器未必提供字段级原因'
                elif 1 in codes:
                    verdict, reason = 'timeout', '判题器报告任务超时'
                elif feedback.get(uid) is False:
                    verdict, reason = 'action_failed', '提交动作未生效，不代表答案内容错误'
                elif not payload.get('phaseTask') and feedback.get(uid) is True and alive and not codes:
                    verdict, reason = 'completed_inferred', '提交后任务结束且无错答反馈；完成为推断'
                else:
                    verdict, reason = 'unconfirmed', '缺少足够判题反馈，不能断言答案正确'
                event('answerFeedback', answer=cmd['taskAnswer'], verdict=verdict, reason=reason, judgeErrors=errors)
            if cmd['action'] == 'summonTreasure':
                code = payload.get('lastSummonTreasureResult', 0)
                event('treasureFeedback', code=code, reason={0: '未探测或动作非法', 1: '成功获取宝藏',
                    2: '位置无宝藏或尚未开启', 3: '献祭物品错误，不能多或少', 4: '宝藏已空'}.get(code, '未知结果'))
        for cmd in response['roleCommandMap'].values():
            if cmd['action'] == 'submitAnswer':
                event('answerSubmitted', answer=cmd['taskAnswer'])
            if cmd['action'] == 'summonTreasure':
                event('treasureAttempt', pos=cmd['targetPos'], items=cmd['item'])
        for name in ('lastCmdResult', 'llmResp'):
            value = payload.get(name, '')
            if value:
                event(name, text=value[:10000], omittedChars=max(0, len(value) - 10000))
        if response.get('executeCmd'):
            probe = next((e for e in trace.get('events', []) if e.get('kind') == 'taskProbe'), None)
            cmd = '读取本轮题目并执行已识别的沙盒任务流程' if probe else response['executeCmd']
            event('sandboxCommand', command=cmd[:8000], omittedChars=max(0, len(cmd) - 8000))
        for e in trace.get('events', []):
            if e.get('kind') in ('retreat', 'taskSidestep', 'treasureInference', 'taskProbe'):
                event('decision', detail=e)
        if elapsed_ms >= 500:
            event('slowDecision', ms=round(elapsed_ms, 1))
        if self.map_every and (r - 1) % self.map_every == 0:
            event('map', text=ascii_map(payload))
        self.last[key] = {'request': payload, 'response': response}

    def close(self):
        self.stream.flush()


class ReadableJournal(EventJournal):
    """Chinese request/response blocks by default; JSON remains an explicit local option."""
    def __init__(self, stream=None, map_every=0, output_format='human'):
        self.output_format = output_format
        self.events = []
        super().__init__(stream, map_every)

    def emit(self, record):
        if self.output_format == 'json':
            return super().emit(record)
        if record['type'] == 'start':
            self.stream.write(f"对局启动 session={record['session']} version={record['version']} code={record['code']}\n")
            self.stream.flush()
        else:
            self.events.append(record)

    def record(self, payload, response, trace, elapsed_ms):
        if self.output_format == 'json':
            return super().record(payload, response, trace, elapsed_ms)
        from .human_log import render_round
        key = payload['teamOur']['teamId'], payload['teamOur']['type']
        previous = self.last.get(key)
        if previous and previous['request']['roundNo'] != payload['roundNo'] - 1:
            previous = None
        self.events = []
        super().record(payload, response, trace, elapsed_ms)
        if not self.events:  # Identical retry: no second copy of the same round.
            return
        self.stream.write(render_round(payload, response, previous, self.events, trace))
        self.stream.flush()
