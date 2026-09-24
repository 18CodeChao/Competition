"""Observed-state maintenance and daytime interference; R02/R03/R06/R07.

Threat forecasts and deployment thresholds are strategy estimates, not robot AI rules.
"""
from collections import Counter, deque

from .recon import ReconTactics
from .combat import defensive_robot
from .layout import facing
from .rules import (WEAPONS, ROBOT_STATS, build_ring, command, distance,
                    footprint, max_health, neighbours, pos)


class FieldTactics(ReconTactics):
    def prepare_tactics(self):
        w = self.w
        self.day_no = (w.round - 1) // 130 + 1
        present = {pos(u['pos']) for u in w.ours if u['roleType'] == 'wall'}
        self.seen_walls.update(present & set(self.layout['walls']))
        self.breaches = self.missing_walls() & self.seen_walls
        # A reconnect in day four must not require having observed yesterday's wall.
        if self.day_no >= 4:
            self.breaches |= self.missing_walls()
        self.enemy_base = next((u for u in w.enemies if u['roleType'] == 'station'), None)
        enemy_walls = {pos(u['pos']) for u in w.enemies if u['roleType'] == 'wall'}
        self.seen_enemy_walls.update(enemy_walls)
        self.raid_assignments = {}
        self.raid_kind = None
        self.blocking_breach = False
        self.plan_interference(enemy_walls)

    def operator_post(self):
        """An occupied shared hub must not disable every gun indefinitely."""
        w, hub = self.w, self.layout['hub']
        actor = w.units.get(self.gunner)
        if not actor or hub is None:
            return hub
        path = w.route(actor, {hub}, self.reserved, caution=None)
        if path is not None:
            return hub
        choices = []
        for cell in sorted({p for t in w.weapons for p in neighbours(pos(t['pos']))}):
            route = w.route(actor, {cell}, self.reserved, caution=None)
            if route is None:
                continue
            coverage = sum(distance(cell, pos(t['pos'])) <= 1 for t in w.weapons)
            ready = sum(distance(cell, pos(t['pos'])) <= 1 and t.get('cooldown', 0) == 0
                        for t in w.weapons)
            choices.append((-coverage, -ready, len(route), cell))
        if choices:
            post = min(choices)[-1]
            self.events.append({'kind': 'operatorFallback', 'role': self.gunner,
                                'target': post, 'reason': '公共操炮位被占用或不可达，改用可达炮位'})
            return post
        return hub

    def prepare_support(self):
        self.support_gunner, self.support_post = None, None
        w, hub = self.w, self.layout['hub']
        if not hub or not any(u['roleType'] in ('pioneer', 'worker') and pos(u['pos']) == hub for u in w.enemies):
            return
        actor = w.units.get(self.wall_builder)
        if not actor:
            return
        primary = {t['id'] for t in w.weapons if self.operator_hub and distance(self.operator_hub, pos(t['pos'])) <= 1}
        choices = []
        cells = {p for t in w.weapons for p in neighbours(pos(t['pos']))} - {self.operator_hub}
        for cell in sorted(cells):
            route = w.route(actor, {cell}, self.reserved | {self.operator_hub}, caution=None)
            if route is None:
                continue
            covered = {t['id'] for t in w.weapons if distance(cell, pos(t['pos'])) <= 1}
            if covered:
                choices.append((-len(covered - primary), -len(covered), len(route), cell))
        if choices:
            self.support_gunner, self.support_post = actor['id'], min(choices)[-1]

    def support_turn(self, actor):
        if actor['id'] != self.support_gunner:
            return False
        route = self.w.route(actor, {self.support_post}, self.reserved, caution=None)
        if self.w.day and self.w.left > (len(route) if route is not None else 30) + 8:
            return False
        # Emergency base healing can take a cooldown turn; ordinary shopping and
        # wall patrol cannot pull the recalled operator away from the guns.
        if self.w.base and self.w.base['health'] < .35 * max_health('station', self.w.base.get('level', 1)):
            if self.use_upgrade(actor, defense_only=True):
                return True
        self.events.append({'kind': 'supportGunner', 'role': actor['id'], 'target': self.support_post,
                            'reason': '公共炮位被敌方占据，召回第二工人分担未封堵武器'})
        if route:
            return self.emit(actor, command('move', [route[0]]))
        self.heal(actor)
        return True

    def guard_operator(self, actor):
        if actor['id'] != self.gunner or not self.w.day or not self.layout['hub']:
            return False
        if any(u['roleType'] in ('worker', 'pioneer') and
               distance(pos(u['pos']), self.layout['hub']) <= 5 for u in self.w.enemies):
            return self.walk_exact(actor, self.operator_hub, caution=None)
        return False

    def emergency_rebuild(self, actor):
        w = self.w
        if not w.day or actor['roleType'] != 'worker' or not self.breaches:
            return False
        if 'stone' not in actor.get('backpack', []):
            return False
        # The dedicated repairer gets first choice; an adjacent gunner can fill a
        # second hole without taking a long trip away from the guns.
        options = []
        for cell in sorted(self.breaches - w.blocked - self.reserved - self.jobs):
            route = w.adjacent_route(actor, {cell}, self.reserved)
            if route is None or len(route) >= w.left:
                continue
            if actor['id'] == self.gunner and route:
                continue
            options.append((len(route), cell, route))
        if not options:
            return False
        # When several holes are open and the worker is still at a remote mine,
        # collect a small emergency batch rather than commute for every stone.
        stones = actor.get('backpack', []).count('stone')
        travel = min(o[0] for o in options)
        if travel > 2 and stones < min(3, len(options)) and w.left > travel + 2 * len(options) + 8:
            if self.reserve_stone(actor):
                return True
        face = self.front_face(w.base)
        _, cell, route = min(options, key=lambda o: (o[1] not in face, o[0], o[1]))
        self.jobs.add(cell)
        self.events.append({'kind': 'urgentRebuild', 'role': actor['id'], 'target': cell,
                            'reason': '已知防线缺口优先重建，不等待攒满石头'})
        return self.emit(actor, command('move', [route[0]]) if route else
                         command('build', [cell], name='wall'))

    def incoming_damage(self, building, travel=0):
        w = self.w
        if w.day:
            return 0
        observed = self.intelligence.damage_per_round.get(building['id'], 0)
        # Conservative approach bound, not a claim that every nearby robot will
        # attack this wall. Only the wave aimed at our base is counted.
        approach = sum(ROBOT_STATS.get(r['roleType'], (0, 5, 0))[1] for r in w.robots
                       if defensive_robot(r, w.base, w.side) and r.get('abnormalState') != 'dizzy'
                       and min(distance(pos(r['pos']), p) for p in footprint(building)) <= 3 + travel + 1)
        return max(observed, approach)

    def repair_route(self, actor, building):
        if actor['id'] == self.wall_builder and self.day_no >= 4 and self.w.base:
            inner = build_ring(self.w.base, 1)
            goals = {p for c in footprint(building) for p in neighbours(c)} & inner
            return self.w.route(actor, goals, self.reserved)
        return self.w.adjacent_route(actor, footprint(building), self.reserved)

    def defense_stock(self, actor):
        """Per-person inventory: another actor's voucher does not equip this worker."""
        if actor['id'] != self.wall_builder or self.day_no < 4 or not self.w.base:
            return []
        walls = [u for u in self.w.ours if u['roleType'] == 'wall']
        levels = {u.get('level', 1) for u in walls}
        if self.missing_walls():
            levels.add(1)
        stock = [(f'WallUpgradeVoucher{level}', 2 if level == 1 else 1)
                 for level in sorted(levels) if level < 3]
        # Carry the second step before leaving the shop: after using voucher 1
        # there may be no safe shop trip to obtain voucher 2 that same night.
        if 1 in levels and 2 not in levels:
            stock.append(('WallUpgradeVoucher2', 1))
        stock.append(('WallFixer', 2))
        if self.w.base.get('level', 1) < 3:
            base_item = (f"StationUpgradeVoucher{self.w.base.get('level', 1)}", 1)
            if self.breaches or self.w.base['health'] < .6 * max_health('station', self.w.base.get('level', 1)):
                stock.insert(0, base_item)
            else:
                stock.append(base_item)
        return stock

    def buy_defense_stock(self, actor):
        if not self.w.day:
            return False  # Do not abandon the wall for a night shopping trip.
        w, bag = self.w, Counter(actor.get('backpack', []))
        shops = {c for c, k in w.zones.items() if k == 'weaponShop'}
        route = w.adjacent_route(actor, shops, self.reserved)
        if route is None:
            return False
        destination = route[-1] if route else pos(actor['pos'])
        proxy = dict(actor, pos={'x': destination[0], 'y': destination[1]})
        back = w.route(proxy, self.maintenance_posts(), self.reserved)
        if back is None or len(route) + len(back) + 4 >= w.left:
            return False
        for item, desired in self.defense_stock(actor):
            price = w.shop.get(item)
            if price is None or price <= 0 or bag[item] >= desired:
                continue
            count = min(desired - bag[item], int(self.ledger.gold // price),
                        actor.get('backPackCapability', 100) - len(actor.get('backpack', [])))
            if count <= 0:
                continue
            if route:
                return self.emit(actor, command('move', [route[0]]))
            self.events.append({'kind': 'defenseStock', 'role': actor['id'], 'item': item, 'count': count})
            return self.emit(actor, command('buy', name=item, num=count))
        return False

    def reserve_stone(self, actor):
        w = self.w
        if not w.day or actor['id'] != self.wall_builder:
            return False
        needed = self.stone_target() - actor.get('backpack', []).count('stone')
        if needed <= 0 or len(actor.get('backpack', [])) >= actor.get('backPackCapability', 100):
            return False
        choices = []
        day = self.day_no
        banned = any(c.get('name') == 'stone' and type(c.get('startDay')) is int
                     and type(c.get('endDay')) is int and c['startDay'] <= day <= c['endDay']
                     for c in self.memory.blocked_minerals if isinstance(c, dict))
        if banned:
            return False
        for cell, kind in w.zones.items():
            if kind != 'stone' or self.failed_mines.get(cell, 0) >= w.round:
                continue
            route = w.adjacent_route(actor, {cell}, self.reserved)
            if route is None:
                continue
            destination = route[-1] if route else pos(actor['pos'])
            proxy = dict(actor, pos={'x': destination[0], 'y': destination[1]})
            home = w.route(proxy, self.maintenance_posts(), self.reserved)
            if home is None or len(route) + len(home) + 5 >= w.left:
                continue
            choices.append((len(route), cell, route))
        if not choices:
            return False
        _, cell, route = min(choices)
        return self.emit(actor, command('move', [route[0]]) if route else command('collect', [cell]))

    def maintenance_posts(self):
        if not self.w.base:
            return set()
        sign = facing(self.w.base)
        center = pos(self.w.base['pos'])[0] + .5
        return {p for p in build_ring(self.w.base, 1) if (p[0] - center) * sign > 0}

    def maintenance_duty(self, actor):
        w = self.w
        if actor['id'] != self.wall_builder or self.day_no < 4 or not w.base:
            return False
        posts = self.maintenance_posts()
        route = w.route(actor, posts, self.reserved)
        travel = len(route) if route is not None else 30
        if w.day and w.left > travel + 8:
            return False
        # Adjacent urgent maintenance was attempted earlier in the scheduler.
        # If actually exposed in a breach, preserve the worker using an inner
        # sidestep; never send it outside the wall to service the outer face.
        if self.heal(actor) or self.escape(actor, allowed=posts):
            return True
        walls = [u for u in w.ours if u['roleType'] == 'wall']
        weak = min(walls, key=lambda u: (u['health'] / max(1, self.incoming_damage(u)), u['id']), default=None)
        if weak:
            adjacent = {p for p in posts if distance(p, pos(weak['pos'])) <= 1}
            preferred = w.route(actor, adjacent, self.reserved)
            if preferred is not None:
                route = preferred
        if route:
            self.events.append({'kind': 'maintenancePost', 'role': actor['id'], 'target': route[-1]})
            return self.emit(actor, command('move', [route[0]]))
        return True  # Hold inside even during a lull or when a post is occupied.

    def home_secure(self):
        w = self.w
        if not w.base or len(w.weapons) < 3 or self.missing_walls():
            return False
        return (w.base['health'] >= .75 * max_health('station', w.base.get('level', 1))
                and all(u['health'] >= .75 * max_health('wall', u.get('level', 1))
                        for u in w.ours if u['roleType'] == 'wall'))

    def front_face(self, base):
        ring = build_ring(base, 2)
        edge = (max if facing(base) > 0 else min)(p[0] for p in ring)
        return {p for p in ring if p[0] == edge}

    def enclosure_exits(self, enemy_walls):
        """Require an actual 8-neighbour cut; two visually apparent gaps aren't enough."""
        ring = build_ring(self.enemy_base, 2)
        gaps = ring - enemy_walls
        if len(gaps) not in (1, 2):
            return set()
        inner = build_ring(self.enemy_base, 1)
        outer = build_ring(self.enemy_base, 3)
        static = set(self.w.zones) | footprint(self.enemy_base)
        static |= {p for u in self.w.enemies if u['roleType'] in WEAPONS for p in footprint(u)}
        def reachable(closed):
            visited = set(inner - static)
            queue = deque(visited)
            while queue:
                cell = queue.popleft()
                if cell in outer:
                    return True
                for nxt in neighbours(cell):
                    if nxt in inner | ring | outer and nxt not in visited | static | closed:
                        visited.add(nxt)
                        queue.append(nxt)
            return False
        return gaps if reachable(enemy_walls) and not reachable(enemy_walls | gaps) else set()

    def boss_pressure(self, actor):
        w = self.w
        if not w.day or not self.blocking_breach or not self.home_secure() or self.boss_day == self.day_no:
            return False
        if actor['id'] != self.wall_builder:
            return False
        bag = Counter(actor.get('backpack', []))
        if self.day_no >= 4 and bag['stone'] < 3:
            return False
        reserve = sum(max(0, count - bag[item]) * w.shop.get(item, 100000)
                      for item, count in self.defense_stock(actor))
        if 'BossRobotSummonOrder' in bag:
            if self.emit(actor, command('use', name='BossRobotSummonOrder')):
                self.boss_day = self.day_no  # At most one attempt/day, below R06's limit.
                self.events.append({'kind': 'bossPressure', 'role': actor['id'], 'reason': '已占敌方正面缺口，增援对方下一夜波次'})
                return True
        price = w.shop.get('BossRobotSummonOrder')
        if price is None or self.ledger.gold < price + reserve or w.left <= 2:
            return False
        shops = {c for c, k in w.zones.items() if k == 'weaponShop'}
        route = w.adjacent_route(actor, shops, self.reserved)
        if route is None:
            return False
        proxy = dict(actor, pos={'x': (route[-1] if route else pos(actor['pos']))[0],
                                 'y': (route[-1] if route else pos(actor['pos']))[1]})
        home = w.route(proxy, self.maintenance_posts(), self.reserved)
        if home is None or len(route) + len(home) + 10 >= w.left:
            return False
        return self.emit(actor, command('move', [route[0]]) if route else command('buy', name='BossRobotSummonOrder', num=1))
