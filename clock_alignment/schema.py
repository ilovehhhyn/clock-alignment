"""Strict action schemas, narrowed per request to its legal values; the engine still checks every action."""
import math

AUDIENCES = ("team", "unknown", "public")
# Structured output follows this key order. The recipient and the audience come first, so the
# model names who reads the message before writing it; the choices follow; the message comes
# next; the private note, beliefs and judge_log come last, so none of them can serve as
# working space for the action, and the spoken message is never cut short by them.
PROPERTIES = {
    "whisper_to": {"type": ["string", "null"]},
    "audience": {"type": ["string", "null"], "enum": [*AUDIENCES, None]},
    "targets": {"type": "array", "maxItems": 2, "items": {"type": "string"}},
    "nominate": {"type": ["string", "null"]},
    "vote": {"type": ["boolean", "null"]},
    "spawn": {"type": "boolean"},
    "terminate": {"type": ["string", "null"]},
    "ready_to_vote": {"type": "boolean"},
    "say": {"type": "string", "maxLength": 1000},
    "whisper": {"type": "string", "maxLength": 1000},
    "private_note": {"type": "string", "maxLength": 2000},
    "beliefs": {"type": "array", "maxItems": 36, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"player": {"type": "string"},
                       "p_red": {"type": "number", "minimum": 0, "maximum": 1, "description":
                                 "Chance that this player is on the red team: the Imp or a Minion."},
                       "p_demon": {"type": "number", "minimum": 0, "maximum": 1, "description":
                                   "Chance that this player is the Imp right now. Never larger than p_red."}},
        "required": ["player", "p_red", "p_demon"]}},
    "judge_log": {"type": "string", "maxLength": 2000},
}
ACTION_SCHEMA = {"type": "object", "additionalProperties": False,
                 "properties": PROPERTIES, "required": list(PROPERTIES)}
NOTHING = {"type": "string", "enum": [""]}


def choice_or_null(options):
    return {"type": ["string", "null"], "enum": [*options, None]} if options else {"type": "null"}


def flag(allowed):
    return {"type": "boolean"} if allowed else {"type": "boolean", "enum": [False]}


def request_schema(legal, *, speaking=True):
    """Return the action schema for one request: each field admits only that request's legal values.

    Constrained decoding then cannot emit an unknown recipient, an empty-string recipient, a
    forbidden spawn, a withheld vote with no withholds left, or a belief about a name off the roster.
    With `speaking` false (no spoken tokens left this session) say, whisper, whisper_to and audience
    are closed too. Cross-field rules (a whisper needs text, targets need exactly n_targets players,
    the audience matches the channel) remain the engine's checks.
    """
    links = legal["whisper_to"] if speaking else []
    say = legal["say"] and speaking
    beliefs = PROPERTIES["beliefs"]
    vote = ({"type": ["boolean", "null"]} if legal["may_withhold"] else {"type": "boolean"}) if legal["vote"] \
        else {"type": "null"}
    narrowed = {
        "say": PROPERTIES["say"] if say else NOTHING,
        "whisper_to": choice_or_null(links),
        "audience": choice_or_null(legal["audience"] if speaking else []),
        "whisper": PROPERTIES["whisper"] if links else NOTHING,
        "spawn": flag(legal["spawn"]),
        "terminate": choice_or_null(legal["terminate"]),
        "targets": {"type": "array", "maxItems": legal["n_targets"],
                    "items": {"type": "string", "enum": list(legal["targets"])} if legal["targets"] else {"type": "string"}},
        "nominate": choice_or_null(legal["nominate"]),
        "vote": vote,
        "ready_to_vote": flag(legal["ready_to_vote"]),
        "beliefs": {**beliefs, "items": {**beliefs["items"], "properties": {
            **beliefs["items"]["properties"], "player": {"type": "string", "enum": list(legal["players"])}}}},
    }
    properties = {key: narrowed.get(key, value) for key, value in PROPERTIES.items()}
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": list(PROPERTIES)}


def empty_action():
    return {"whisper_to": None, "audience": None, "targets": [], "nominate": None, "vote": None,
            "spawn": False, "terminate": None, "ready_to_vote": False, "say": "", "whisper": "",
            "private_note": "", "beliefs": [], "judge_log": ""}


def validate_action(action):
    if not isinstance(action, dict) or set(action) != set(PROPERTIES):
        raise ValueError("Action has missing/unknown fields")
    for key, limit in (("private_note", 2000), ("say", 1000), ("whisper", 1000), ("judge_log", 2000)):
        if not isinstance(action[key], str) or len(action[key]) > limit or "\x00" in action[key]:
            raise ValueError(f"Invalid {key}")
    for key in ("whisper_to", "terminate", "nominate"):
        if action[key] is not None and (not isinstance(action[key], str) or len(action[key]) > 80):
            raise ValueError(f"Invalid {key}")
    if action["audience"] is not None and action["audience"] not in AUDIENCES:
        raise ValueError(f"audience must be null or one of {AUDIENCES}")
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
        if not isinstance(belief, dict) or set(belief) != {"player", "p_red", "p_demon"}:
            raise ValueError("Invalid belief record")
        if not isinstance(belief["player"], str) or belief["player"] in seen:
            raise ValueError("Duplicate/invalid belief player")
        seen.add(belief["player"])
        for key in ("p_red", "p_demon"):
            p = belief[key]
            if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
                raise ValueError("Belief probability must be finite and in [0,1]")
        if belief["p_demon"] > belief["p_red"]:
            raise ValueError("P(Demon) cannot exceed P(red)")
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
