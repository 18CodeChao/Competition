"""R04 damage. Ray endpoint/corner interpretation documented in implementation notes."""
from collections import defaultdict
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
