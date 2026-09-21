"""R01: strict response shape, separate from execution eligibility."""
import json
from .rules import TARGET_ITEMS

ACTIONS = {"move", "attack", "sell", "buy", "build", "remove", "acceptTask",
           "submitAnswer", "summonTreasure", "use", "drop", "collect"}
POSITION_ACTIONS = {"move", "attack", "build", "remove", "collect", "summonTreasure"}
NAME_ACTIONS = {"sell", "buy", "build", "use", "drop"}


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate JSON key: {key}")
        obj[key] = value
    return obj


def loads(raw: str | bytes):
    return json.loads(raw, object_pairs_hook=_unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def empty_response() -> dict:
    return {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}


def schema_errors(response) -> list[str]:
    errors = []
    if not isinstance(response, dict) or not isinstance(response.get("roleCommandMap"), dict):
        return ["roleCommandMap must be an object"]
    for field in ("prompt", "executeCmd"):
        if not isinstance(response.get(field, ""), str):
            errors.append(f"{field} must be a string")
    for key, cmd in response["roleCommandMap"].items():
        if not isinstance(key, str) or not key.isdecimal():
            errors.append("command key must encode a unit ID")
        if (not isinstance(cmd, dict) or not isinstance(cmd.get("action"), str)
                or cmd["action"] not in ACTIONS):
            errors.append(f"{key}: unknown action")
            continue
        action = cmd["action"]
        if action in NAME_ACTIONS and (not isinstance(cmd.get("name"), str) or not cmd["name"]):
            errors.append(f"{key}: name required")
        required = action in POSITION_ACTIONS or (action == "use" and isinstance(cmd.get("name"), str)
                                                 and cmd["name"] in TARGET_ITEMS)
        targets = cmd.get("targetPos")
        if required or targets is not None:
            if not isinstance(targets, list) or not targets or any(
                not isinstance(p, dict) or type(p.get("x")) is not int or type(p.get("y")) is not int
                for p in targets
            ):
                errors.append(f"{key}: targetPos must be a nonempty coordinate array")
            elif action != "attack" and len(targets) != 1:
                errors.append(f"{key}: exactly one target required")
        if action == "attack" and (not isinstance(cmd.get("controllerId"), str)
                                   or not cmd["controllerId"].isdecimal()):
            errors.append(f"{key}: controllerId must be an ID string")
        if action in ("buy", "sell") and (type(cmd.get("num", 1)) is not int or cmd.get("num", 1) < 1):
            errors.append(f"{key}: num must be positive integer")
        if action == "submitAnswer" and not isinstance(cmd.get("taskAnswer"), str):
            errors.append(f"{key}: taskAnswer required")
        if action == "summonTreasure" and (not isinstance(cmd.get("item"), list)
                                           or any(not isinstance(v, str) for v in cmd["item"])):
            errors.append(f"{key}: item must be an array of names")
    return errors
