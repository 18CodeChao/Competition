"""Observation only: never imports local_judge or simulator-private state."""
from collections import deque
import heapq
from .rules import reach, distance
from .rules import ACTORS, WEAPONS, footprint, pos, daylight, neighbours, inside


class World:
    def __init__(self, payload: dict):
        self.raw = payload
        self.round = int(payload["roundNo"])
        info = payload["mapInfo"]
        if (info["width"], info["height"]) != (41, 32) or not 1 <= self.round <= 1300:
            raise ValueError("unsupported official map or round")
        self.team = payload["teamOur"]
        self.side = self.team["type"]
        self.day = daylight(self.round)
        self.left = 70 - (self.round - 1) % 130
        self.ours = [u for u in self.team["roles"] if u["health"] > 0]
        self.enemies = [u for u in payload.get("teamEnemy", {}).get("roles", []) if u["health"] > 0]
        self.robots = [u for u in payload.get("robot", {}).get("roles", []) if u["health"] > 0]
        self.units = {u["id"]: u for u in self.ours}
        self.actors = sorted((u for u in self.ours if u["roleType"] in ACTORS), key=lambda u: u["id"])
        self.weapons = [u for u in self.ours if u["roleType"] in WEAPONS]
        self.base = next((u for u in self.ours if u["roleType"] == "station"), None)
        self.zones = {pos(z["pos"]): z["neutralType"] for z in info.get("zones", [])}
        self.blocked = set(self.zones)
        for u in self.ours + self.enemies + self.robots:
            self.blocked.update(footprint(u))
        self.vendor = {s["name"]: s["price"] for s in payload.get("vendorShopList", [])}
        self.shop = {s["name"]: s["price"] for s in payload.get("weaponShopList", [])}
        self.tasks = self.team.get("playerTasks", [])
        self.phase = payload.get("phaseTask") or ""
        self.enemy_weapons = [u for u in self.enemies if u["roleType"] in WEAPONS]
        self.horizon = self.left if self.day else 130 - (self.round - 1) % 130
        self.danger = set()
        self.danger_now = set()
        if not self.day:
            for robot in self.robots:
                if robot.get("abnormalState") == "dizzy":
                    continue
                x, y = pos(robot["pos"])
                self.danger.update((a, b) for a in range(x - 4, x + 5)
                                   for b in range(y - 4, y + 5) if inside((a, b)))
                self.danger_now.update((a, b) for a in range(x - 3, x + 4)
                                       for b in range(y - 3, y + 4) if inside((a, b)))

    def route(self, actor: dict, targets: set[tuple], reserved=frozenset(), caution=True) -> list[tuple] | None:
        start = pos(actor["pos"])
        hazard = set() if caution is None else self.danger if caution else self.danger_now
        blocked = (self.blocked | hazard | set(reserved)) - {start}
        blocked |= {cell for (uid, cell), expiry in getattr(self, "move_avoid", {}).items()
                    if uid == actor["id"] and expiry >= self.round and cell != start}
        hub = getattr(self, "protected_hub", None)
        if hub is not None and actor["id"] != getattr(self, "gunner_id", None) and start != hub:
            blocked.add(hub)
        goals = targets - blocked
        if not goals:
            return None
        if not self.day and self.enemy_weapons and caution is not None:
            # Soft danger cost: global enemy rocket reach must not block the entire map.
            frontier, costs, parent = [(0, start)], {start: 0}, {start: None}
            while frontier:
                cost, cell = heapq.heappop(frontier)
                if cost != costs[cell]:
                    continue
                if cell in goals:
                    path = []
                    while parent[cell] is not None:
                        path.append(cell)
                        cell = parent[cell]
                    return path[::-1]
                for nxt in neighbours(cell):
                    if nxt in blocked:
                        continue
                    candidate = cost + 1 + self.enemy_fire_risk(nxt) / 100
                    if candidate < costs.get(nxt, float("inf")):
                        costs[nxt], parent[nxt] = candidate, cell
                        heapq.heappush(frontier, (candidate, nxt))
            return None
        queue = deque([start])
        parent = {start: None}
        while queue:
            cell = queue.popleft()
            if cell in goals:
                path = []
                while parent[cell] is not None:
                    path.append(cell)
                    cell = parent[cell]
                return path[::-1]
            for nxt in neighbours(cell):
                if nxt not in blocked and nxt not in parent:
                    parent[nxt] = cell
                    queue.append(nxt)
        return None

    def enemy_fire_risk(self, cell):
        # Potential damage, not confirmed targeting/line of fire. User feedback 2026-09-22.
        return sum((20 if t["roleType"] == "rocket" else 10) * t.get("level", 1)
                   for t in self.enemy_weapons
                   if distance(cell, pos(t["pos"])) <= reach(t) + (t["roleType"] == "rocket"))

    def adjacent_route(self, actor: dict, cells: set[tuple], reserved=frozenset(), caution=True):
        goals = {p for c in cells for p in neighbours(c)} - cells
        return self.route(actor, goals, reserved, caution)
