"""R02/R07: observed weapon memory, timed rear staging and safe blockade choices."""
from collections import Counter
from itertools import combinations

from .layout import facing
from .rules import WEAPONS, build_ring, command, distance, footprint, neighbours, pos


class ReconTactics:
    def cooling_tasks(self):
        return [t for t in self.w.tasks if not t.get('isValid')
                and isinstance(t.get('coldDownRounds'), int) and t['coldDownRounds'] > 0
                and self.w.zones.get(pos(t['taskPosition']), '').startswith(self.w.side)]

    def known_enemy_guns(self):
        return [r['unit'] for r in self.intelligence.enemies.values()
                if r['unit']['roleType'] in WEAPONS and not r['missingAtLastPosition']]

    def enemy_rear_cells(self):
        if not self.enemy_base:
            return set()
        center = pos(self.enemy_base['pos'])[0] + .5
        sign = facing(self.enemy_base)
        cells = set().union(*(build_ring(self.enemy_base, radius) for radius in (2, 3, 4)))
        return {p for p in cells if (p[0] - center) * sign < -2}

    def raid_route(self, actor, targets):
        # A free scout may take a detour; unlike the home gunner it must never
        # deliberately move into either wave's predicted immediate path.
        forbidden = self.w.danger if not self.w.day else set()
        return self.w.route(actor, set(targets), self.reserved | forbidden)

    def enemy_robot_distance(self):
        return min((distance(pos(r['pos']), cell) for r in self.w.robots
                    for cell in footprint(self.enemy_base)), default=1000)

    def rear_assignment(self, pioneer):
        cells = self.enemy_rear_cells() - self.w.danger
        route = self.raid_route(pioneer, cells)
        if route is None:
            return False
        self.raid_assignments[pioneer['id']] = route[-1] if route else pos(pioneer['pos'])
        self.raid_kind = 'rearWait'
        return True

    def target_trip(self, actor, cell):
        route = self.raid_route(actor, {cell})
        if route is None:
            return None
        proxy = dict(actor, pos={'x': cell[0], 'y': cell[1]})
        rear = self.raid_route(proxy, self.enemy_rear_cells())
        if rear is None:
            return None
        if self.w.day:
            if len(route) + len(rear) + 4 >= self.w.left:
                return None
        elif self.enemy_robot_distance() <= 4 + len(route) + 2:
            return None  # Empty now is insufficient if robots can arrive during the trip.
        return route

    def scout_once(self, pioneer):
        """At most the first 35 rounds; active tasks always remain protected."""
        blue = build_ring(self.enemy_base, 1)
        vision = {cell for u in self.w.ours for cell in footprint(u)}
        self.scouted_cells.update(p for p in blue if any(distance(p, c) <= 4 for c in vision))
        if blue <= self.scouted_cells or len(self.known_enemy_guns()) >= 3 or self.w.round > 35:
            self.scout_done = True
        self.first_scout = self.day_no == 1 and not self.scout_done and self.w.day
        if not self.first_scout:
            return False
        unknown = blue - self.scouted_cells
        choices = []
        # Scout from the rear or flank; news/walls/robots require no reconnaissance.
        candidates = self.enemy_rear_cells() | build_ring(self.enemy_base, 3)
        for cell in sorted(candidates - self.w.blocked | ({pos(pioneer['pos'])} & candidates)):
            coverage = sum(distance(cell, p) <= 4 for p in unknown)
            if not coverage:
                continue
            route = self.target_trip(pioneer, cell)
            if route is not None and self.w.round + len(route) <= 35:
                choices.append((-coverage, len(route), cell))
        if choices:
            self.raid_kind = 'firstScout'
            self.raid_assignments[pioneer['id']] = min(choices)[2]
            return True
        self.scout_done, self.first_scout = True, False
        return False

    def plan_interference(self, enemy_walls):
        w = self.w
        self.first_scout = self.raid_safety = False
        pioneer = next((a for a in w.actors if a['roleType'] == 'pioneer'), None)
        if not pioneer or w.phase or not self.enemy_base:
            return
        # Compute the real retreat path while it is still daytime. The extra
        # four rounds cover collisions and newly observed occupants.
        rear = self.raid_route(pioneer, self.enemy_rear_cells())
        near_enemy = min(distance(pos(pioneer['pos']), c) for c in footprint(self.enemy_base)) <= 12
        retreat_cost = len(rear) if rear is not None else min((distance(pos(pioneer['pos']), p) for p in self.enemy_rear_cells()), default=30) + 4
        too_late = w.day and w.left <= retreat_cost + 4
        unsafe_night = not w.day and (self.enemy_robot_distance() <= 4 or (w.round - 1) % 130 < 74)
        if near_enemy and (too_late or unsafe_night or pioneer['health'] < 100):
            self.raid_safety = True
            self.rear_assignment(pioneer)
            return
        if self.treasure_trip(pioneer) is not None:
            return
        if self.scout_once(pioneer):
            return
        # isValid=false means unavailable now; do not permanently mark a cooling
        # point finished. Recheck every observation, including after scouting.
        if any(t.get('isValid') and w.zones.get(pos(t['taskPosition']), '').startswith(w.side) for t in w.tasks):
            return
        if self.cooling_tasks():
            return
        if pioneer['health'] < 100:
            self.rear_assignment(pioneer)
            return
        face = self.front_face(self.enemy_base)
        holes = {p for p in face - enemy_walls if p in self.seen_enemy_walls or
                 ((p[0], p[1]-1) in enemy_walls and (p[0], p[1]+1) in enemy_walls)}
        gaps = self.enclosure_exits(enemy_walls)
        worker = w.units.get(self.wall_builder)
        if len(gaps) == 1:
            cell = next(iter(gaps))
            if self.target_trip(pioneer, cell) is not None:
                self.raid_assignments[pioneer['id']] = cell
                self.raid_kind = 'oneExit'
        elif len(gaps) == 2 and worker and w.day and self.home_secure() and not self.support_gunner:
            bag = Counter(worker.get('backpack', []))
            equipped = self.day_no < 4 or (bag['stone'] >= 3 and all(bag[n] >= min(1, count) for n, count in self.defense_stock(worker)))
            pairs = []
            if equipped:
                for first in sorted(gaps):
                    second = next(p for p in gaps if p != first)
                    a = self.target_trip(pioneer, first)
                    b = w.route(worker, {second}, self.reserved)
                    proxy = dict(worker, pos={'x': second[0], 'y': second[1]})
                    home = w.route(proxy, self.maintenance_posts(), self.reserved)
                    if a is not None and b is not None and home is not None and max(len(a), len(b)) + len(home) + 10 < w.left:
                        pairs.append((len(a) + len(b), first, second))
                if pairs:
                    _, first, second = min(pairs)
                    self.raid_assignments = {pioneer['id']: first, worker['id']: second}
                    self.raid_kind = 'twoExits'
        if not self.raid_assignments:
            guns = self.known_enemy_guns()
            shared = set().union(*(set(neighbours(pos(a['pos']))) & set(neighbours(pos(b['pos'])))
                                  for a, b in combinations(guns, 2))) if len(guns) >= 2 else set()
            # User priority: shared weapon post precedes a front wall breach.
            for kind, cells in (('operatorBlock', shared), ('frontBreach', holes)):
                choices = []
                for cell in sorted(cells):
                    route = self.target_trip(pioneer, cell)
                    if route is not None:
                        coverage = sum(distance(cell, pos(t['pos'])) == 1 for t in guns) if kind == 'operatorBlock' else 0
                        choices.append((-coverage, len(route), cell))
                if choices:
                    self.raid_kind = kind
                    self.raid_assignments[pioneer['id']] = min(choices)[2]
                    break
        if not self.raid_assignments:
            self.rear_assignment(pioneer)
        cell = self.raid_assignments.get(pioneer['id'])
        self.blocking_breach = self.raid_kind != 'rearWait' and cell in holes and pos(pioneer['pos']) == cell

    def interference(self, actor):
        target = self.raid_assignments.get(actor['id'])
        if target is not None:
            path = self.raid_route(actor, {target}) if actor['roleType'] == 'pioneer' else self.w.route(actor, {target}, self.reserved)
            if path is None:
                return False
            self.raiders.add(actor['id'])
            self.events.append({'kind': 'interference', 'role': actor['id'], 'mode': self.raid_kind,
                                'target': target, 'holding': not path})
            return self.emit(actor, command('move', [path[0]])) if path else True
        if actor['id'] not in self.raiders:
            return False
        # A second worker returns to our wall. The pioneer stages behind the
        # opponent instead of crossing the incoming wave on its way home.
        goals = self.maintenance_posts() if actor['roleType'] == 'worker' else self.enemy_rear_cells()
        path = self.raid_route(actor, goals)
        if path:
            self.events.append({'kind': 'raidWithdraw', 'role': actor['id'], 'reason': '工人回防；开拓者转至敌方基地背面避开波次'})
            return self.emit(actor, command('move', [path[0]]))
        if path == []:
            self.raiders.discard(actor['id'])
            return actor['roleType'] == 'pioneer'
        return False

    def pioneer_turn(self, actor):
        if self.raid_safety:
            if self.heal(actor) or self.interference(actor):
                return
            self.escape(actor)
            return
        if self.heal(actor) or self.treasure(actor):
            self.raiders.discard(actor['id'])
            return
        if self.first_scout and self.interference(actor):
            return
        if self.accept_task(actor):
            self.raiders.discard(actor['id'])
            return
        cooling = self.cooling_tasks()
        if cooling:
            # A cooling point still has work to return to. Use a short interval
            # for upgrades instead of making a full cross-map scouting commute.
            self.raiders.discard(actor['id'])
            if self.use_upgrade(actor) or self.procure(actor):
                return
            self.walk(actor, {pos(t['taskPosition']) for t in cooling})
            return
        if self.escape(actor) or self.interference(actor) or self.procure(actor):
            return
        if self.w.base:
            self.walk(actor, footprint(self.w.base))
