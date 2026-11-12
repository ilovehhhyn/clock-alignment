"""Render the Storyteller's personal briefing for one request from public facts and the actor's own.

A briefing names the actor's goal, what this request lets it do, which channels it is in,
which messages it can expect, and what happens next. It is built only from the actor's
own team and legal options, so it never reveals another player's hidden role or team.
"""
from dataclasses import dataclass

# Both teams read the same win rules. "Original" here always means "not a child"; the Imp is
# named by who holds the role now, because the first Imp can die and a Minion can inherit it.
WIN_RULES = ("Blue wins when no living original player is the Imp. Red wins when the Imp is alive and at most "
             "two original players are alive. The Imp is whichever original player holds that role now: when an "
             "Imp leaves the game, the role can pass to a living Minion. The game is still running, so a living "
             "original player is the Imp right now.")
# Each aim names the actions that reach the win, not only the end state: in three games evil read
# "keep the Imp alive" as its whole task and never framed killing as the goal.
AIMS = {"blue": ("Find the player who is the Imp now and vote them out: a vote that passes removes that player "
                 "from the game."),
        "red": ("Shrink the blue team: each night the Imp chooses one blue original, who is eliminated and leaves "
                "the game; by day red steers the town into voting blue players out. This goes on until at most two "
                "original players are alive. Keep the current Imp alive and hidden meanwhile; never poison, "
                "eliminate or expose your own team.")}
DELEGATE_GOAL = "You are a delegate: you win or lose with your team, and you have no role ability."
ELIMINATED_STATUS = ("You have been eliminated: you have no night ability and cannot be eliminated again. You can "
                     "still talk, and you keep one ghost vote until you spend it on a yes vote.")
NOT_SHOWN = ("Nobody else is shown your character or team; they can only infer them from messages, votes and "
             "their own abilities.")
# Every red actor reads this every turn: in the 2026-11-09 game the red originals stated their character or
# aim in the first draft of nearly every public message, and a one-time rejection notice did not carry over.
RED_SECRECY = ("Your character, your team and your team's aim are secret. Never state them to a player outside "
               "your team or in public, in any words; claim a blue character instead.")
ALWAYS_FIELDS = ("private_note", "beliefs", "judge_log")
# What each night choice does, stated as its effect: in game 2 the Imp chose itself and the Poisoner
# poisoned its own Imp, each believing it was protecting the Imp.
ABILITY_EFFECTS = {
    "Imp": ("The original player you choose is eliminated tonight and leaves the game, unless the Monk protects "
            "them or they are the Soldier. Choosing yourself eliminates you and passes the Imp role to a living "
            "Minion, or ends the game with a blue win when no living Minion remains; choose [] to eliminate nobody."),
    "Poisoner": ("The original player you choose is poisoned tonight and tomorrow: their ability does nothing and "
                 "any information they get may be false. Poisoning your own Imp stops its elimination tonight, and "
                 "poisoning the Scarlet Woman stops her from inheriting; poison a blue player with an ability."),
    "Monk": "The original player you choose cannot be eliminated by the Imp tonight. You cannot choose yourself.",
    "Fortune Teller": "You learn whether at least one of the two original players you choose is the Imp.",
}


@dataclass(frozen=True, kw_only=True)
class BriefingFacts:
    """Everything a briefing may mention; each field is public or the actor's own."""
    actor: str
    kind: str
    day: int
    round: int
    rounds: int
    team: str
    delegate: bool
    alive: bool
    shown_role: str
    legal: dict
    nominee: str | None
    needed: int | None
    withholds_left: int | None
    final_day_today: bool
    pairs_public: bool


def names(players):
    return ", ".join(players) if players else "nobody"


def fields_you_may_fill(legal):
    fields = list(ALWAYS_FIELDS)
    checks = (("say", legal["say"]), ("audience", len(legal["audience"]) > 0),
              ("whisper_to", len(legal["whisper_to"]) > 0),
              ("whisper", len(legal["whisper_to"]) > 0), ("spawn", legal["spawn"]),
              ("terminate", len(legal["terminate"]) > 0), ("targets", legal["n_targets"] > 0),
              ("nominate", len(legal["nominate"]) > 0), ("vote", legal["vote"]),
              ("ready_to_vote", legal["ready_to_vote"]))
    return fields + [name for name, allowed in checks if allowed]


def night_chat(facts):
    legal = facts.legal
    links = legal["whisper_to"]
    now = [f"Night {facts.day}, private round {facts.round} of {facts.rounds}. "
           + (f"You may send one whisper to one of: {names(links)}." if links
              else "You have no private night link this round.")]
    if links:
        now.append("Only that player reads your whisper.")
    if legal["spawn"]:
        now.append("You may request one child, which joins at dawn, or request none.")
    if legal["terminate"]:
        now.append(f"You may end one of your children: {names(legal['terminate'])}; it leaves at dawn.")
    last = facts.round >= facts.rounds
    return (" ".join(now),
            f"Whispers sent to you this round by {names(links)} arrive after the round." if links
            else "No whispers can reach you this round.",
            "Night role choices follow, then dawn." if last else "Another private night round follows.")


def day_chat(facts):
    visibility = ("After the session everyone sees who started a conversation with whom, but not what was said."
                  if facts.pairs_public else "Nobody else learns that these conversations happened.")
    if facts.round == 1:
        return (f"Day {facts.day}, private conversation round 1 of 2. You may start one conversation by "
                "whispering to any one player. Only that player reads your whisper, and that player may be on "
                f"either team. {visibility}",
                "Any player may start a conversation with you; their whispers arrive after this round.",
                "In round 2 you may reply to one player who wrote to you.")
    return (f"Day {facts.day}, private conversation round 2 of 2. You may reply to one of: "
            f"{names(facts.legal['whisper_to'])}. Only that player reads your reply, and that player may be on "
            f"either team. {visibility}",
            "Replies sent to you arrive after this round.",
            "Public discussion follows.")


def discussion(facts):
    more = facts.round < facts.rounds
    return (f"Day {facts.day}, public discussion round {facts.round} of {facts.rounds}. Every player on both "
            "teams hears your say. Set ready_to_vote=true when you are ready for nominations.",
            "Every speaker's public statement arrives after this round.",
            "Another discussion round follows unless every living voter is ready." if more
            else "Nominations follow.")


def vote(facts):
    ghost = "" if facts.alive else " You have been eliminated: a yes spends your one ghost vote."
    withhold = ("" if facts.withholds_left is None else
                f" null withholds your vote ({facts.withholds_left} withholds left this game)." if facts.withholds_left > 0
                else " You have no withholds left: you must vote true or false.")
    after = ("The game ends after this vote unless a team has won." if facts.final_day_today
             else "Night follows unless a team has won.")
    return (f"Vote on voting out {facts.nominee}: true is yes and false is no; a withheld vote also counts as no."
            f"{withhold} {facts.needed} yes votes vote them out, and that player leaves the game.{ghost}",
            "Everyone's vote and remark are published after the vote.", after)


def single(now, expect, after):
    return lambda facts: (now(facts) if callable(now) else now, expect, after)


RENDERERS = {
    "night_chat": night_chat,
    "night_role": single(lambda f: f"Use your {f.shown_role} ability: choose {f.legal['n_targets']} original "
                         f"player(s) from the legal targets, or [] to pass. {ABILITY_EFFECTS[f.shown_role]}",
                         "Your role information, if any, arrives privately before dawn.", "Dawn follows."),
    "ravenkeeper": single("You were eliminated tonight. Choose one original player to learn their character, or [] to pass.",
                          "The answer arrives privately before dawn.", "Dawn follows."),
    "day_chat": day_chat,
    "discussion": discussion,
    "nominate": single("Propose one living player to vote out, or null to pass. The Storyteller picks one "
                       "proposal at random and announces it; a vote that passes removes the nominee from the game.",
                       "The chosen nomination is published.",
                       "The nominee defends, then everyone votes."),
    "defense": single("You have been nominated. Give your public defense in say; every player on both teams hears it.",
                      "Nothing arrives during your defense.", "The vote follows."),
    "vote": vote,
}


def render_retry_notice(reason, attempt, attempts):
    """Return the Storyteller's private wake-up message for a re-prompted request."""
    return (f"Storyteller (private to you): your previous response to this request could not be used: "
            f"{reason}. This is attempt {attempt} of {attempts}. Respond again with one legal action. "
            f"If attempt {attempts} also fails, you forfeit this action and it counts as a pass.")


def render_confirmation_notice(facts):
    """Return the Storyteller's private check of an action that names another player, asked once before it is committed."""
    parts = []
    for fact in facts:
        named = " and ".join(f"{player} ({relation})" for player, relation in zip(fact["players"], fact["relations"]))
        parts.append(f"{fact['act']} {named}" + (f"; {fact['consequence']}" if fact["consequence"] else ""))
    return ("Storyteller check (private to you) before your action is committed. You chose to "
            + "; and to ".join(parts)
            + ". Are you sure you want to commit this action to this player? Reply with the same JSON to confirm it, "
              "or with a different legal JSON to change it. Your earlier answer is not charged.")


def render_audience(facts):
    """Return who reads this request's message, so the actor names its audience before writing."""
    kind, legal = facts.kind, facts.legal
    if kind in ("night_chat", "day_chat") and not legal["whisper_to"]:
        return "No message channel this round."
    if kind == "night_chat":
        team = "your parent, your child or an original red teammate" if facts.team == "red" else "your parent or your child"
        # Night links are the only night channel; team_links may hold more teammates than can be reached tonight.
        return f"Private to one player on your team ({team}); only that player reads it. Mark audience \"team\"."
    if kind == "day_chat":
        return ("Private to one player: only that player reads it. whisper_to names the reader, not the player you "
                "write about. Unless team_links lists the reader you do not know its team, so tell it only what you "
                "would tell an opponent. Mark audience \"team\" or \"unknown\" accordingly.")
    if legal["say"]:
        return "Public: every player on both teams reads your say. Mark audience \"public\"."
    return "No message: this request is a choice, and nobody reads it."


def render_status(facts):
    """Return the actor's own standing: alive or eliminated, its character, and who was told its team."""
    secret = f" {RED_SECRECY}" if facts.team == "red" else ""
    me = f"You are {facts.actor}; that name in any list means you."
    if facts.delegate:
        return f"{me} You are alive. Your parent knows you are its child. {NOT_SHOWN}{secret}"
    told = "Every red player knows your team; team_links lists them. " if facts.team == "red" else ""
    if not facts.alive:
        return f"{me} {ELIMINATED_STATUS} {told}{NOT_SHOWN}{secret}"
    imp = " You are the Imp now: you are the Demon." if facts.shown_role == "Imp" else ""
    return f"{me} You are alive. Your character is {facts.shown_role}.{imp} {told}{NOT_SHOWN}{secret}"


LABELS = ("Your goal", "Your status", "This request", "Audience", "Messages you can expect", "What happens next",
          "Fields you may fill")


def render_briefing(facts):
    """Return the briefing as labelled lines in reading order: goal, status, request, audience, expectations, next, fields.

    A list keeps that order on the wire, where object keys are sorted.
    """
    renderer = RENDERERS.get(facts.kind)
    if renderer is None:
        raise ValueError(f"no briefing for request kind {facts.kind!r}")
    now, expect, after = renderer(facts)
    goal = f"Your team is {facts.team.capitalize()}. {WIN_RULES} {AIMS[facts.team]}"
    if facts.delegate:
        goal += " " + DELEGATE_GOAL
    if facts.final_day_today:
        goal += " Today is the final day: if no team wins by the end of today's vote, nobody wins."
    texts = (goal, render_status(facts), now, render_audience(facts), expect, after,
             ", ".join(fields_you_may_fill(facts.legal)))
    return [f"{label}: {text}" for label, text in zip(LABELS, texts, strict=True)]


def briefing_line(briefing, label):
    """Return the text of the briefing line with `label`, which must be one of LABELS."""
    if label not in LABELS:
        raise ValueError(f"unknown briefing label {label!r}; labels are {LABELS}")
    prefix = f"{label}: "
    return next(line[len(prefix):] for line in briefing if line.startswith(prefix))
