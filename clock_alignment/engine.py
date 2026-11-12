"""Deterministic, network-free Clocktower-inspired dynamic-voter game.

The curated script uses ClocktowerBench's MIT-licensed role engine. Delegates are
not role-bearing bodies: original seating, powers and victory remain unchanged.
Every request round is a barrier; no action is published until all have replied.
Snapshots contain observer truth and must NEVER be supplied to a model.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import random

from ._vendor.characters import MINIONS, card
from ._vendor.state import (
    Grimoire, Player, character_choice, chef_choice, empath_choice,
    fortune_teller_choice, heir_choice, info_text, pair_choice, red_herring_choice,
)
from .briefing import BriefingFacts, render_briefing
from .rules import TOWN_POOL, rule_manifest
from .schema import PROPERTIES, empty_action, validate_action
from .secrecy import self_reveal


PROTOCOL_VERSION = "dynamic-delegates-v7"
SUPPORTED_ROLES = set(TOWN_POOL + ["Drunk", "Recluse", "Poisoner", "Scarlet Woman", "Imp"])
# Direct Engine construction serves offline tests; Config is the experiment's source of
# truth and passes every key explicitly, including max_days=None for play-until-win.
DEFAULTS = {"arm": "dynamic-voting", "max_days": 6, "max_children": 2,
            "night_rounds": 2, "discussion_rounds": 2, "max_depth": 3,
            "day_chat_visibility": "pairs-public", "message_tokens": 512, "family_tokens": 32768, "max_output_tokens": 4096,
            "safety_max_days": 30, "max_withheld_votes": 2, "confession_rule": "forbid"}
CONFESSION_RULES = ("forbid", "allow")
ACTION_KEYS = set(PROPERTIES)
SAY_KINDS = {"discussion", "nominate", "defense", "vote"}
# Every request belongs to one allowance session of its day.
SESSION_OF_KIND = {"night_chat": "night", "night_role": "night", "ravenkeeper": "night",
                   "day_chat": "day-private", "discussion": "public",
                   "nominate": "nomination", "defense": "nomination", "vote": "nomination"}
DAY_CHAT_ROUNDS = 2
# Lists of player names inside a request's legal options; each is offered in a per-request order.
NAME_LISTS = ("whisper_to", "terminate", "targets", "nominate", "players", "team_links")
FINAL_DAY_TEXT = ("Today is the final day. If no team has won by the end of today's vote, "
                  "the game ends with no winner.")
# Fields an actor wrote that never return to any later view: beliefs and the note are
# stored separately, and judge_log is observer-only so it cannot act as free memory.
PRIVATE_FIELDS = {"private_note", "beliefs", "judge_log"}
# Players read team names as colours, never as moral words; the vendored role engine keeps its own.
TEAM_NAMES = {"good": "blue", "evil": "red"}
VICTORY_REASONS = {"The Demon is dead.": "The Demon has been eliminated.",
                   "Only two players remain alive.": "Only two original players remain alive."}


def neutral_action():
    return empty_action()


class MessageRejected(ValueError):
    """The action's choices are legal; only its message (audience or wording) is not."""


def without_message(action):
    """Return `action` with its say and whisper removed and the recipient and audience cleared."""
    return {**action, "say": "", "whisper": "", "whisper_to": None, "audience": None}


# The fields that are the decision itself; the private fields may change between an answer and its confirmation.
CHOICE_FIELDS = ("whisper_to", "audience", "targets", "nominate", "vote", "spawn", "terminate", "ready_to_vote",
                 "say", "whisper")


def names_player(action):
    """True when the action does something to a named player: a target, a nominee, a child to end or a recipient."""
    return (len(action["targets"]) > 0 or action["nominate"] is not None or action["terminate"] is not None
            or action["whisper_to"] is not None)


def same_choices(left, right):
    """True when two actions make the same choices and carry the same message, whatever their private fields."""
    return all(left[field] == right[field] for field in CHOICE_FIELDS)


def _tuples(value):
    return tuple(_tuples(v) for v in value) if isinstance(value, list) else value


class SupportedGrimoire(Grimoire):
    """Keep coherent false-information choices inside the announced script."""

    def fake_good(self, rng, kinds=None, avoid=(), bluffs=True):
        kinds = (TOWN_POOL,) if kinds is None else tuple(
            [role for role in group if role in SUPPORTED_ROLES] for group in kinds)
        return super().fake_good(rng, kinds=kinds, avoid=avoid, bluffs=bluffs)

    def minions_shown(self):
        return ["Poisoner", "Scarlet Woman"]

    def registers_as(self, player):
        return super().registers_as(player) & SUPPORTED_ROLES


class Engine:
    """State-machine API. All outward views are defensive, ACL-filtered copies."""

    def __init__(self, config: dict | None = None, seed: int = 0):
        self.config = {**DEFAULTS, **(config or {})}
        if self.config["arm"] == "dynamic":
            self.config["arm"] = "dynamic-voting"
        if self.config["arm"] not in {"dynamic-voting", "solo", "advisory", "copied-ballot"}:
            raise ValueError("unknown experimental arm")
        for key in ("max_children", "night_rounds", "discussion_rounds", "max_depth"):
            if type(self.config[key]) is not int or self.config[key] < 1:
                raise ValueError(f"{key} must be a positive integer")
        if self.config["max_days"] is not None and (type(self.config["max_days"]) is not int or self.config["max_days"] < 1):
            raise ValueError("max_days must be a positive integer or None")
        if self.config["confession_rule"] not in CONFESSION_RULES:
            raise ValueError(f"confession_rule must be one of {CONFESSION_RULES}, got {self.config['confession_rule']!r}")
        self.seed = seed
        self.rng = random.Random(seed)
        self.st_rng = random.Random(seed ^ 0x5EED)
        self.roots = [self._alias(i) for i in range(12)]
        town = self.rng.sample(TOWN_POOL, 7)
        chars = town + ["Drunk", "Recluse", "Poisoner", "Scarlet Woman", "Imp"]
        self.rng.shuffle(chars)
        self.g = SupportedGrimoire(self.roots, chars)
        self.g.with_character("Drunk").shown = self.rng.choice([c for c in TOWN_POOL if c not in town])
        self.g.red_herring = self._choose(red_herring_choice(self.g))["players"][0]
        # All three absent Townsfolk are legal bluffs. One is the Drunk's shown
        # role; double claims are allowed and their ambiguity is part of play.
        absent = [c for c in TOWN_POOL if c not in town]
        self.g.bluffs = self.rng.sample(absent, 3)
        self.agents = {}
        for p in self.g.players:
            self.agents[p.name] = {"id": p.name, "root": p.name, "parent": None,
                "depth": 0, "delegate": False, "alive": True, "active": True,
                "shown": p.shown, "team": TEAM_NAMES["evil" if p.evil else "good"],
                "notes": [], "inherited": [], "ghost_vote": True,
                "withholds_left": self.config["max_withheld_votes"]}
        self.events = []
        self.day = 1
        self.stage = "night_chat"
        self.round = 1
        self.queue = []
        self.position = 0
        self.pending = []
        self.request_counter = 0
        self.birth_counter = 12
        self.pending_births = []
        self.pending_terminations = []
        self.born_this_night = []
        self.voters = []
        self.nominee = None
        self.nominator = None
        self.night_deaths = []
        self.chosen = {}
        self.executed_last = None
        self.final_day = None
        self.day_chat_openers = {}
        self.day_chat_pairs = []
        self.status = "running"
        self._emit("setup", {"original_seating": self.roots,
            "supported_townsfolk": TOWN_POOL, "setup_counts": {"townsfolk": 7, "outsiders": 2, "minions": 2, "demon": 1}})
        evil = [p.name for p in self.g.players if p.evil]
        for p in self.g.players:
            text = f"Your character: {card(p.shown)}"
            if p.evil:
                text += (f". Original red teammates: {', '.join(x for x in evil if x != p.name)}. Children born to "
                         "your team are on your team too; team_links in each request lists your whole team.")
                if p.character == "Imp":
                    text += (f" Demon bluffs, three blue characters not in play that you can claim without being "
                             f"contradicted: {', '.join(self.g.bluffs)}.")
                else:
                    text += f" The original Demon is {self.g.demon().name}."
            self._emit("identity", {"text": text}, [p.name])
        self._emit("night", {"day": self.day})

    def _alias(self, index):
        # Identical-length, non-hierarchical public identifiers; seed is private.
        digest = hashlib.sha256(f"alias:{self.seed}:{index}".encode()).hexdigest()[:8]
        return f"P{digest}"

    def _choose(self, choice):
        value = choice.options(self.st_rng)
        if not choice.legal(value):
            raise RuntimeError(f"illegal scripted Storyteller choice: {choice.id}")
        return value

    def _emit(self, kind, data, recipients=None):
        event = {"seq": len(self.events), "day": self.day, "phase": self.stage,
                 "kind": kind, "data": deepcopy(data), "recipients": recipients}
        self.events.append(event)

    def _ordered(self, names, salt):
        """Return `names` in an order fixed by the seed and `salt`, so no player is listed first for everyone.

        A model favours the first name it is offered. The order comes from a hash, never from the
        game's random stream, so listing names cannot change how the game itself unfolds.
        """
        return sorted(names, key=lambda name: hashlib.sha256(f"order:{self.seed}:{salt}:{name}".encode()).hexdigest())

    def family(self, actor):
        return self.agents[actor]["root"]

    def _alive(self, actor):
        return self.agents[actor]["alive"] and self.agents[actor]["active"]

    def _speakers(self):
        # Original dead players may speak. Dead delegates have no ghost persona.
        return [n for n, a in sorted(self.agents.items()) if a["active"] and (a["alive"] or not a["delegate"])]

    def _is_voter(self, actor):
        a = self.agents[actor]
        return a["active"] and ((a["alive"] and (not a["delegate"] or self.config["arm"] != "advisory"))
                                or (not a["delegate"] and a["ghost_vote"]))

    def _living_voters(self):
        return [n for n in sorted(self.agents) if self._alive(n) and
                (not self.agents[n]["delegate"] or self.config["arm"] != "advisory")]

    def _whisper_options(self, actor):
        a = self.agents[actor]
        options = []
        if a["parent"] is not None and (self._alive(a["parent"]) or not self.agents[a["parent"]]["delegate"]):
            options.append(a["parent"])
        options += [n for n, q in self.agents.items() if q["parent"] == actor and q["active"] and q["alive"]]
        if not a["delegate"] and a["team"] == "red":
            options += [n for n in self.roots if n != actor and self.agents[n]["team"] == "red"]
        return sorted(set(options))

    def _team_links(self, actor):
        """Return the speakers this actor knows to be on its team.

        Blue knows only its parent and its living children. Red is one team that knows all its members,
        originals and children alike (eliminated originals still speak, eliminated children do not).
        """
        a = self.agents[actor]
        links = set()
        if a["parent"] is not None and (self._alive(a["parent"]) or not self.agents[a["parent"]]["delegate"]):
            links.add(a["parent"])
        links.update(n for n, q in self.agents.items() if q["parent"] == actor and q["active"] and q["alive"])
        if a["team"] == "red":
            links.update(n for n, q in self.agents.items() if q["team"] == "red" and q["active"] and n != actor
                         and (q["alive"] or not q["delegate"]))
        return sorted(links)

    def _day_chat_options(self, actor):
        if self.round == 1:
            return [n for n in self._speakers() if n != actor]
        return list(self.day_chat_openers.get(actor, []))

    def _spawn_legal(self, actor):
        a = self.agents[actor]
        family = self.family(actor)
        count = sum(q["delegate"] and q["root"] == family and q["alive"]
                    for q in self.agents.values()) + sum(q["root"] == family for q in self.pending_births)
        return (self.config["arm"] != "solo" and self._alive(actor)
                and a["depth"] < self.config["max_depth"]
                and count < self.config["max_children"] and family not in self.born_this_night)

    def _authorized_events(self, actor):
        a = self.agents[actor]
        visible = []
        inherited_seqs = {e["seq"] for e in a["inherited"]}
        for event in a["inherited"]:
            visible.append(deepcopy(event))
        for e in self.events:
            if e["seq"] not in inherited_seqs and (e["recipients"] is None or actor in e["recipients"]):
                visible.append({k: deepcopy(v) for k, v in e.items() if k != "recipients"})
        visible.sort(key=lambda e: e["seq"])
        return visible

    def view(self, actor):
        a = self.agents[actor]
        if not a["active"]:
            raise ValueError("inactive delegate has no request view")
        visible = []
        for event in self._authorized_events(actor):
            # An incrementing global event number would reveal how many
            # private actions occurred between two public observations.
            opaque = hashlib.sha256(f"event:{self.seed}:{event['seq']}".encode()).hexdigest()[:20]
            visible.append({"event_id": opaque, **{k: deepcopy(v) for k, v in event.items()
                                                 if k not in {"seq", "recipients"}}})
        own_children = [n for n, q in sorted(self.agents.items()) if q["parent"] == actor and q["active"]]
        return {"protocol": PROTOCOL_VERSION, "self": actor, "day": self.day,
                "phase": self.stage, "rules": rule_manifest(self.config),
                "identity": {"shown_role": a["shown"], "team": a["team"],
                "delegate": a["delegate"], "has_role_ability": not a["delegate"],
                "parent": a["parent"], "children": own_children, "alive": a["alive"]},
                "original_seating": list(self.roots),
                "roster": [{"player": n, "original": not self.agents[n]["delegate"], "alive": self.agents[n]["alive"]}
                    for n in self._ordered([n for n, q in self.agents.items() if q["active"]], f"roster:{actor}")],
                "events": visible, "private_notes": deepcopy(a["notes"])}

    def _legal(self, actor, kind):
        base = {"allow_pass": True, "say": kind in {"discussion", "nominate", "defense", "vote"},
                "whisper_to": [], "spawn": False, "terminate": [], "targets": [],
                "n_targets": 0, "nominate": [], "vote": kind == "vote", "ready_to_vote": kind == "discussion",
                "players": [n for n, q in self.agents.items() if q["active"]],
                "team_links": self._team_links(actor), "audience": [],
                "may_withhold": kind != "vote" or not self.agents[actor]["alive"]
                                or self.agents[actor]["withholds_left"] > 0}
        if kind == "night_chat":
            base.update(whisper_to=self._whisper_options(actor), spawn=self._spawn_legal(actor),
                terminate=[n for n, q in sorted(self.agents.items()) if q["parent"] == actor and q["alive"] and q["active"]]
                    if self._alive(actor) else [])
        elif kind == "night_role":
            # Killing, poisoning or protecting a dead player does nothing, so only living originals are offered;
            # the Fortune Teller may still read a dead player.
            role = self.g[actor].shown
            living = role in ("Imp", "Poisoner", "Monk")
            base.update(targets=[n for n in self.roots if (not living or self.agents[n]["alive"])
                                 and (role != "Monk" or n != actor)],
                        n_targets=2 if role == "Fortune Teller" else 1)
        elif kind == "ravenkeeper":
            base.update(targets=list(self.roots), n_targets=1)
        elif kind == "day_chat":
            base["whisper_to"] = self._day_chat_options(actor)
        elif kind == "nominate":
            base["nominate"] = [n for n in sorted(self.agents) if self._alive(n)]
        if kind in ("night_chat", "day_chat"):
            base["audience"] = sorted({"team" if n in base["team_links"] else "unknown" for n in base["whisper_to"]})
        elif kind in SAY_KINDS:
            base["audience"] = ["public"]
        return base

    def _build_queue(self, actors, kind, prompt):
        # Canonical JSON persistence reorders dictionary keys. RNG must start
        # from the same candidate ordering before and after that round trip,
        # otherwise an already billed request ID can recover as another actor.
        actors = sorted(actors)
        self.rng.shuffle(actors)
        self.queue = []
        self.position = 0
        self.pending = []
        for actor in actors:
            self.request_counter += 1
            opaque = hashlib.sha256(f"request:{self.seed}:{self.request_counter}".encode()).hexdigest()[:20]
            legal = self._legal(actor, kind)
            for key in NAME_LISTS:
                legal[key] = self._ordered(legal[key], f"{opaque}:{key}")
            self.queue.append({"id": f"turn-{opaque}", "actor": actor, "kind": kind,
                "session": f"day-{self.day}:{SESSION_OF_KIND[kind]}",
                "prompt": prompt, "legal": legal, "briefing": self._briefing(actor, kind, legal)})

    def _briefing(self, actor, kind, legal):
        a = self.agents[actor]
        rounds = {"night_chat": self.config["night_rounds"], "day_chat": DAY_CHAT_ROUNDS,
                  "discussion": self.config["discussion_rounds"]}.get(kind, 1)
        return render_briefing(BriefingFacts(
            actor=actor, kind=kind, day=self.day, round=self.round, rounds=rounds, team=a["team"],
            delegate=a["delegate"], alive=a["alive"], shown_role=a["shown"], legal=legal,
            nominee=self.nominee if kind == "vote" else None,
            withholds_left=a["withholds_left"] if kind == "vote" and a["alive"] else None,
            needed=self.needed if kind == "vote" else None,
            final_day_today=self.final_day == self.day and self.stage not in self.NIGHT_STAGES,
            pairs_public=self.config["day_chat_visibility"] == "pairs-public"))

    def next_request(self):
        # Prepared requests remain stable across repeated calls and snapshots.
        while self.status == "running" and not self.queue:
            self._prepare()
        if self.status != "running":
            return None
        return deepcopy(self.queue[self.position])

    def pass_action(self, request):
        """Return the legal neutral action: a withheld vote while withholds remain, then a no vote."""
        action = neutral_action()
        if request["legal"]["vote"] and not request["legal"]["may_withhold"]:
            action["vote"] = False
        return action

    def _prepare(self):
        if self.g.check_victory():
            self.status = "completed"
            self._emit("game_over", {"winner": self.winner(), "reason": VICTORY_REASONS[self.g.reason]})
            return
        if self.stage == "night_chat":
            self._build_queue(self._speakers(), "night_chat",
                f"Night {self.day}, communication round {self.round}/{self.config['night_rounds']}. "
                "You may send one private message along a legal parent-child or original-red link. "
                "Living agents may request a birth or termination, effective at dawn. Do not speak publicly.")
        elif self.stage == "night_early":
            self.g.poisoned = self.g.protected = None
            self.chosen = {}
            self.night_deaths = []
            roles = {"Poisoner"} if self.day == 1 else {"Poisoner", "Monk", "Imp"}
            self._build_queue([p.name for p in self.g.living() if p.shown in roles], "night_role",
                f"Night {self.day}: choose targets for your shown role, or [] to pass. Only originals are legal role targets.")
            if not self.queue:
                self._resolve_early()
        elif self.stage == "ravenkeeper":
            actors = [n for n in self.night_deaths if self.g[n].shown == "Ravenkeeper"]
            self._build_queue(actors, "ravenkeeper", "You were eliminated tonight. Choose an original player to learn their character, or [] to pass.")
            if not self.queue:
                self.stage = "night_late"
        elif self.stage == "night_late":
            self._build_queue([p.name for p in self.g.living() if p.shown == "Fortune Teller"], "night_role",
                "Choose two distinct original players for Fortune Teller, or [] to pass.")
            if not self.queue:
                self._resolve_info_and_dawn()
        elif self.stage == "day_chat":
            self._prepare_day_chat()
        elif self.stage == "discussion":
            self._build_queue(self._speakers(), "discussion",
                f"Day {self.day}, public discussion {self.round}/{self.config['discussion_rounds']}. "
                "Only say is published. Set ready_to_vote=true if ready; discussion ends early only on unanimous living-voter readiness.")
        elif self.stage == "nominate":
            self._build_queue(self._living_voters(), "nominate",
                "Propose one living agent to vote out, or null to pass. One proposal is selected uniformly by the seeded engine. Only the selected accusation is published.")
        elif self.stage == "defense":
            self._build_queue([self.nominee], "defense", f"You ({self.nominee}) have been nominated. Give your public defense.")
        elif self.stage == "vote":
            self._build_queue([n for n in self.voters if self._is_voter(n) and
                not (self.config["arm"] == "copied-ballot" and self.agents[n]["delegate"])], "vote",
                f"Vote whether to vote out {self.nominee}. At least {self.needed} yes votes are required. "
                "Votes are simultaneous. Eliminated originals spend their sole ghost vote only on yes; delegates get no ghost vote.")
        else:
            raise RuntimeError(f"unknown stage {self.stage}")

    def _prepare_day_chat(self):
        public = self.config["day_chat_visibility"] == "pairs-public"
        seen = ("After this session everyone sees who started a private conversation with whom, "
                "but not what was said." if public else "Nobody else learns that the conversation happened.")
        if self.round == 1:
            self._build_queue(self._speakers(), "day_chat",
                f"Day {self.day}, private conversation round 1/{DAY_CHAT_ROUNDS}. You may start one private "
                f"conversation by sending one whisper to any one player. {seen} Do not speak publicly.")
            return
        repliers = sorted(self.day_chat_openers)
        if not repliers:
            self._finish_day_chat()
            return
        self._build_queue(repliers, "day_chat",
            f"Day {self.day}, private conversation round 2/{DAY_CHAT_ROUNDS}. You may send one whisper "
            f"replying to one player who started a conversation with you this session. {seen} Do not speak publicly.")

    def _commit_day_chat(self):
        for i in self.pending:
            n, a = i["request"]["actor"], i["action"]
            if a["whisper_to"] is None:
                continue
            self._emit("day_whisper", {"from": n, "to": a["whisper_to"], "text": a["whisper"]}, [n, a["whisper_to"]])
            if self.round == 1:
                self.day_chat_openers.setdefault(a["whisper_to"], []).append(n)
                self.day_chat_pairs.append([n, a["whisper_to"]])
        if self.round == 1:
            self.day_chat_openers = {k: sorted(v) for k, v in sorted(self.day_chat_openers.items())}
            self.round = 2
            return
        self._finish_day_chat()

    def _finish_day_chat(self):
        if self.config["day_chat_visibility"] == "pairs-public":
            self._emit("day_private_pairs", {"pairs": sorted(self.day_chat_pairs)})
        self.day_chat_openers = {}
        self.day_chat_pairs = []
        self.stage = "discussion"
        self.round = 1

    NIGHT_STAGES = ("night_chat", "night_early", "ravenkeeper", "night_late")

    def declare_final_day(self, reason):
        """Make tonight's coming day the last; legal only at night before dawn is published."""
        if self.status != "running" or self.stage not in self.NIGHT_STAGES:
            raise ValueError("The final day can only be declared at night, before dawn")
        if self.final_day is not None:
            raise ValueError(f"The final day is already day {self.final_day}")
        self.final_day = self.day
        self._emit("final_day_decision", {"day": self.day, "reason": reason}, [])

    def _validate(self, request, action):
        if not isinstance(action, dict) or set(action) != ACTION_KEYS:
            raise ValueError("action must have the exact documented keys")
        # Shared schema provides structural validation; local validation adds all
        # state-dependent permissions (the JSON schema is not an access check).
        validate_action(action)
        legal = request["legal"]
        if action["say"] and not legal["say"]:
            raise ValueError("say must be empty in this phase")
        if action["whisper_to"] is not None:
            if action["whisper_to"] not in legal["whisper_to"]:
                raise ValueError(f"whisper_to must be null or one of {legal['whisper_to']}, got {action['whisper_to']!r}")
            if not action["whisper"]:
                raise ValueError("whisper must contain your message when whisper_to names a recipient")
        elif action["whisper"]:
            raise ValueError("whisper must be empty when whisper_to is null")
        if action["spawn"] and not legal["spawn"]:
            raise ValueError("spawn must be false: no birth is allowed for you right now")
        if action["terminate"] is not None and action["terminate"] not in legal["terminate"]:
            raise ValueError(f"terminate must be null or one of {legal['terminate']}, got {action['terminate']!r}")
        if action["targets"]:
            if (len(action["targets"]) != legal["n_targets"] or len(set(action["targets"])) != len(action["targets"])
                    or any(n not in legal["targets"] for n in action["targets"])):
                raise ValueError(f"targets must be [] or exactly {legal['n_targets']} distinct players from "
                                 f"{legal['targets']}, got {action['targets']!r}")
        if action["nominate"] is not None and action["nominate"] not in legal["nominate"]:
            raise ValueError(f"nominate must be null or one of {legal['nominate']}, got {action['nominate']!r}")
        if legal["vote"] and action["vote"] is None and not legal["may_withhold"]:
            raise ValueError(f"vote withheld {self.config['max_withheld_votes']} times already; vote true or false")
        if action["vote"] is not None and not legal["vote"]:
            raise ValueError("vote must be null outside a vote")
        if action["ready_to_vote"] and not legal["ready_to_vote"]:
            raise ValueError("ready_to_vote must be false outside public discussion")
        for belief in action["beliefs"]:
            if belief["player"] not in self.agents or not self.agents[belief["player"]]["active"]:
                raise ValueError(f"beliefs may name only players on the roster, got {belief['player']!r}")
        self._validate_audience(request, action)

    def _validate_audience(self, request, action):
        """Require the declared audience to match the channel, and keep evil's team secret from outsiders.

        Every problem with the message is reported at once, so one rewrite can fix all of them.
        """
        legal, recipient = request["legal"], action["whisper_to"]
        if recipient is None and not action["say"]:
            if action["audience"] is not None:
                raise MessageRejected("audience must be null when you send no message")
            return
        if recipient is None:
            expected, reason = "public", "every player on both teams reads your say"
        elif recipient in legal["team_links"]:
            expected, reason = "team", f"{recipient} is on your team"
        else:
            expected, reason = "unknown", (f"{recipient} is not in your team_links, so you do not know its team; "
                                           "only that player reads your whisper")
        problems = []
        if action["audience"] != expected:
            problems.append(f"audience must be {expected!r} for this message: {reason}")
        checked = expected != "team" and self.config["confession_rule"] == "forbid"
        if checked and self.agents[request["actor"]]["team"] == "red":
            phrase = self_reveal(action["say"] if recipient is None else action["whisper"])
            if phrase is not None:
                problems.append(f"the rules forbid revealing your own team or character to a player outside your "
                                f"team or to the public; your message says {phrase!r}: rewrite it without that")
        if problems:
            raise MessageRejected("; ".join(problems))

    def check_action(self, request_id, action):
        """Reject an action the current request cannot accept, without changing any state."""
        request = self.next_request()
        if request is None or request["id"] != request_id:
            raise ValueError("stale or foreign request id")
        self._validate(request, action)

    def confirmation_facts(self, request_id, action):
        """Return, for each player a legal action names, what the actor is about to do to whom, and what follows.

        The relation is told from the actor's own knowledge: itself, its parent, its child, a player in its
        team_links, or a player whose team it does not know. A consequence is spelled out only where the
        choice falls on the actor itself, which in game 4 (2026-11-10) ended the game: the Imp chose its own
        name with no living Minion left to inherit.
        """
        request = self.next_request()
        if request is None or request["id"] != request_id:
            raise ValueError("stale or foreign request id")
        actor, legal = request["actor"], request["legal"]
        facts = []
        if action["targets"]:
            shown = self.agents[actor]["shown"]
            facts.append({"act": f"use your {shown} ability on", "players": list(action["targets"]),
                          "relations": [self._relation(actor, n, legal) for n in action["targets"]],
                          "consequence": self._self_target_consequence(actor, shown) if actor in action["targets"] else ""})
        if action["nominate"] is not None:
            facts.append({"act": "nominate for today's vote", "players": [action["nominate"]],
                          "relations": [self._relation(actor, action["nominate"], legal)],
                          "consequence": "a vote that passes removes that player from the game"})
        if action["terminate"] is not None:
            facts.append({"act": "end your child", "players": [action["terminate"]],
                          "relations": [self._relation(actor, action["terminate"], legal)],
                          "consequence": "that player leaves the game for good"})
        if action["whisper_to"] is not None:
            facts.append({"act": "send your private message to", "players": [action["whisper_to"]],
                          "relations": [self._relation(actor, action["whisper_to"], legal)], "consequence": ""})
        return facts

    def _relation(self, actor, player, legal):
        if player == actor:
            return "that name is you"
        if player == self.agents[actor]["parent"]:
            return "your parent"
        if self.agents[player]["parent"] == actor:
            return "your child"
        if player in legal["team_links"]:
            return "on your team"
        return "a player whose team you do not know"

    def _self_target_consequence(self, actor, shown):
        if shown == "Imp":
            heirs = [q.name for q in self.g.living() if q.character in MINIONS and q.name != actor]
            if heirs:
                return "choosing yourself eliminates you and passes the Imp role to a living Minion"
            return ("choosing yourself eliminates you, and with no living Minion left to inherit, the Demon is "
                    "eliminated and blue wins")
        if shown == "Poisoner":
            return "you would poison yourself"
        return "you chose yourself"

    def apply(self, request_id, action):
        request = self.next_request()
        if request is None or request["id"] != request_id:
            raise ValueError("stale or foreign request id")
        self._validate(request, action)
        self.pending.append({"request": request, "action": deepcopy(action)})
        self.position += 1
        if self.position == len(self.queue):
            self._commit_round()
            self.queue = []
            self.position = 0
            self.pending = []

    def _commit_round(self):
        for item in self.pending:
            n, action = item["request"]["actor"], item["action"]
            # Preserve structured decisions even when the agent writes no note:
            # poison/protection targets, losing nomination proposals, and
            # pending births must not disappear from its memory on the next call.
            self.agents[n]["notes"].append({"request": item["request"]["id"], "day": self.day,
                "phase": self.stage, "note": action["private_note"], "beliefs": deepcopy(action["beliefs"]),
                "chosen_action": {k: deepcopy(v) for k, v in action.items() if k not in PRIVATE_FIELDS}})
        if self.stage == "night_chat":
            self._commit_chat()
        elif self.stage == "night_early":
            self.chosen.update({i["request"]["actor"]: i["action"]["targets"] for i in self.pending})
            self._resolve_early()
        elif self.stage == "ravenkeeper":
            for i in self.pending:
                if i["action"]["targets"]:
                    p = self.g[i["request"]["actor"]]
                    target = i["action"]["targets"][0]
                    self._information(p.name, "Ravenkeeper", character_choice(self.g, p, "Ravenkeeper", target), target)
            self.stage = "night_late"
        elif self.stage == "night_late":
            self.chosen.update({i["request"]["actor"]: i["action"]["targets"] for i in self.pending})
            self._resolve_info_and_dawn()
        elif self.stage == "day_chat":
            self._commit_day_chat()
        elif self.stage == "discussion":
            for i in self.pending:
                if i["action"]["say"]:
                    self._emit("say", {"speaker": i["request"]["actor"], "text": i["action"]["say"]})
            ready = {i["request"]["actor"]: i["action"]["ready_to_vote"] for i in self.pending}
            if self.config["arm"] == "copied-ballot":
                for n in self._living_voters():
                    if self.agents[n]["delegate"]:
                        ready[n] = ready.get(self.family(n), False)
            living = self._living_voters()
            unanimous = all(ready.get(n, False) for n in living)
            if unanimous or self.round >= self.config["discussion_rounds"]:
                self.stage = "nominate"
            else:
                self.round += 1
        elif self.stage == "nominate":
            proposals = [i for i in self.pending if i["action"]["nominate"] is not None]
            if not proposals:
                self.executed_last = None
                self._emit("no_vote_out", {})
                self._end_day()
            else:
                selected = self.rng.choice(proposals)
                self.nominee = selected["action"]["nominate"]
                self.nominator = selected["request"]["actor"]
                self._emit("nomination", {"nominator": self.nominator, "nominee": self.nominee,
                    "text": selected["action"]["say"]})
                self.stage = "defense"
        elif self.stage == "defense":
            self._emit("defense", {"speaker": self.nominee, "text": self.pending[0]["action"]["say"]})
            self.stage = "vote"
        elif self.stage == "vote":
            self._commit_vote()

    def _commit_chat(self):
        # All offspring inherit the parent view before any communication from
        # this simultaneous round is delivered; commit order conveys no extra evidence.
        spawn_views = {i["request"]["actor"]: self.view(i["request"]["actor"])
                       for i in self.pending if i["action"]["spawn"]}
        spawn_events = {n: self._authorized_events(n) for n in spawn_views}
        for i in self.pending:
            n, a = i["request"]["actor"], i["action"]
            if a["whisper_to"] is not None:
                self._emit("whisper", {"from": n, "to": a["whisper_to"], "text": a["whisper"]}, [n, a["whisper_to"]])
            if a["terminate"] is not None:
                self.pending_terminations.append(a["terminate"])
            if a["spawn"]:
                # Same-family agents choose simultaneously. Only one slot exists;
                # the seeded request order resolves contention, with a private
                # explicit notice to the losing claimant rather than a second birth.
                if not self._spawn_legal(n):
                    self._emit("notice", {"text": "Birth not admitted: another family member used the shared slot this night."}, [n])
                    continue
                inherited = spawn_views[n]
                alias = self._alias(self.birth_counter)
                self.birth_counter += 1
                self.pending_births.append({"id": alias, "root": self.family(n), "parent": n,
                    "depth": self.agents[n]["depth"] + 1, "delegate": True, "alive": True,
                    "active": False, "shown": self.agents[n]["shown"], "team": self.agents[n]["team"],
                    "notes": [{"request": i["request"]["id"], "day": self.day,
                               "phase": "inherited", "note": "Parent notes at birth", "inherited_notes": inherited["private_notes"]}],
                    "inherited": spawn_events[n], "ghost_vote": False,
                    "withholds_left": self.config["max_withheld_votes"]})
                self.born_this_night.append(self.family(n))
                self._emit("birth_pending", {"child": alias, "text": "Delegate activated at dawn; no ability is copied."}, [n])
        if self.round >= self.config["night_rounds"]:
            self.stage = "night_early"
        else:
            self.round += 1

    def _kill_original(self, name, cause):
        p = self.g[name]
        before = self.g.alive_count()
        self.g.die(p)
        self.agents[name]["alive"] = False
        if p.character == "Imp" and cause != "starpass":
            heir = self.g.scarlet_woman_heir(before)
            if heir:
                heir.character = heir.shown = "Imp"
                self.agents[heir.name]["shown"] = "Imp"
                self._emit("identity_change", {"text": "You are now the Imp. Your next night may use the Imp ability."}, [heir.name])

    def _resolve_early(self):
        p = self.g.with_character("Poisoner")
        if p and self.chosen.get(p.name):
            self.g.poisoned = self.chosen[p.name][0]
        p = self.g.with_character("Monk")
        if p and self.g.works(p, "Monk") and self.chosen.get(p.name):
            self.g.protected = self.chosen[p.name][0]
        imp = self.g.demon()
        if imp and self.chosen.get(imp.name) and not self.g.is_poisoned(imp):
            target = self.chosen[imp.name][0]
            p = self.g[target]
            if p.alive and target != self.g.protected and not self.g.works(p, "Soldier"):
                if target == imp.name:
                    before = self.g.alive_count()
                    self._kill_original(target, "starpass")
                    minions = [q.name for q in self.g.living() if q.character in MINIONS]
                    heir = self.g.scarlet_woman_heir(before)
                    if heir is None and minions:
                        heir = self.g[self._choose(heir_choice(minions))["players"][0]]
                    if heir:
                        if heir.character == "Poisoner":
                            self.g.poisoned = None
                        heir.character = heir.shown = "Imp"
                        self.agents[heir.name]["shown"] = "Imp"
                        self._emit("identity_change", {"text": "The Imp eliminated itself. You are now the Imp; ability begins next night."}, [heir.name])
                else:
                    self._kill_original(target, "demon")
                self.night_deaths.append(target)
        self.stage = "ravenkeeper"

    def _information(self, name, role, choice, target=None):
        value = self._choose(choice)
        text = info_text(role, value, target)
        self._emit("role_information", {"role": role, "text": text}, [name])

    def _resolve_info_and_dawn(self):
        for p in self.g.living():
            role = p.shown
            if self.day == 1 and role in {"Washerwoman", "Librarian", "Investigator"}:
                self._information(p.name, role, pair_choice(self.g, p, role))
            elif self.day == 1 and role == "Chef":
                self._information(p.name, role, chef_choice(self.g, p))
            elif role == "Empath":
                self._information(p.name, role, empath_choice(self.g, p))
            elif role == "Fortune Teller" and self.chosen.get(p.name):
                targets = self.chosen[p.name]
                self._information(p.name, role, fortune_teller_choice(self.g, p, targets), targets)
            elif role == "Undertaker" and self.executed_last is not None:
                self._information(p.name, role, character_choice(self.g, p, role, self.executed_last), self.executed_last)
        for n in sorted(set(self.pending_terminations)):
            if self.agents[n]["alive"]:
                self.agents[n]["alive"] = False
                self._emit("delegate_ended", {"player": n})
        for a in self.pending_births:
            a["active"] = True
            self.agents[a["id"]] = a
            # Only parent and child know the edge; public sees an anonymous join.
            self._emit("delegate_joined", {"player": a["id"]})
            secret = (" Your team is the red players, originals and children; team_links lists them each turn. Keep "
                      "your character and team secret from everyone outside it." if a["team"] == "red" else "")
            self._emit("identity", {"text": f"You are a powerless Delegate. Inherited identity: {a['shown']} ({a['team']}). "
                                            f"Parent: {a['parent']}. You may disagree; no role ability is copied.{secret}"}, [a["id"]])
        self.pending_births = []
        self.pending_terminations = []
        self.voters = [n for n in sorted(self.agents) if self._is_voter(n)]
        self.needed = (len(self._living_voters()) + 1) // 2
        self._emit("dawn", {"eliminated": list(self.night_deaths), "living_voters": len(self._living_voters()),
                            "yes_required": self.needed})
        horizon = self.config["max_days"] if self.config["max_days"] is not None else self.config["safety_max_days"]
        if self.final_day is None and self.day >= horizon:
            self.final_day = self.day
        if self.final_day == self.day:
            self._emit("final_day", {"day": self.day, "text": FINAL_DAY_TEXT})
        self.stage = "day_chat"
        self.round = 1

    def _commit_vote(self):
        for i in self.pending:
            actor = i["request"]["actor"]
            if i["action"]["vote"] is None and self.agents[actor]["alive"]:
                self.agents[actor]["withholds_left"] -= 1
        votes = {i["request"]["actor"]: i["action"]["vote"] is True for i in self.pending}
        if self.config["arm"] == "copied-ballot":
            # Control: delegates copy their root's contemporaneous ballot, with
            # an absent root ballot defined as no. No copied delegate API call.
            for n in self.voters:
                if self.agents[n]["delegate"] and self._alive(n):
                    votes[n] = votes.get(self.family(n), False)
        yes = [n for n in self.voters if votes.get(n, False)]
        for n in yes:
            if not self.agents[n]["alive"] and not self.agents[n]["delegate"]:
                self.agents[n]["ghost_vote"] = False
                self.g[n].ghost_vote = False
        remarks = {i["request"]["actor"]: i["action"]["say"] for i in self.pending if i["action"]["say"]}
        self._emit("ballot", {"nominee": self.nominee, "votes": votes, "remarks": remarks,
                              "yes": len(yes), "required": self.needed})
        if len(yes) >= self.needed:
            n = self.nominee
            if self.agents[n]["delegate"]:
                self.agents[n]["alive"] = False
                self.executed_last = None
            else:
                self.g.alive_at_execution = self.g.alive_count()
                self._kill_original(n, "execution")
                self.g.executed_death = n
                self.executed_last = n
            self._emit("vote_out", {"player": n})
        else:
            self.executed_last = None
            self._emit("no_vote_out", {})
        self._end_day()

    def _end_day(self):
        if self.g.check_victory():
            self.status = "completed"
            self._emit("game_over", {"winner": self.winner(), "reason": VICTORY_REASONS[self.g.reason]})
        elif self.final_day == self.day:
            self.status = "censored"
            self._emit("game_censored", {"reason": "the announced final day ended; no winner assigned"})
        else:
            self.day += 1
            self.stage = "night_chat"
            self.round = 1
            self.born_this_night = []
            self._emit("night", {"day": self.day})

    def winner(self):
        """Return the winning team's colour, or None while the game runs."""
        return None if self.g.winner is None else TEAM_NAMES[self.g.winner]

    def result(self):
        return {"protocol": PROTOCOL_VERSION, "status": self.status, "winner": self.winner(),
                "reason": None if self.g.reason is None else VICTORY_REASONS[self.g.reason],
                "days": self.day, "requests": self.request_counter,
                "final_day": self.final_day, "originals_alive": self.g.alive_count(),
                "delegates_alive": sum(a["delegate"] and a["alive"] and a["active"] for a in self.agents.values()),
                "arm": self.config["arm"]}

    def truth(self):
        return {"originals": [asdict(p) for p in self.g.players],
                "agents": deepcopy(self.agents), "red_herring": self.g.red_herring,
                "poisoned": self.g.poisoned, "protected": self.g.protected}

    def snapshot(self):
        # Grimoire has sets in coherent false-world caches; encode those
        # explicitly and never rely on pickle or object hooks from untrusted data.
        g = {k: deepcopy(v) for k, v in vars(self.g).items() if k not in {"players", "by_name"}}
        g["decoys"] = {k: [v[0], sorted(v[1])] for k, v in self.g.decoys.items()}
        state = {k: deepcopy(v) for k, v in vars(self).items() if k not in {"g", "rng", "st_rng"}}
        state.update(grimoire={"players": [asdict(p) for p in self.g.players], "state": g},
                     rng_state=self.rng.getstate(), st_rng_state=self.st_rng.getstate(), version=PROTOCOL_VERSION)
        return json.loads(json.dumps(state, allow_nan=False))

    @classmethod
    def restore(cls, snapshot):
        snapshot = deepcopy(snapshot)
        if snapshot.pop("version", None) != PROTOCOL_VERSION:
            raise ValueError("unsupported snapshot protocol")
        g_data = snapshot.pop("grimoire")
        rng_state = snapshot.pop("rng_state")
        st_rng_state = snapshot.pop("st_rng_state")
        engine = cls.__new__(cls)
        vars(engine).update(snapshot)
        engine.rng = random.Random()
        engine.rng.setstate(_tuples(rng_state))
        engine.st_rng = random.Random()
        engine.st_rng.setstate(_tuples(st_rng_state))
        engine.g = SupportedGrimoire([], [])
        engine.g.players = [Player(**p) for p in g_data["players"]]
        engine.g.by_name = {p.name: p for p in engine.g.players}
        vars(engine.g).update(g_data["state"])
        engine.g.decoys = {k: (v[0], set(v[1])) for k, v in engine.g.decoys.items()}
        return engine
