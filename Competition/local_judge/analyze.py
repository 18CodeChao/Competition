"""python -m local_judge.analyze logs/session.jsonl --round 71 --side challenger"""
import argparse
from collections import Counter
import json
from pathlib import Path
from agent.telemetry import ascii_map, read_records


def analyze(path, round_no=None, side=None):
    summary = {"turns": 0, "actions": Counter(), "failedActions": 0, "errors": Counter(),
               "effectiveDamagePlanned": 0, "overkillPlanned": 0, "maxLatencyMs": 0,
               "malformedLines": 0, "repeatedTurns": 0}
    seen = set()
    last_round = {}
    maps = []
    with Path(path).open(encoding="utf-8") as stream:
        for record in read_records(stream, summary):
            if record.get("type") == "metadata":
                seen.clear()
                last_round.clear()
                continue
            if "request" in record:
                rows = [(record["request"], record.get("response") or {}, record.get("trace") or {})]
            else:
                rows = [(request, (record.get("responses") or {}).get(s) or {},
                         record.get("traces", {}).get(s) or {}) for s, request in record.get("observations", {}).items()]
            for request, response, trace in rows:
                if side and request["teamOur"]["type"] != side:
                    continue
                identity = (record.get("seed"), record.get("swapped"), request["teamOur"].get("teamId"),
                            request["teamOur"]["type"], request["roundNo"])
                # A lower round means a new half in a persistent HTTP process.
                prefix = identity[:-1]
                if request["roundNo"] < last_round.get(prefix, 0):
                    seen = {key for key in seen if key[:-1] != prefix}
                last_round[prefix] = request["roundNo"]
                if identity in seen:
                    summary["repeatedTurns"] += 1
                    continue
                seen.add(identity)
                summary["turns"] += 1
                summary["actions"].update(c["action"] for c in response.get("roleCommandMap", {}).values())
                summary["failedActions"] += sum(v is False for v in request.get("lastRoundRoleActionResults", {}).values())
                summary["errors"].update(str(e["errorCode"]) for e in request.get("errors", []))
                summary["maxLatencyMs"] = max(summary["maxLatencyMs"], record.get("elapsedMs", trace.get("elapsedMs", 0)))
                for event in trace.get("events", []):
                    summary["effectiveDamagePlanned"] += event.get("effectiveDamage", 0)
                    summary["overkillPlanned"] += event.get("overkill", 0)
                if request["roundNo"] == round_no:
                    maps.append(f"round={round_no} side={request['teamOur']['type']}\n" + ascii_map(request))
    summary["actions"], summary["errors"] = dict(summary["actions"]), dict(summary["errors"])
    return summary, maps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--round", type=int)
    parser.add_argument("--side", choices=("challenger", "defender"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summary, maps = analyze(args.log, args.round, args.side)
    text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n" + "\n\n".join(maps)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
