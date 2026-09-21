"""Strategy geometry: a shared artillery station with an open rear escape route."""
from collections import deque
from itertools import combinations
from .rules import build_ring, footprint, neighbours, pos, distance


def facing(base):
    return 1 if pos(base["pos"])[0] < 20 else -1


def front_walls(base):
    center = pos(base["pos"])[0] + .5
    sign = facing(base)
    return {p for p in build_ring(base, 2) if (p[0] - center) * sign > 0}


def rear_access(hub, base, blocked):
    """Connectivity, not a promise about enemy movements."""
    ring = build_ring(base, 2)
    rear = ring - front_walls(base) - blocked
    allowed = ring | build_ring(base, 1)
    queue, visited = deque([hub]), {hub}
    while queue:
        p = queue.popleft()
        if p in rear:
            return True
        for q in neighbours(p):
            if q in allowed and q not in blocked and q not in visited:
                visited.add(q)
                queue.append(q)
    return False


def plan_layout(w):
    if not w.base:
        return {"hub": None, "guns": [], "walls": []}
    blue, walls = build_ring(w.base, 1), front_walls(w.base)
    fixed = {
        (9, 22): ((8, 21), [(9, 20), (8, 20), (8, 22)]),
        (30, 10): ((32, 10), [(31, 11), (32, 11), (32, 9)]),
    }
    if pos(w.base["pos"]) in fixed:
        hub, guns = fixed[pos(w.base["pos"])]
        return {"hub": hub, "guns": guns, "walls": sorted(walls)}
    stationary = set(w.zones) | footprint(w.base)
    stationary |= {p for u in w.enemies for p in footprint(u)}
    existing = {pos(u["pos"]) for u in w.weapons}
    sign = facing(w.base)
    best = None
    for hub in sorted(blue - stationary - existing):
        nearby = sorted(p for p in blue - stationary - {hub} if distance(p, hub) == 1)
        for guns in combinations(nearby, 3):
            if not rear_access(hub, w.base, stationary | walls | set(guns) | existing):
                continue
            # Retain existing guns; prefer coverage toward incoming robots then shop access.
            score = (len(set(guns) & existing), sum(p[0] * sign for p in guns),
                     -distance(hub, (20, 16)), tuple(guns))
            if best is None or score > best[0]:
                best = score, hub, guns
    if best:
        _, hub, guns = best
        return {"hub": hub, "guns": list(guns), "walls": sorted(walls)}
    # Nonstandard/existing layout: keep existing structures, use per-turn reachable gun selection.
    return {"hub": None, "guns": sorted(blue - stationary), "walls": sorted(walls)}
