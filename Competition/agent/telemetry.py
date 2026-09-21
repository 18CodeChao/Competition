"""Replayable official observations and strategy explanations; no hidden-state map."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import uuid
import sys

from .rules import WIDTH, HEIGHT, footprint, pos, inside


def ascii_map(payload):
    grid = [["." for _ in range(WIDTH)] for _ in range(HEIGHT)]
    def paint(cells, symbol):
        for x, y in cells:
            if inside((x, y)):
                grid[y][x] = symbol
    for z in payload["mapInfo"].get("zones", []):
        kind = z["neutralType"]
        symbol = {"stone": "s", "iron": "i", "copper": "c", "vendor": "V", "weaponShop": "Q"}.get(kind, "T")
        paint([pos(z["pos"])], symbol)
    for field, enemy in (("teamEnemy", True), ("teamOur", False)):
        for unit in payload.get(field, {}).get("roles", []):
            if unit["health"] <= 0:
                continue
            kind = unit["roleType"]
            symbols = ({"station": "E", "wall": "~", "worker": "w", "pioneer": "p",
                        "rocket": "R", "railgun": "L", "gatling": "G"} if enemy else
                       {"station": "S", "wall": "#", "worker": "W", "pioneer": "P",
                        "rocket": "r", "railgun": "l", "gatling": "g"})
            paint(footprint(unit), symbols.get(kind, "?"))
    for robot in payload.get("robot", {}).get("roles", []):
        paint([pos(robot["pos"])], {"smallRobot": "1", "middleRobot": "2", "largeRobot": "3", "bossRobot": "B"}.get(robot["roleType"], "?"))
    rows = [f"{y:02d} {''.join(grid[y])}" for y in range(HEIGHT - 1, -1, -1)]
    rows += ["   " + "".join(str(x // 10) if x % 10 == 0 else " " for x in range(WIDTH)),
             "   " + "".join(str(x % 10) for x in range(WIDTH)),
             "S/E=our/enemy base #/~=wall W/P=worker/pioneer r/l/g=weapons",
             "1/2/3/B=robots s/i/c=minerals V=vendor Q=shop T=task; enemy fog is preserved"]
    return "\n".join(rows)


class MatchJournal:
    def __init__(self, directory, map_every=1, max_bytes=64 * 1024 * 1024, backups=8):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self.map_every = max(1, map_every)
        self.last = {}
        self.logger = logging.getLogger("match-json-" + name)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.handler = RotatingFileHandler(directory / (name + ".jsonl"), maxBytes=max_bytes,
                                           backupCount=backups, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(self.handler)
        self.maps = RotatingFileHandler(directory / (name + "-maps.log"), maxBytes=max_bytes,
                                        backupCount=backups, encoding="utf-8")
        self.map_logger = logging.getLogger("match-map-" + name)
        self.map_logger.setLevel(logging.INFO)
        self.map_logger.propagate = False
        self.map_logger.addHandler(self.maps)
        root = Path(__file__).resolve().parent
        self.logger.info(json.dumps({"type": "metadata", "schemaVersion": 2, "session": name,
            "sourceHashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob("*.py"))},
            "maxBytesPerFile": max_bytes, "backups": backups}, ensure_ascii=False))

    def record(self, payload, response, trace, elapsed_ms):
        team = payload["teamOur"]
        round_no = payload["roundNo"]
        previous = self.last.get((team["teamId"], team["type"]))
        prior_team = previous["teamOur"] if previous and previous["roundNo"] < round_no else None
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        frame = {"type": "turn", "schemaVersion": 2, "round": round_no,
                 "team": team["teamId"], "side": team["type"], "elapsedMs": elapsed_ms,
                 "requestHash": fingerprint, "request": payload, "response": response, "trace": trace,
                 "summary": {"gold": team.get("goldNum"), "score": team.get("totalScore"),
                     "goldDelta": team.get("goldNum", 0) - prior_team.get("goldNum", 0) if prior_team else None,
                     "scoreDelta": team.get("totalScore", 0) - prior_team.get("totalScore", 0) if prior_team else None,
                     "robots": dict(Counter(r["roleType"] for r in payload.get("robot", {}).get("roles", []))),
                     "actionResults": payload.get("lastRoundRoleActionResults", {}), "errors": payload.get("errors", [])}}
        if (round_no - 1) % self.map_every == 0:
            frame["map"] = ascii_map(payload)
            if self.map_logger is not None:
                self.map_logger.info("round=%s team=%s side=%s\n%s", round_no, team["teamId"], team["type"], frame["map"])
        self.logger.info(json.dumps(frame, ensure_ascii=False, allow_nan=False))
        self.last[(team["teamId"], team["type"])] = payload

    def close(self):
        self.handler.close()
        self.maps.close()
        self.logger.removeHandler(self.handler)
        self.map_logger.removeHandler(self.maps)


class StreamJournal(MatchJournal):
    """Platform-collected stdout. No directories, local files or rotating handlers."""
    def __init__(self, stream=None, map_every=1, chunk_chars=1800):
        self.stream = stream if stream is not None else sys.stdout
        self.map_every, self.chunk_chars = max(1, map_every), max(256, chunk_chars)
        self.last = {}
        self.logger, self.map_logger = self, None
        self.session, self.sequence = uuid.uuid4().hex, 0
        root = Path(__file__).resolve().parent
        self.info(json.dumps({"type": "metadata", "schemaVersion": 3, "session": self.session,
            "transport": "stdout chunks", "sourceHashes": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob("*.py"))}}))

    def info(self, message, *args):
        text = message % args if args else message
        self.sequence += 1
        chunks = [text[i:i + self.chunk_chars] for i in range(0, len(text), self.chunk_chars)]
        for part, data in enumerate(chunks):
            envelope = {"session": self.session, "event": self.sequence, "part": part,
                        "parts": len(chunks), "data": data}
            self.stream.write("COMPETITION_LOG " + json.dumps(envelope, ensure_ascii=False) + "\n")
        self.stream.flush()

    def close(self):
        self.stream.flush()


def read_records(stream, stats):
    """Recover JSONL or platform log chunks, tolerating timestamp prefixes and interleaving."""
    pending = {}
    for line in stream:
        marker = line.find("COMPETITION_LOG ")
        try:
            if marker >= 0:
                packet = json.loads(line[marker + len("COMPETITION_LOG "):])
                key = packet["session"], packet["event"]
                parts = pending.setdefault(key, {})
                parts[packet["part"]] = packet["data"]
                if len(parts) < packet["parts"]:
                    continue
                text = "".join(parts[i] for i in range(packet["parts"]))
                del pending[key]
                yield json.loads(text)
            elif line.lstrip().startswith("{"):
                yield json.loads(line)
            else:
                stats["ignoredLines"] = stats.get("ignoredLines", 0) + 1
        except (ValueError, KeyError, TypeError):
            stats["malformedLines"] = stats.get("malformedLines", 0) + 1
    stats["incompleteEvents"] = len(pending)
