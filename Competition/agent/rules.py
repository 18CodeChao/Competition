"""R02-R06: task book v1.0 §§4.1-4.7. Never tune these as strategy."""
from __future__ import annotations

WIDTH, HEIGHT = 41, 32
DAY_LENGTH, DAYLIGHT, MAX_ROUNDS = 130, 70, 1300
WEAPONS = ("gatling", "railgun", "rocket")
ACTORS = ("worker", "pioneer")
MINERALS = ("stone", "iron", "copper")
RANGES = {"gatling": (3, 5, 7), "railgun": (6, 8, 10), "rocket": (10, 15, 40)}
ROBOT_STATS = {"smallRobot": (40, 5, 1), "middleRobot": (60, 10, 2),
               "largeRobot": (500, 20, 4), "bossRobot": (800, 40, 10)}
SHOP = {"WeaponUpgradeVoucher1": 100, "WeaponUpgradeVoucher2": 150,
        "StationUpgradeVoucher1": 100, "StationUpgradeVoucher2": 150,
        "WallUpgradeVoucher1": 20, "WallUpgradeVoucher2": 30,
        "WallFixer": 10, "Medicine": 10, "DizzyWeapon": 100, "Bomb": 100,
        "SmallRobotSummonOrder": 20, "MiddleRobotSummonOrder": 30,
        "LargeRobotSummonOrder": 100, "BossRobotSummonOrder": 200,
        "AcientTablet": 15, "StarSand": 15, "FlameBreath": 15,
        "FrostPotion": 15, "ThornAmulet": 15, "IronWhistle": 15}
SUMMONS = dict(zip(("SmallRobotSummonOrder", "MiddleRobotSummonOrder",
                    "LargeRobotSummonOrder", "BossRobotSummonOrder"), ROBOT_STATS))
UPGRADES = {f"{prefix}UpgradeVoucher{level}": (kinds, level)
            for prefix, kinds in (("Weapon", WEAPONS), ("Station", ("station",)),
                                  ("Wall", ("wall",))) for level in (1, 2)}
TARGET_ITEMS = set(UPGRADES) | {"WallFixer", "DizzyWeapon", "Bomb"}
STEPS = tuple((x, y) for x in (-1, 0, 1) for y in (-1, 0, 1) if x or y)
Pos = tuple[int, int]


def pos(raw: dict) -> Pos:
    return raw["x"], raw["y"]


def xy(p: Pos) -> dict:
    return {"x": p[0], "y": p[1]}


def distance(a: Pos, b: Pos) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def inside(p: Pos) -> bool:
    return 0 <= p[0] < WIDTH and 0 <= p[1] < HEIGHT


def neighbours(p: Pos) -> list[Pos]:
    return [(p[0] + dx, p[1] + dy) for dx, dy in STEPS
            if inside((p[0] + dx, p[1] + dy))]


def footprint(unit: dict) -> set[Pos]:
    x, y = pos(unit["pos"])
    if unit["roleType"] == "station":
        return {(x, y), (x + 1, y), (x, y - 1), (x + 1, y - 1)}
    return {(x, y)}


def unit_distance(p: Pos, unit: dict) -> int:
    return min(distance(p, cell) for cell in footprint(unit))


def build_ring(base: dict, radius: int) -> set[Pos]:
    """docs/1.jpg: blue ring distance 1 (12), yellow ring distance 2 (20)."""
    x, y = pos(base["pos"])
    return {(a, b) for a in range(x - radius, x + 2 + radius)
            for b in range(y - 1 - radius, y + 1 + radius)
            if inside((a, b)) and unit_distance((a, b), base) == radius}


def max_health(kind: str, level: int = 1) -> int:
    if kind == "station":
        return 1500 * level
    if kind in WEAPONS or kind == "wall":
        return 500 + 500 * level
    if kind in ROBOT_STATS:
        return ROBOT_STATS[kind][0]
    return 220 if kind == "worker" else 200


def reach(unit: dict) -> int:
    # User decision: task book wins over conflicting request sample values.
    return RANGES[unit["roleType"]][unit.get("level", 1) - 1]


def daylight(round_no: int) -> bool:
    return (round_no - 1) % DAY_LENGTH < DAYLIGHT


def command(action: str, targets: list[Pos] | None = None, **fields) -> dict:
    result = {"action": action, **fields}
    if targets is not None:
        result["targetPos"] = [xy(p) for p in targets]
    return result
