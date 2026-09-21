"""Observation-backed history. Last seen is not current omniscient knowledge."""
from copy import deepcopy
from .rules import WEAPONS, footprint, pos, distance, ROBOT_STATS


class Intelligence:
    def __init__(self):
        self.enemies = {}
        self.prices = {}
        self.damage_per_round = {}
        self.previous_hp = {}
        self.previous_round = None
        self.enemy_wave = []
        self.previous_enemy_hp = None

    def update(self, w):
        elapsed = max(1, w.round - self.previous_round) if self.previous_round is not None else 1
        for unit in w.ours:
            uid, hp = unit["id"], unit["health"]
            lost = max(0, self.previous_hp.get(uid, hp) - hp) / elapsed
            self.damage_per_round[uid] = max(lost, self.damage_per_round.get(uid, 0) * .65)
            self.previous_hp[uid] = hp
        self.previous_round = w.round
        if not w.day:
            enemy_robots = [r for r in w.robots if r.get("targetTeam") not in (None, w.side)]
            hp = sum(r["health"] for r in enemy_robots)
            if (w.round - 1) % 130 == 70:
                self.previous_enemy_hp = None
                self.enemy_wave = []
            self.enemy_wave.append({"round": w.round, "count": len(enemy_robots), "hp": hp,
                "observedHpDrop": max(0, self.previous_enemy_hp - hp) if self.previous_enemy_hp is not None else 0})
            self.previous_enemy_hp = hp
            self.enemy_wave = self.enemy_wave[-60:]
        seen = {u["id"] for u in w.enemies}
        vision = {c for u in w.ours for c in footprint(u)}
        for uid, record in self.enemies.items():
            record["visible"] = uid in seen
            if uid not in seen and any(distance(pos(record["unit"]["pos"]), c) <= 4 for c in vision):
                record["missingAtLastPosition"] = True
        for unit in w.enemies:
            self.enemies[unit["id"]] = {"unit": deepcopy(unit), "lastSeen": w.round,
                "visible": True, "missingAtLastPosition": False}
        for name, price in w.vendor.items():
            history = self.prices.setdefault(name, [])
            if not history or history[-1][1] != price:
                history.append((w.round, price))

    def attack_risk(self, w, unit):
        p = pos(unit["pos"])
        return sum(ROBOT_STATS.get(r["roleType"], (0, 0, 0))[1] for r in w.robots
                   if r.get("abnormalState") != "dizzy" and
                   min(distance(pos(r["pos"]), c) for c in footprint(unit)) <= 4)

    def offense_assessment(self, w):
        recent = [r for r in self.enemies.values() if not r["missingAtLastPosition"]
                  and w.round - r["lastSeen"] <= 130 and r["unit"]["roleType"] in WEAPONS]
        # Deliberately conservative: observations alone cannot prove a guaranteed base kill.
        return {"knownWeapons": len(recent), "freshVisibleWeapons": sum(r["visible"] for r in recent),
                "levels": [r["unit"].get("level", 1) for r in recent],
                "upperBoundDamagePerRound": sum((25 if r["unit"]["roleType"] == "rocket" else 10)
                                                   * r["unit"].get("level", 1) for r in recent),
                "observedEnemyWave": list(self.enemy_wave),
                "attribution": "机器人血量下降可能包含我方攻击，不等同于敌方独立防守火力",
                "recommendSummon": False,
                "reason": "需要敌方清怪速度、剩余防线与召唤收益证据；不凭旧视野保证平推"}
