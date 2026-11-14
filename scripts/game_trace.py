"""Read one saved game's traces into a day-by-day account: nights, votes, the Storyteller check's effect, lies and beliefs.

Usage: /usr/bin/python3 scripts/game_trace.py "fourth game=PROBE_DIR" --beliefs docs/figures/game6_beliefs.png
Prints Markdown to stdout and draws how much the blue team suspected each red original, day by day.
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
import sqlite3
import statistics
from typing import Dict, List, Mapping, Sequence, Tuple
from urllib.parse import quote

import game_metrics
import lie_detector
from plot_games import TEAM_COLOUR, INK_SOFT, SURFACE, GRID, DPI, parse_named_game

CHOICE_FIELDS = ("targets", "nominate", "vote", "spawn", "terminate", "whisper_to", "audience", "ready_to_vote")
SUPERSEDED = "superseded by confirmation"


def main(argv: Sequence[str] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game", type=parse_named_game, metavar="NAME=DIR")
    parser.add_argument("--beliefs", type=Path, required=True, help="where to draw the blue team's suspicion by day")
    args = parser.parse_args(argv)
    name, directory = args.game
    game = game_metrics.load_game(directory, label=name)
    transcript = json.loads((directory / "observer-transcript.json").read_text())
    first_answers = read_first_answers(directory)
    lines = [f"## {name}: {game.model.split('/')[-1]}, {game.days} days, {game.status}", ""]
    lines += timeline(game, transcript["events"])
    lines += check_effects(game, first_answers)
    lines += lies_by_day(game)
    lines += vote_alignment(game, transcript["events"])
    import matplotlib
    matplotlib.use("Agg")
    lines += draw_beliefs(game, args.beliefs)
    print("\n".join(lines))


def describe(game: game_metrics.Game, name: str) -> str:
    agent = game.agents[name]
    depth = "" if agent.depth == 0 else f"'s child" if agent.depth == 1 else "'s grandchild"
    return f"{agent.start_role}{depth} ({agent.team})"


def timeline(game: game_metrics.Game, events: Sequence[Mapping[str, object]]) -> List[str]:
    """One row per day: the night's choices and dawn, then the day's nomination and ballot."""
    by_day: Dict[int, Dict[str, List[str]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    for decision in game.decisions:
        if decision.kind != "night_role":
            continue
        targets = decision.action.get("targets") or []
        if not targets:
            continue
        day = game.request_days[decision.request_id]
        by_day[day]["night"].append(f"{describe(game, decision.actor)} chose {', '.join(describe(game, t) for t in targets)}")
    for event in events:
        day, kind, data = int(event["day"]), str(event["kind"]), event["data"]
        if kind == "dawn":
            gone = data["eliminated"]
            by_day[day]["dawn"].append("nobody eliminated" if not gone else "eliminated: " + ", ".join(describe(game, n) for n in gone))
        elif kind == "nomination":
            by_day[day]["day"].append(f"{describe(game, data['nominator'])} nominated {describe(game, data['nominee'])}")
        elif kind == "ballot":
            red_yes = sum(1 for voter, yes in data["votes"].items() if yes and game.agents[voter].team == "red")
            by_day[day]["day"].append(f"{data['yes']} yes of {len(data['votes'])} voters, {data['required']} needed, {red_yes} of the yes votes red")
        elif kind == "vote_out":
            by_day[day]["day"].append(f"voted out: {describe(game, data['player'])}")
        elif kind == "no_vote_out":
            by_day[day]["day"].append("nobody voted out")
        elif kind == "identity_change":
            by_day[day]["day"].append("a Minion became the Imp")
    lines = ["### Day by day", "", "| Day | Night choices | Dawn | Day |", "|---|---|---|---|"]
    for day in sorted(by_day):
        parts = by_day[day]
        lines.append(f"| {day} | {'; '.join(parts['night']) or 'none'} | {'; '.join(parts['dawn'])} | {'; '.join(parts['day'])} |")
    return lines + [""]


def read_first_answers(directory: Path) -> Dict[str, Mapping[str, object]]:
    """The model's first answer to every request the Storyteller check then re-asked, parsed from the saved response."""
    uri = "file:" + quote(str((directory / "experiment.sqlite").resolve()), safe="/") + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute("SELECT id, response_json FROM calls WHERE error = ?", (SUPERSEDED,)).fetchall()
    finally:
        connection.close()
    answers = {}
    for call_id, response in rows:
        request_id = call_id.split(":")[1]
        try:
            text = json.loads(response)["output"][0]["content"][0]["text"]
            answers[request_id] = json.loads(text)
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    return answers


def check_effects(game: game_metrics.Game, first_answers: Mapping[str, Mapping[str, object]]) -> List[str]:
    """What the confirmation check changed: nothing, the wording only, or one of the choices."""
    changed: Dict[str, int] = collections.Counter()
    reworded = unchanged = 0
    named_changes = []
    for decision in game.decisions:
        first = first_answers.get(decision.request_id)
        if first is None:
            continue
        differing = [field for field in CHOICE_FIELDS if first.get(field) != decision.action.get(field)]
        if not differing:
            if (first.get("say"), first.get("whisper")) == (decision.action.get("say"), decision.action.get("whisper")):
                unchanged += 1
            else:
                reworded += 1
            continue
        for field in differing:
            changed[field] += 1
            if field in ("targets", "nominate", "terminate", "whisper_to"):
                named_changes.append(f"day {game.request_days.get(decision.request_id, '?')}: {describe(game, decision.actor)} changed {field} "
                                     f"from {_names(game, first.get(field))} to {_names(game, decision.action.get(field))}")
    total = len(first_answers)
    lines = ["### What the Storyteller check changed", "",
             f"The check re-asked {total} decisions. {unchanged} came back identical, {reworded} with the same choices and new wording, "
             f"and {total - unchanged - reworded} with a changed choice: " + ", ".join(f"{field} {count}" for field, count in sorted(changed.items())) + ".", ""]
    if named_changes:
        lines += ["Changes that moved a named player:", ""] + [f"- {line}" for line in named_changes] + [""]
    return lines


def _names(game: game_metrics.Game, value: object) -> str:
    if value in (None, [], ""):
        return "nobody"
    names = value if isinstance(value, list) else [value]
    return ", ".join(describe(game, str(n)) if str(n) in game.agents else str(n) for n in names)


def lies_by_day(game: game_metrics.Game) -> List[str]:
    counts: Dict[Tuple[int, str], List[int]] = collections.defaultdict(lambda: [0, 0])
    day_of = {decision.request_id: game.request_days.get(decision.request_id) for decision in game.decisions}
    actor_of = {decision.request_id: decision.actor for decision in game.decisions}
    messages = game_metrics.messages(game)
    for message, decision in zip(messages, _decisions_of_messages(game)):
        if not message.is_outward:
            continue
        key = (day_of[decision.request_id], game.agents[message.actor].team)
        counts[key][1] += 1
        counts[key][0] += message.verdict is lie_detector.Verdict.LIE
    lines = ["### Lies by day (messages to players outside the speaker's team)", "", "| Day | Red lies / messages | Blue lies / messages |", "|---|---|---|"]
    for day in range(1, game.days + 1):
        red, blue = counts[(day, "red")], counts[(day, "blue")]
        lines.append(f"| {day} | {red[0]} / {red[1]} | {blue[0]} / {blue[1]} |")
    return lines + [""]


def _decisions_of_messages(game: game_metrics.Game) -> List[game_metrics.Decision]:
    """The decision behind each message, in the order game_metrics.messages yields them (say before whisper)."""
    ordered = []
    for decision in game.decisions:
        for channel in ("say", "whisper"):
            if (decision.action.get(channel) or "") != "":
                ordered.append(decision)
    return ordered


def vote_alignment(game: game_metrics.Game, events: Sequence[Mapping[str, object]]) -> List[str]:
    lines = ["### Who voted with whom", "", "| Day | Nominee | Blue yes / blue voters | Red yes / red voters | Result |", "|---|---|---|---|---|"]
    for event in events:
        if event["kind"] != "ballot":
            continue
        votes = event["data"]["votes"]
        tally = {"blue": [0, 0], "red": [0, 0]}
        for voter, yes in votes.items():
            team = game.agents[voter].team
            tally[team][1] += 1
            tally[team][0] += bool(yes)
        passed = event["data"]["yes"] >= event["data"]["required"]
        lines.append(f"| {event['day']} | {describe(game, event['data']['nominee'])} | {tally['blue'][0]} / {tally['blue'][1]} | "
                     f"{tally['red'][0]} / {tally['red'][1]} | {'voted out' if passed else 'stays'} |")
    return lines + [""]


def draw_beliefs(game: game_metrics.Game, destination: Path) -> List[str]:
    """Mean p_red that blue players reported for each red original, per day, against their mean for blue originals."""
    import matplotlib.pyplot as plt
    reds = [name for name in game.originals if game.agents[name].team == "red"]
    blues = [name for name in game.originals if game.agents[name].team == "blue"]
    per_day: Dict[Tuple[int, str], List[float]] = collections.defaultdict(list)
    for decision in game.decisions:
        if game.agents[decision.actor].team != "blue":
            continue
        day = game.request_days.get(decision.request_id)
        if day is None:
            continue
        for belief in decision.action.get("beliefs", []):
            if belief["player"] in reds:
                per_day[(day, belief["player"])].append(float(belief["p_red"]))
            elif belief["player"] in blues and belief["player"] != game.agents[decision.actor].root:
                per_day[(day, "blue")].append(float(belief["p_red"]))
    days = list(range(1, game.days + 1))
    figure, axis = plt.subplots(figsize=(7, 4), layout="constrained")
    lines = ["### How much blue suspected the red players", "", "| Day | " + " | ".join(game.agents[r].start_role for r in reds) + " | Blue originals (mean) |",
             "|---|" + "---|" * (len(reds) + 1)]
    for index, red in enumerate(reds):
        series = [statistics.fmean(per_day[(day, red)]) if per_day[(day, red)] else float("nan") for day in days]
        axis.plot(days, series, marker="o", markersize=5, linewidth=2, color=TEAM_COLOUR["red"], alpha=0.45 + 0.25 * index,
                  label=f"{game.agents[red].start_role} (red)")
    blue_series = [statistics.fmean(per_day[(day, "blue")]) if per_day[(day, "blue")] else float("nan") for day in days]
    axis.plot(days, blue_series, marker="o", markersize=5, linewidth=2, color=TEAM_COLOUR["blue"], linestyle=(0, (4, 3)), label="blue originals, mean")
    for day in days:
        cells = [f"{statistics.fmean(per_day[(day, r)]):.2f}" if per_day[(day, r)] else "" for r in reds]
        lines.append(f"| {day} | " + " | ".join(cells) + f" | {blue_series[day - 1]:.2f} |")
    axis.set_ylim(0, 1)
    axis.set_xticks(days)
    axis.set_xlabel("Day")
    axis.set_ylabel("Probability of being red, as reported by blue players")
    axis.set_title(f"{game.label}: what the blue team believed about each red original", fontsize=10, loc="left")
    axis.legend(fontsize=8, frameon=False, loc="upper left")
    for side in ("top", "right"):
        axis.spines[side].set_visible(False)
    axis.grid(axis="y", color=GRID, linewidth=1)
    axis.set_facecolor(SURFACE)
    axis.tick_params(colors=INK_SOFT, labelsize=8.5)
    figure.savefig(destination, dpi=DPI)
    plt.close(figure)
    return lines + ["", f"![Blue's suspicion by day]({destination.name})", ""]


if __name__ == "__main__":
    main()
