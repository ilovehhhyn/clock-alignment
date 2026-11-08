"""One strict API schema; phase and ACL checks remain the engine's responsibility."""
import math

PROPERTIES = {
    "private_note": {"type": "string", "maxLength": 2000},
    "beliefs": {"type": "array", "maxItems": 36, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"player": {"type": "string"},
                       "p_evil": {"type": "number", "minimum": 0, "maximum": 1},
                       "p_demon": {"type": "number", "minimum": 0, "maximum": 1}},
        "required": ["player", "p_evil", "p_demon"]}},
    "say": {"type": "string", "maxLength": 1000},
    "whisper_to": {"type": ["string", "null"]},
    "whisper": {"type": "string", "maxLength": 1000},
    "spawn": {"type": "boolean"},
    "terminate": {"type": ["string", "null"]},
    "targets": {"type": "array", "maxItems": 2, "items": {"type": "string"}},
    "nominate": {"type": ["string", "null"]},
    "vote": {"type": ["boolean", "null"]},
    "ready_to_vote": {"type": "boolean"},
}
ACTION_SCHEMA = {"type": "object", "additionalProperties": False,
                 "properties": PROPERTIES, "required": list(PROPERTIES)}


def empty_action():
    return {"private_note": "", "beliefs": [], "say": "", "whisper_to": None,
            "whisper": "", "spawn": False, "terminate": None, "targets": [],
            "nominate": None, "vote": None, "ready_to_vote": False}


def validate_action(action):
    if not isinstance(action, dict) or set(action) != set(PROPERTIES):
        raise ValueError("Action has missing/unknown fields")
    for key, limit in (("private_note", 2000), ("say", 1000), ("whisper", 1000)):
        if not isinstance(action[key], str) or len(action[key]) > limit or "\x00" in action[key]:
            raise ValueError(f"Invalid {key}")
    for key in ("whisper_to", "terminate", "nominate"):
        if action[key] is not None and (not isinstance(action[key], str) or len(action[key]) > 80):
            raise ValueError(f"Invalid {key}")
    for key in ("spawn", "ready_to_vote"):
        if type(action[key]) is not bool:
            raise ValueError(f"Invalid {key}")
    if action["vote"] is not None and type(action["vote"]) is not bool:
        raise ValueError("Invalid vote")
    if (not isinstance(action["targets"], list) or len(action["targets"]) > 2
            or any(not isinstance(t, str) for t in action["targets"])
            or len(set(action["targets"])) != len(action["targets"])):
        raise ValueError("Invalid targets")
    beliefs = action["beliefs"]
    if not isinstance(beliefs, list) or len(beliefs) > 36:
        raise ValueError("Invalid beliefs")
    seen = set()
    for belief in beliefs:
        if not isinstance(belief, dict) or set(belief) != {"player", "p_evil", "p_demon"}:
            raise ValueError("Invalid belief record")
        if not isinstance(belief["player"], str) or belief["player"] in seen:
            raise ValueError("Duplicate/invalid belief player")
        seen.add(belief["player"])
        for key in ("p_evil", "p_demon"):
            p = belief[key]
            if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
                raise ValueError("Belief probability must be finite and in [0,1]")
        if belief["p_demon"] > belief["p_evil"]:
            raise ValueError("P(Demon) cannot exceed P(evil)")
    return action


def validate_json(value, schema):
    """Validate the small JSON-schema subset used for observer annotations."""
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict) or set(value) != set(schema["required"]):
            raise ValueError("Observer object has missing/unknown fields")
        for key, child in schema["properties"].items():
            validate_json(value[key], child)
    elif kind == "array":
        if not isinstance(value, list):
            raise ValueError("Observer field must be an array")
        for item in value:
            validate_json(item, schema["items"])
    elif kind == "string":
        if not isinstance(value, str) or ("enum" in schema and value not in schema["enum"]):
            raise ValueError("Invalid observer string/label")
    elif kind == "number":
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                or value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf)):
            raise ValueError("Invalid observer probability")
    else:
        raise ValueError("Unsupported observer schema type")
    return value
