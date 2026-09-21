"""R04 damage. Ray endpoint/corner interpretation documented in implementation notes."""
from collections import defaultdict
import heapq
import time
from .rules import distance, pos, reach, neighbours, ROBOT_STATS, unit_distance


def ray_entry(start, end, cell):
    """Segment vs closed unit square; includes corner touches (local assumption A02)."""
    lo, hi = 0.0, 1.0
    for axis in (0, 1):
        delta = end[axis] - start[axis]
        lower, upper = cell[axis] - 0.5, cell[axis] + 0.5
        if delta == 0:
            if not lower <= start[axis] <= upper:
                return None
        else:
            a, b = (lower - start[axis]) / delta, (upper - start[axis]) / delta
            lo, hi = max(lo, min(a, b)), min(hi, max(a, b))
            if lo > hi:
                return None
    return lo


def cone_ok(origin, targets):
    vectors = [(p[0] - origin[0], p[1] - origin[1]) for p in targets]
    return all(x or y for x, y in vectors) and all(
        a[0] * b[0] + a[1] * b[1] >= 0 for a in vectors for b in vectors)


def damage(tower: dict, targets: list[tuple], robots: list[dict]) -> dict[int, int]:
    result = defaultdict(int)
    origin, kind = pos(tower["pos"]), tower["roleType"]
    for target in targets:
        if kind == "rocket":
            for r in robots:
                d = distance(target, pos(r["pos"]))
                if d <= 1:
                    result[r["id"]] += 20 if d == 0 else 10
            continue
        hits = []
        for r in robots:
            t = ray_entry(origin, target, pos(r["pos"]))
            if t is not None and r["health"] > 0:
                hits.append((t, r["id"], r))
        hits.sort(key=lambda h: (h[0], h[1]))
        if kind == "gatling":
            if hits:
                result[hits[0][1]] += 10
        else:
            energy = 10 * tower.get("level", 1)
            for _, rid, robot in hits:
                taken = min(energy, robot["health"])
                result[rid] += taken
                energy -= taken
                if not energy:
                    break
    return dict(result)


def choose_targets(tower, robots, base, side, allocated, deadline=float("inf")):
    if tower["roleType"] == "rocket":
        return rocket_targets(tower, robots, base, side, allocated, deadline)
    origin = pos(tower["pos"])
    kind = tower["roleType"]
    candidates = {pos(r["pos"]) for r in robots}
    if kind == "rocket":
        candidates |= {p for r in robots for p in neighbours(pos(r["pos"]))}
    candidates = sorted(p for p in candidates if 0 < distance(origin, p) <= reach(tower))
    count = 1 if kind == "railgun" else tower.get("level", 1)
    chosen, cumulative = [], dict(allocated)
    effects = {}
    for p in candidates:
        if effects and time.perf_counter() >= deadline:
            break
        effects[p] = damage(tower, [p], robots)
    candidates = list(effects)
    for _ in range(count):
        best, value = None, -1.0
        for p in candidates:
            if best is not None and time.perf_counter() >= deadline:
                break
            if kind == "gatling" and not cone_ok(origin, chosen + [p]):
                continue
            score = 0.0
            for r in robots:
                hp = max(0, r["health"] - cumulative.get(r["id"], 0))
                hit = min(hp, effects[p].get(r["id"], 0))
                threat = (2 if r.get("targetTeam") == side else 0.2)
                if base:
                    threat *= 1 + 6 / max(1, unit_distance(pos(r["pos"]), base))
                score += hit * threat
                if hp and hit >= hp:
                    score += 10 * ROBOT_STATS.get(r["roleType"], (0, 0, 1))[2]
            if score > value:
                best, value = p, score
        if best is None:
            return []
        chosen.append(best)
        for rid, amount in effects[best].items():
            cumulative[rid] = cumulative.get(rid, 0) + amount
    return chosen


def rocket_targets(tower, robots, base, side, allocated, deadline=float("inf")):
    """All useful centers for level 1; width-24 beam for multi-missile combinations.

    Sparse reverse splash index includes empty landing cells. Uses current positions,
    never spawn columns or future waves. Beam search is explicitly approximate.
    """
    origin, radius = pos(tower["pos"]), reach(tower)
    effects = defaultdict(dict)
    remaining, weights, bonuses = {}, {}, {}
    for robot in robots:
        rid, cell = robot["id"], pos(robot["pos"])
        remaining[rid] = max(0, robot["health"] - allocated.get(rid, 0))
        threat = 2.0 if robot.get("targetTeam") in (side, None) else .15
        if base:
            threat *= 1 + 6 / max(1, unit_distance(cell, base))
        weights[rid] = threat
        bonuses[rid] = 10 * ROBOT_STATS.get(robot["roleType"], (0, 0, 1))[2]
        for center in [cell] + neighbours(cell):
            if 0 < distance(origin, center) <= radius:
                effects[center][rid] = 20 if center == cell else 10
    if not effects:
        return []

    def marginal(previous, added):
        result = 0.0
        for rid, amount in added.items():
            hp = remaining[rid]
            old = previous.get(rid, 0)
            hit = max(0, min(hp, old + amount) - min(hp, old))
            result += hit * weights[rid]
            if old < hp <= old + amount:
                result += bonuses[rid]
        return result

    candidates = sorted(effects, key=lambda p: (-marginal({}, effects[p]), p))
    count = tower.get("level", 1)
    beam = [(0.0, (), {})]
    best = []
    for depth in range(count):
        heap = []
        for score, chosen, previous in beam:
            for index in range(chosen[-1] if chosen else 0, len(candidates)):
                center = candidates[index]
                value = score + marginal(previous, effects[center])
                indices = chosen + (index,)
                if len(heap) < 24 or (value, tuple(-i for i in indices)) > heap[0][:2]:
                    accumulated = dict(previous)
                    for rid, amount in effects[center].items():
                        accumulated[rid] = accumulated.get(rid, 0) + amount
                    entry = (value, tuple(-i for i in indices), indices, accumulated)
                    if len(heap) < 24:
                        heapq.heappush(heap, entry)
                    else:
                        heapq.heapreplace(heap, entry)
                if time.perf_counter() >= deadline:
                    break
            if time.perf_counter() >= deadline:
                break
        if not heap:
            break
        beam = [(v, indices, hits) for v, _, indices, hits in heap]
        winner = max(beam, key=lambda s: (s[0], tuple(-i for i in s[1])))
        best = [candidates[i] for i in winner[1]]
        if time.perf_counter() >= deadline:
            break
    if best:
        best += [best[-1]] * (count - len(best))
    return best


def target_metrics(tower, targets, robots, allocated=None):
    allocated = allocated or {}
    hits = damage(tower, targets, robots)
    effective = kills = raw = 0
    for robot in robots:
        hp = max(0, robot["health"] - allocated.get(robot["id"], 0))
        amount = hits.get(robot["id"], 0)
        raw += amount
        effective += min(hp, amount)
        kills += bool(hp and amount >= hp)
    return {"hitRobots": len(hits), "rawDamage": raw, "effectiveDamage": effective,
            "overkill": raw - effective, "predictedKills": kills}
