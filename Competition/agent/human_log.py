"""Readable stdout only. Current commands are intentions, next-round feedback is evidence."""
from collections import Counter
import json
import re
from .rules import ACTORS, distance, pos


NAMES = {'worker': '工人', 'pioneer': '开拓者', 'stone': '石头', 'iron': '铁', 'copper': '铜',
         'rocket': '火箭发射台', 'gatling': '加特林', 'railgun': '电磁炮', 'wall': '围墙',
         'Medicine': '生命药剂', 'WallFixer': '修墙包'}
TREASURE = {0: '未探测或动作非法', 1: '成功获取宝藏', 2: '无宝藏或宝藏尚未开启',
            3: '献祭物品错误，不能多或少', 4: '宝藏已空'}
ERRORS = {0: '未知错误', 1: '任务超时', 2: '答案错误或不完全正确', 3: '网络错误',
          4: '指令错误', 5: 'LLM额度超限'}


def text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def block(label, value):
    # Indent untrusted multiline text so embedded "Round/Response" never looks like a real heading.
    value = text(value)
    lines = value.splitlines() or ['']
    if len(lines) == 1 and len(value) <= 1600:
        return [label + value]
    result = [label]
    for line in lines:
        result.extend('  ' + line[i:i + 1600] for i in range(0, max(1, len(line)), 1600))
    return result


def inventory(bag):
    counts = Counter(bag)
    return '当前背包有 ' + '、'.join(f'{n}个{NAMES.get(k, k)}' for k, n in counts.items()) if counts else '当前背包为空'


def coordinates(targets):
    return '、'.join(f"({p['x']},{p['y']})" for p in targets)


def action_text(cmd):
    a = cmd.get('action', 'hold')
    where = coordinates(cmd.get('targetPos', []))
    item = NAMES.get(cmd.get('name'), cmd.get('name', ''))
    if a == 'move':
        return '移动至' + where
    if a == 'collect':
        return '采集' + where
    if a == 'attack':
        return f"操控武器 {cmd.get('tower', '')} 攻击{where}"
    if a in ('buy', 'sell'):
        return ('购买' if a == 'buy' else '出售') + f"{item}×{cmd.get('num', 1)}"
    if a in ('build', 'remove', 'use'):
        return {'build': '建造', 'remove': '拆除', 'use': '使用'}[a] + item + where
    if a == 'drop':
        return f"丢弃{item}×{cmd.get('num', 1)}{where}"
    if a == 'submitAnswer':
        return '提交答案（见下方）'
    if a == 'summonTreasure':
        return '召唤宝藏' + where + ' 献祭：' + '、'.join(NAMES.get(k, k) for k in cmd.get('item', []))
    return {'acceptTask': '接取任务', 'hold': '原地待命'}.get(a, a)


def command_meaning(value):
    if value.startswith('[TIMEOUT]'):
        result = '沙盒命令超时；输出可能不完整'
    elif value.startswith('[JUDGER_ERROR]'):
        result = '判题器侧执行异常'
    else:
        match = re.match(r'\[exitCode:(-?\d+)\]', value)
        if match and int(match[1]) == 0:
            result = '进程正常退出；不代表任务答案已正确，需结合输出及判题反馈'
        elif match:
            result = f'命令异常退出，退出码{match[1]}；具体原因见输出'
        else:
            result = '非标准结果格式，保留原文，不能推断执行成功'
    if '[TRUNCATED]' in value:
        result += '；判题器已截断输出'
    return result


def render_round(payload, response, previous, events, trace):
    r, team = payload['roundNo'], payload['teamOur']
    lines = ['----------------------------------', f'Round {r}：', 'Request：']
    before = previous['request'] if previous else {}
    old_commands = previous['response'].get('roleCommandMap', {}) if previous else {}
    old_actors = {str(a['id']): a for a in before.get('teamOur', {}).get('roles', [])}
    feedback = payload.get('lastRoundRoleActionResults', {})
    code = payload.get('lastSummonTreasureResult')
    if code is not None and (code != 0 or any(c.get('action') == 'summonTreasure' for c in old_commands.values())):
        lines.append(f'lastSummonTreasureResult的结果码为：{code}，含义为：{TREASURE.get(code, "未知结果码")}')
    if payload.get('lastCmdResult'):
        lines += block('lastCmdResult的结果为：', payload['lastCmdResult'])
        lines.append('含义为：' + command_meaning(payload['lastCmdResult']))
    if payload.get('llmResp'):
        lines += block('llmResp为：', payload['llmResp'])
    if any(payload.get('worldNews', {}).values()):
        lines += block('官方消息和民间新闻为：', payload['worldNews'])
    if payload.get('errors'):
        lines += block('errors为：', payload['errors'])
        lines.append('含义为：' + '；'.join(ERRORS.get(e.get('errorCode'), '未知结果码') for e in payload['errors']))
    if payload.get('phaseTask'):
        lines += block('当前任务内容为：', payload['phaseTask'])
        remaining = trace.get('taskState', {}).get('remainingRounds')
        if remaining is not None:
            lines.append(f'任务剩余回合（本地计时）：{remaining}')
    lines.append(f"资源：金币 {team.get('goldNum', '?')}，积分 {team.get('totalScore', '?')}")
    for base in (a for a in team['roles'] if a['roleType'] == 'station'):
        lines.append(f"基地：等级 {base.get('level', 1)}，血量 {base['health']}")
    for e in events:
        if e['type'] == 'answerFeedback':
            lines += block('上轮提交答案为：', e['answer'])
            lines.append('答案判定：' + e['reason'])
    for actor in team['roles']:
        uid = str(actor['id'])
        if actor['roleType'] not in ACTORS:
            continue
        cmd = old_commands.get(uid)
        feedback_uid = uid
        if cmd is None:
            gun = next(((tid, c) for tid, c in old_commands.items() if str(c.get('controllerId')) == uid), None)
            if gun:
                feedback_uid, cmd = gun[0], dict(gun[1], tower=gun[0])
        if cmd is None or feedback_uid not in feedback:
            continue
        desc = f"{NAMES[actor['roleType']]} {uid} 上轮{action_text(cmd)}："
        if feedback[feedback_uid] is False:
            lines.append(desc + '动作未生效，具体原因以errors为准；没有说明则原因未知')
        elif feedback[feedback_uid] is True:
            desc += '动作合法（效果以本轮状态为准）'
            if cmd['action'] == 'collect' and uid in old_actors:
                gain = Counter(actor.get('backpack', [])) - Counter(old_actors[uid].get('backpack', []))
                desc += '，确认获得 ' + '、'.join(f'{NAMES.get(k, k)}×{n}' for k, n in gain.items()) if gain else '，未观察到背包增加'
            lines.append(desc)
    lines.append('Response：')
    commands = response.get('roleCommandMap', {})
    for actor in team['roles']:
        if actor['roleType'] not in ACTORS:
            continue
        uid = str(actor['id'])
        cmd = commands.get(uid)
        if cmd is None:
            gun = next(((tid, c) for tid, c in commands.items() if str(c.get('controllerId')) == uid), None)
            cmd = dict(gun[1], tower=gun[0]) if gun else {'action': 'hold'}
        if actor['health'] <= 0:
            desc = '死亡，等待复活'
        else:
            desc = action_text(cmd)
            if cmd['action'] != 'hold':
                desc += '（下发，待下轮确认）'
        lines.append(f"{NAMES[actor['roleType']]} {uid} {desc}；位置{coordinates([actor['pos']])} 血量{actor['health']}；{inventory(actor.get('backpack', []))}")
        if cmd['action'] == 'acceptTask':
            tasks = [t for t in team.get('playerTasks', []) if distance(pos(t['taskPosition']), pos(actor['pos'])) <= 2]
            lines.append('任务内容为：待下轮phaseTask返回；附近任务类型：' + '、'.join(t['taskType'] for t in tasks))
        if cmd['action'] == 'submitAnswer':
            lines += block('提交的答案为：', cmd['taskAnswer'])
    if response.get('prompt'):
        lines += block('Prompt为：', response['prompt'])
    if response.get('executeCmd'):
        lines += block('executeCmd为：', response['executeCmd'])
    for e in trace.get('events', []):
        if e.get('kind') == 'answerBlocked':
            lines += block('未提交答案原因：', e)
        elif e.get('kind') in ('retreat', 'taskSidestep'):
            lines += block('避让说明：', e)
    for e in events:
        if e['type'] == 'map':
            lines += block('地图：', e['text'])
    return '\n'.join(lines) + '\n'
