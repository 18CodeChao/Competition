"""R06/R07: separate news channels, evidence-backed local extraction and LLM fallback."""
import json
import re

from .rules import inside, pos

NUMBER = r'[0-9零〇一二两三四五六七八九十]+'


def number(value):
    if value.isdigit():
        return int(value)
    digits = dict(zip('零〇一二两三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    if '十' in value:
        left, right = value.split('十', 1)
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    return digits.get(value)


def official_effects(records):
    closures, forecasts = [], []
    for record in records:
        text, day = record['text'], record['day']
        if not re.search(r'停工|停采|无法采集', text):
            continue
        minerals = [name for word, name in (('铁矿', 'iron'), ('铜矿', 'copper'), ('石矿', 'stone'), ('采石', 'stone')) if word in text]
        if len(set(minerals)) != 1:
            continue  # Mixed incidents need language reasoning, not a broad regex.
        tomorrow = bool(re.search(r'明[日天]', text))
        duration = re.search(r'(?:需要|持续|停工|停采)[^。；\d零〇一二两三四五六七八九十]{0,8}(' + NUMBER + r')\s*天', text)
        if not tomorrow or not duration:
            continue
        days = number(duration[1])
        if not days or days > 10:
            continue
        effect = {'name': minerals[0], 'startDay': day + 1, 'endDay': min(10, day + days), 'evidence': text}
        closures.append(effect)
        forecasts.append(dict(effect, direction='up', confidence=.95))
    return closures, forecasts


ITEM_CLUES = {
    'AcientTablet': (r'古符石板|铭文石板|石板.{0,25}(?:刻|字)|(?:刻|文字).{0,25}石板', r'石板|真言之印'),
    'StarSand': (r'星辰之沙|银白.{0,16}(?:粉末|细沙)|(?:粉末|细沙).{0,35}(?:发光|冷光)', r'星辰之沙|不灭之光|明光之印'),
    'FlameBreath': (r'烈焰之息|橙红.{0,16}雾', r'烈焰之息|纯净之火|焚天之印'),
    'FrostPotion': (r'寒霜药剂|深蓝.{0,16}液体|瓶口.{0,15}薄霜', r'寒霜药剂|寒霜|纯净之冰'),
    'ThornAmulet': (r'荆棘护符|藤蔓.{0,15}(?:护符|尖刺)', r'荆棘护符|生命之印'),
    'IronWhistle': (r'回音铁哨|生铁.{0,15}哨', r'回音铁哨|回音之印'),
}


def local_treasure(records, shop):
    text = '\n'.join(r['text'] for r in records)
    positions, times, count = set(), set(), set()
    evidence = {}
    # Only explicit coordinate anchors; incidental ages and water levels do not count.
    for m in re.finditer(r'原点[^。\n]{0,90}', text):
        fragment = m[0]
        axes = {}
        for direction, n in re.findall(r'(东|西|北|南)\s*(' + NUMBER + r')\s*(?:公里|千米|格)', fragment):
            value = number(n)
            if value is not None:
                axes['x' if direction in '东西' else 'y'] = value * (-1 if direction in '西南' else 1)
        if set(axes) == {'x', 'y'}:
            positions.add((axes['x'], axes['y']))
            evidence['position'] = fragment
    for m in re.finditer(r'(?:坐标|地点)[^。\n]{0,12}[（(]\s*(\d+)\s*[,，]\s*(\d+)\s*[)）]', text):
        positions.add((int(m[1]), int(m[2])))
        evidence['position'] = m[0]
    for m in re.finditer(r'第(' + NUMBER + r')[日天](白昼|白天|夜晚|夜间)?[^。\n]{0,18}(?:开启|松动|打开|显现)', text):
        day = number(m[1])
        if day and 1 <= day <= 10:
            start = (day - 1) * 130 + 1
            # Unspecified duration: choose the named day's window conservatively.
            times.add((start + (70 if m[2] in ('夜晚', '夜间') else 0), start + (69 if m[2] in ('白昼', '白天') else 129)))
            evidence['time'] = m[0]
    for m in re.finditer(r'门需(' + NUMBER + r')钥|(' + NUMBER + r')道封印', text):
        count.add(number(m[1] or m[2]))
    requirements = '\n'.join(s for s in re.split(r'[。\n]', text) if re.search(r'封印|缺一不可|献祭|需以|门需', s))
    items = [item for item, (description, required) in ITEM_CLUES.items()
             if re.search(description, text) and re.search(required, requirements)]
    missing, conflicts = [], []
    if re.search(r'并非|不在|(?<!是)不是|虚假|不实|谣言|另有|改为|取消|无需|不需要', text):
        conflicts.append('包含否定或修订线索，需语言推理核对')
    if len(positions) != 1 or not all(inside(p) for p in positions):
        missing.append('明确且唯一的地图坐标')
        if len(positions) > 1:
            conflicts.append('坐标线索冲突，等待核对')
    if len(times) != 1:
        missing.append('明确且唯一的开启时间')
        if len(times) > 1:
            conflicts.append('时间线索冲突，等待核对')
    if len(count) != 1 or len(items) != next(iter(count), -1) or not items or any(i not in shop for i in items):
        missing.append('完整献祭物品集合及当前商店映射')
    if items:
        evidence['items'] = requirements
    treasure = None
    if not missing and not conflicts:
        x, y = next(iter(positions)); start, end = next(iter(times))
        treasure = {'certain': True, 'pos': {'x': x, 'y': y}, 'items': items,
                    'startRound': start, 'endRound': end, 'evidence': evidence}
    return {'treasure': treasure, 'missing': missing, 'conflicts': conflicts, 'source': 'localExplicitClues'}


def valid_treasure(treasure, shop):
    try:
        p = pos(treasure['pos']); start, end = treasure['startRound'], treasure['endRound']
        items = treasure['items']
        return (all(type(v) is int for v in (*p, start, end)) and inside(p) and 1 <= start <= end <= 1300
                and isinstance(items, list) and bool(items) and all(isinstance(i, str) and i in shop for i in items))
    except (KeyError, TypeError):
        return False


def grounded(value, records):
    quotes = value if isinstance(value, list) else [value]
    return bool(quotes) and all(isinstance(q, str) and len(q.strip()) >= 3
                               and any(q.strip() in r['text'] for r in records) for q in quotes)


class NewsReasoning:
    def init_news_channels(self):
        self.official_news = []
        self.folk_legends = []
        self.local_news_plan = None

    def ingest_news(self, w):
        incoming = w.raw.get('worldNews') or {}
        changed, folk_changed = {}, False
        for key, records in (('officialNews', self.official_news), ('folkLegends', self.folk_legends)):
            value = incoming.get(key)
            if not isinstance(value, str) or not value.strip() or any(r['text'] == value for r in records):
                continue
            records.append({'round': w.round, 'day': (w.round - 1) // 130 + 1, 'text': value})
            changed[key] = value
            folk_changed |= key == 'folkLegends'
        if not changed:
            return
        self.news.append({'round': w.round, 'content': changed})
        self.news_dirty = True
        self.news_signature = json.dumps(self.news, sort_keys=True, ensure_ascii=False)
        closures, forecasts = official_effects(self.official_news)
        # Preserve validated model inferences for stories outside the local grammar.
        names = {c['name'] for c in closures}
        self.blocked_minerals = [c for c in self.blocked_minerals if c.get('name') not in names] + closures
        self.price_forecasts = [c for c in self.price_forecasts if c.get('name') not in names] + forecasts
        if folk_changed:
            self.local_news_plan = local_treasure(self.folk_legends, w.shop)
            self.treasure_plan = self.local_news_plan
            self.treasure = self.local_news_plan['treasure']
        self.news_updated = True

    def accept_news_reply(self, reply, w):
        self.news_dirty = False
        self.news_updated = True
        self.treasure_plan = reply
        treasure = reply.get('treasure')
        if (isinstance(treasure, dict) and treasure.get('certain') is True
                and isinstance(treasure.get('evidence'), dict)
                and all(grounded(treasure['evidence'].get(k), self.folk_legends) for k in ('position', 'items', 'time'))
                and not reply.get('missing') and not reply.get('conflicts') and valid_treasure(treasure, w.shop)):
            # An unambiguous deterministic extraction cannot be overwritten by a
            # model accidentally using an incidental number in the same story.
            local = (self.local_news_plan or {}).get('treasure')
            self.treasure = local or treasure
        for key, attr in (('closures', 'blocked_minerals'), ('priceForecasts', 'price_forecasts')):
            accepted = []
            for effect in reply.get(key, []) if isinstance(reply.get(key), list) else []:
                if (isinstance(effect, dict) and effect.get('name') in ('iron', 'copper', 'stone')
                        and type(effect.get('startDay')) is int and type(effect.get('endDay')) is int
                        and 1 <= effect['startDay'] <= effect['endDay'] <= 10
                        and grounded(effect.get('evidence'), self.official_news)):
                    accepted.append(effect)
            local_closures, local_prices = official_effects(self.official_news)
            local = local_closures if key == 'closures' else local_prices
            local_names = {c['name'] for c in local}
            if accepted or local:
                setattr(self, attr, [c for c in accepted if c['name'] not in local_names] + local)
