"""Observation only: never imports local_judge or simulator-private state."""
from collections import deque
from .rules import ACTORS, WEAPONS, footprint, pos, daylight, neighbours


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

    def route(self, actor: dict, targets: set[tuple], reserved=frozenset()) -> list[tuple] | None:
        start = pos(actor["pos"])
        blocked = (self.blocked | set(reserved)) - {start}
        goals = targets - blocked
        if not goals:
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

    def adjacent_route(self, actor: dict, cells: set[tuple], reserved=frozenset()):
        goals = {p for c in cells for p in neighbours(c)} - cells
        return self.route(actor, goals, reserved)
