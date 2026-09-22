"""Run from Competition: python -m local_judge --seeds 1 7 --opponent demo --swap."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import platform
import sys
import time
from urllib.parse import urlsplit

from agent.policy import Agent
from agent.protocol import empty_response, loads
from .engine import Game, SIDES


def demo_policy():
    folder = Path(__file__).resolve().parents[1] / "Demo" / "CoreGeek" / "src" / "agent"
    if not folder.exists():
        raise RuntimeError("Demo/CoreGeek/src/agent is not present")
    spec = importlib.util.spec_from_file_location("official_demo_agent", folder / "__init__.py",
                                                submodule_search_locations=[str(folder)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    from official_demo_agent.brain import decide
    return lambda payload: {"roleCommandMap": decide(payload), "prompt": "", "executeCmd": ""}


def previous_policy(version="v1"):
    archive = Path(__file__).resolve().parents[1] / "reports" / f"baseline-{version}.zip"
    if not archive.exists():
        raise RuntimeError("reports/baseline-v1.zip snapshot missing")
    if str(archive) not in sys.path:
        sys.path.insert(0, str(archive))
    from importlib import import_module
    PreviousAgent = import_module(f"baseline_{version}.policy").Agent
    return PreviousAgent().decide


def http_policy(url):
    endpoint = urlsplit(url)
    if endpoint.scheme != "http" or not endpoint.hostname:
        raise ValueError("local participant URL must use http://")

    def call(payload):
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port or 80, timeout=10)
        try:
            connection.connect()
            connection.sock.settimeout(5)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            connection.request("POST", endpoint.path or "/", body,
                               {"Content-Type": "application/json; charset=utf-8"})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f"HTTP {response.status}")
            return loads(response.read().decode("utf-8"))
        finally:
            connection.close()
    return call


def call_timed(policy, observation):
    begin = time.perf_counter()
    try:
        response = policy(observation)
        elapsed = time.perf_counter() - begin
        if elapsed > 5:
            return None, elapsed, "response exceeded 5 seconds"
        return response, elapsed, None
    except Exception as exc:
        return None, time.perf_counter() - begin, f"{type(exc).__name__}: {exc}"


def source_hashes():
    root = Path(__file__).resolve().parents[1]
    paths = [root / "main.py", root / "run.sh"]
    for directory in ("agent", "local_judge", "tests"):
        paths.extend(sorted((root / directory).glob("*.py")))
    paths.extend(sorted((root / "docs").glob("*.jpg")))
    paths.extend(root / "docs" / name for name in ("任务书.md", "接口文档.md"))
    return {str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths if p.exists()}


def aggregate_matches(games):
    """R08; incompletely specified draw combinations use documented A08."""
    matches = []
    for seed in sorted({g["seed"] for g in games}):
        halves = [g for g in games if g["seed"] == seed]
        if len(halves) != 2 or not all(g["complete"] for g in halves):
            continue
        wins = losses = 0
        scores = [0.0, 0.0]
        for g in halves:
            side = g["participantSide"]
            enemy = SIDES[1 - SIDES.index(side)]
            scores[0] += g["scores"][side]
            scores[1] += g["scores"][enemy]
            wins += g["localWinner"] == side
            losses += g["localWinner"] == enemy
        outcome = ("win" if wins > losses else "loss" if losses > wins else
                   "win" if scores[0] > scores[1] else "loss" if scores[1] > scores[0] else "draw")
        matches.append({"seed": seed, "localOutcome": outcome, "halfWins": wins, "halfLosses": losses,
                        "twoHalfScores": scores, "localMatchPoints": {"win": 3, "draw": 1, "loss": 0}[outcome]})
    return matches


def run(seed, opponent="demo", swapped=False, pressure=1, limit=1300, replay=None, url=None,
        profile="observed", unknown_waves="hold8"):
    game = Game(seed, pressure, profile=profile, unknown_waves=unknown_waves)
    participant = http_policy(url) if url else Agent().decide
    other = (demo_policy() if opponent == "demo" else previous_policy(opponent) if opponent in ("v2", "v3") else previous_policy() if opponent == "previous"
             else Agent().decide if opponent == "self" else lambda _: empty_response())
    policies = [other, participant] if swapped else [participant, other]
    participant_side = "defender" if swapped else "challenger"
    latencies = {s: [] for s in SIDES}
    transport_errors = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        while not game.finished and game.round <= limit:
            observations = {s: game.observation(s) for s in SIDES}
            jobs = {s: pool.submit(call_timed, policies[i], observations[s]) for i, s in enumerate(SIDES)
                    if game.exceptions[s] < 5 and not game.stopped[s]}
            responses = {}
            for side, future in jobs.items():
                response, elapsed, error = future.result()
                latencies[side].append(elapsed * 1000)
                responses[side] = response
                if error:
                    transport_errors.append({"round": game.round, "side": side, "error": error})
                    # In-process uncaught exception represents a crashed participant.
                    if not url and not error.startswith("response exceeded"):
                        game.stopped[side] = True
                        game.exceptions[side] += 1
            frame = {"round": game.round, "seed": seed, "swapped": swapped,
                     "observations": observations, "responses": responses,
                     "traces": {s: getattr(getattr(policies[i], "__self__", None), "trace", {})
                                for i, s in enumerate(SIDES)}}
            game.step(responses)
            if replay:
                frame["events"] = game.last_events
                frame["after"] = game.result()
                replay.write(json.dumps(frame, ensure_ascii=False) + "\n")
    result = game.result()
    result.update({"participantSide": participant_side, "opponent": opponent,
                   "swapped": swapped, "transportErrors": transport_errors,
                   "latencyMs": {s: {"max": max(v, default=0),
                       "p95": sorted(v)[min(len(v) - 1, int(len(v) * .95))] if v else 0}
                                 for s, v in latencies.items()}})
    return result


def main():
    parser = argparse.ArgumentParser(description="Experimental local judge; see docs/IMPLEMENTATION.md")
    parser.add_argument("--seeds", type=int, nargs="+", default=[1])
    parser.add_argument("--opponent", choices=("demo", "self", "idle", "previous", "v2", "v3"), default="v3")
    parser.add_argument("--profile", choices=("observed", "legacy"), default="observed")
    parser.add_argument("--unknown-waves", choices=("hold8", "growth"), default="hold8",
                        help="day 9/10 are unknown; choose an explicitly hypothetical scenario")
    parser.add_argument("--swap", action="store_true", help="also run participant on the opposite side")
    parser.add_argument("--pressure", type=int, default=1, help="local wave multiplier, not an official setting")
    parser.add_argument("--rounds", type=int, default=1300)
    parser.add_argument("--report", type=Path, default=Path("reports/latest.json"))
    parser.add_argument("--replay", type=Path, help="optional JSONL replay file")
    parser.add_argument("--url", help="call an already-running participant HTTP server")
    args = parser.parse_args()
    if not 1 <= args.rounds <= 1300:
        parser.error("rounds must be 1..1300")
    replay = None
    if args.replay:
        args.replay.parent.mkdir(parents=True, exist_ok=True)
        replay = args.replay.open("w", encoding="utf-8")
    report = {"scope": "local experimental evidence, not official validation", "python": platform.python_version(),
              "ruleBaseline": "task book v1.0 2026-09-09 + user decisions 2026-09-21",
              "assumptions": "docs/IMPLEMENTATION.md A01-A09; docs/EXPERIENCE_V2.md B01-B04; docs/LOG_TASK_V4.md (observed robot AI updated)",
              "hashes": source_hashes(), "games": []}
    if args.opponent in ("previous", "v2", "v3"):
        version = "v1" if args.opponent == "previous" else args.opponent
        report["baselineArchiveSHA256"] = hashlib.sha256(
            (Path(__file__).resolve().parents[1] / f"reports/baseline-{version}.zip").read_bytes()).hexdigest()
    try:
        for seed in args.seeds:
            for swapped in ((False, True) if args.swap else (False,)):
                result = run(seed, args.opponent, swapped, args.pressure, args.rounds, replay, args.url,
                             args.profile, args.unknown_waves)
                report["games"].append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        if replay:
            replay.close()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report["matches"] = aggregate_matches(report["games"])
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
