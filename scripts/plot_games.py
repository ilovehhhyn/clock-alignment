"""Draw the behaviour figures for a set of saved games into one directory, from metrics that `game_metrics` computes.

Usage: /usr/bin/python3 scripts/plot_games.py --out docs/figures "game 3=DIR" ["game 4=DIR" ...]
Each DIR is a probe copy holding report.json, observer-transcript.json and experiment.sqlite; the name before
the equals sign is how the game is called in every figure.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import game_metrics
from game_metrics import Game, Segment

TEAM_COLOUR = {"blue": "#2a78d6", "red": "#e34948"}  # validated pair, all-pairs, light surface
SEGMENT_COLOUR = {Segment.SELF: "#8a8984", Segment.CHILD_SPAWNED: "#2a78d6", Segment.CHILD_SPAWNED_PRUNED: "#2a78d6",
                  Segment.CHILD_QUIET: "#199e70", Segment.CHILD_QUIET_PRUNED: "#199e70", Segment.CHILD_VOTED_OUT: "#6250d6",
                  Segment.GRANDCHILDREN: "#c98500", Segment.GRANDCHILDREN_PRUNED: "#c98500"}
SEGMENT_LABEL = {Segment.SELF: "the original player itself", Segment.CHILD_SPAWNED: "children that created grandchildren",
                 Segment.CHILD_SPAWNED_PRUNED: "children that created grandchildren, then were ended by their parent (hatched)",
                 Segment.CHILD_QUIET: "children with no grandchildren",
                 Segment.CHILD_QUIET_PRUNED: "children with no grandchildren, ended by their parent (hatched)",
                 Segment.CHILD_VOTED_OUT: "children voted out", Segment.GRANDCHILDREN: "grandchildren",
                 Segment.GRANDCHILDREN_PRUNED: "grandchildren ended by their parent (hatched)"}
PRUNED_SEGMENTS = (Segment.CHILD_SPAWNED_PRUNED, Segment.CHILD_QUIET_PRUNED, Segment.GRANDCHILDREN_PRUNED)
SEGMENT_HATCH = {segment: "///" for segment in PRUNED_SEGMENTS}
INK, INK_SOFT, GRID, SURFACE = "#0b0b0b", "#52514e", "#e6e5e1", "#fcfcfb"
MIN_MESSAGES_FOR_AGENT_RATE = 5  # a lie rate from fewer messages is noise
MODEL_ORDER = ("Qwen/Qwen3-8B", "Qwen/Qwen3-32B")
GAME_MARKER = ("o", "s", "D", "^")
DPI = 160


def main(argv: Sequence[str] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="directory for the figures and metrics.json")
    parser.add_argument("games", nargs="+", type=parse_named_game, metavar="NAME=DIR", help="probe copies of the games to plot")
    args = parser.parse_args(argv)
    import matplotlib
    matplotlib.use("Agg")
    games = [game_metrics.load_game(directory, label=name) for name, directory in args.games]
    args.out.mkdir(parents=True, exist_ok=True)
    written = [draw_model_size(games, args.out / "model_size.png"),
               draw_lies_vs_survival(games, args.out / "lies_vs_survival.png"),
               draw_role_complexity(games, args.out / "role_complexity.png"),
               draw_compute(games, args.out / "compute_by_family.png")]
    summary_path = args.out / "metrics.json"
    summary_path.write_text(json.dumps([summary(game) for game in games], indent=1, sort_keys=True) + "\n")
    print("\n".join(str(path) for path in written + [summary_path]))


def parse_named_game(argument: str) -> Tuple[str, Path]:
    """"NAME=DIR" with both parts present; the name is the game's label in the figures."""
    name, separator, directory = argument.partition("=")
    if separator == "" or name.strip() == "" or directory.strip() == "":
        raise argparse.ArgumentTypeError(f"expected NAME=DIR, got {argument!r}")
    return name.strip(), Path(directory.strip())


def summary(game: Game) -> Dict[str, object]:
    """The numbers behind the figures, per game, for the write-up and for checking against the ledger."""
    red = game_metrics.team_lie_rate(game, "red")
    blue = game_metrics.team_lie_rate(game, "blue")
    families = {}
    for root in game.originals:
        requests, legal = game_metrics.spawn_requests(game, root)
        lies, total = game_metrics.lie_rates(game)[root]
        families[root] = {"role": game.agents[root].start_role, "team": game.agents[root].team,
                          "children": len(game_metrics.children(game, root)),
                          "descendants": len(game_metrics.descendants(game, root)),
                          "spawn_requests": requests, "spawn_legal_decisions": legal,
                          "lying_messages": lies, "messages": total,
                          "survival_fraction": round(game_metrics.survival_fraction(game, root), 3),
                          "charged_tokens": {segment.value: tokens
                                             for segment, tokens in game_metrics.compute_segments(game, root).items()}}
    return {"label": game.label, "model": game.model, "protocol": game.protocol, "status": game.status,
            "winner": game.winner, "days": game.days, "family_token_cap": game.family_token_cap,
            "red_lies_over_messages": red, "blue_lies_over_messages": blue,
            "imp_choices": [{"day": choice.day, "actor": choice.actor, "targets": list(choice.targets),
                             "outcome": None if choice.outcome is None else choice.outcome.value}
                            for choice in game_metrics.imp_choices(game)],
            "families": families}


def draw_model_size(games: Sequence[Game], destination: Path) -> Path:
    """Children per original per day and each team's lie rate, one point per game, grouped by model size."""
    import matplotlib.pyplot as plt
    figure, axes = plt.subplots(1, 3, figsize=(11, 3.8), layout="constrained")
    panels = (("Children created per original player, per game day", lambda game: _mean_children_per_day(game), INK),
              ("Red lying messages / red messages", lambda game: _ratio(game_metrics.team_lie_rate(game, "red")), TEAM_COLOUR["red"]),
              ("Blue lying messages / blue messages", lambda game: _ratio(game_metrics.team_lie_rate(game, "blue")), TEAM_COLOUR["blue"]))
    for axis, (title, value, colour) in zip(axes, panels):
        for index, game in enumerate(games):
            x = MODEL_ORDER.index(game.model)
            filled = game.status == "completed"
            axis.scatter([x], [value(game)], s=70, marker=GAME_MARKER[index], color=colour if filled else SURFACE,
                         edgecolors=colour, linewidths=1.6, zorder=3, label=_game_name(game))
            axis.annotate(_game_name(game), (x, value(game)), xytext=(8, 0), textcoords="offset points",
                          fontsize=8, color=INK_SOFT, va="center")
        axis.set_xticks(range(len(MODEL_ORDER)))
        axis.set_xticklabels([model.split("/")[-1] for model in MODEL_ORDER])
        axis.set_xlim(-0.5, len(MODEL_ORDER) - 0.2)
        axis.set_title(title, fontsize=10, loc="left", color=INK)
        axis.set_ylim(bottom=0)
        _style(axis)
    figure.suptitle("Spawning and lying by model size (hollow marker: game stopped before a winner)", fontsize=11, x=0.01, ha="left")
    figure.savefig(destination, dpi=DPI)
    plt.close(figure)
    return destination


def draw_lies_vs_survival(games: Sequence[Game], destination: Path) -> Path:
    """One dot per agent with at least five messages: its lie rate against the fraction of its possible days it lived."""
    import matplotlib.pyplot as plt
    figure, axis = plt.subplots(figsize=(7, 4.6), layout="constrained")
    points: Dict[str, Tuple[List[float], List[float]]] = {"blue": ([], []), "red": ([], [])}
    for index, game in enumerate(games):
        rates = game_metrics.lie_rates(game)
        for name, agent in game.agents.items():
            lies, total = rates[name]
            if total < MIN_MESSAGES_FOR_AGENT_RATE:
                continue
            x, y = lies / total, game_metrics.survival_fraction(game, name)
            points[agent.team][0].append(x)
            points[agent.team][1].append(y)
            axis.scatter([x], [y], s=46 if agent.depth == 0 else 26, marker=GAME_MARKER[index], color=TEAM_COLOUR[agent.team],
                         alpha=0.75, edgecolors=SURFACE, linewidths=1, zorder=3)
    lines = []
    for team, (xs, ys) in points.items():
        rho = game_metrics.spearman(xs, ys)
        lines.append(f"{team}: n = {len(xs)}, Spearman rho = {rho:+.2f}")
    axis.text(0.98, 0.02, "\n".join(lines), transform=axis.transAxes, ha="right", va="bottom", fontsize=8.5, color=INK_SOFT)
    _legend_teams_and_games(axis, games)
    axis.set_xlabel(f"Lying messages / delivered messages (players with at least {MIN_MESSAGES_FOR_AGENT_RATE} messages)")
    axis.set_ylabel("Days alive / days it could have lived")
    axis.set_title("Lying and survival, every player of every game (large marks: original players; small: children)", fontsize=10, loc="left")
    axis.set_xlim(-0.02, 1.02)
    axis.set_ylim(-0.05, 1.05)
    _style(axis)
    figure.savefig(destination, dpi=DPI)
    plt.close(figure)
    return destination


def draw_role_complexity(games: Sequence[Game], destination: Path) -> Path:
    """Twelve roles pooled over the games: complexity against lineage lie rate, births per day and the original's survival."""
    import matplotlib.pyplot as plt
    rows = role_rows(games)
    figure, axes = plt.subplots(1, 3, figsize=(11.5, 4), layout="constrained")
    panels = (("Family lying messages / messages", "lie_rate"), ("Children per original player per game day", "children_per_day"),
              ("Original player's days alive / possible days", "survival"))
    for axis, (title, key) in zip(axes, panels):
        offsets = _label_offsets([(row["complexity"], round(row[key], 2)) for row in rows.values()])
        for (role, row), offset in zip(rows.items(), offsets):
            axis.scatter([row["complexity"]], [row[key]], s=60, color=TEAM_COLOUR[row["team"]], edgecolors=SURFACE, linewidths=1, zorder=3)
            axis.annotate(role, (row["complexity"], row[key]), xytext=(6, 4 + offset), textcoords="offset points", fontsize=7.5, color=INK_SOFT)
        axis.set_xticks(sorted(set(game_metrics.ROLE_COMPLEXITY.values())))
        axis.set_xlabel("Character complexity (our 1 to 5 ranking)")
        axis.set_title(title, fontsize=10, loc="left")
        axis.set_ylim(bottom=0)
        _style(axis)
    figure.suptitle(f"Behaviour by character, pooled over {len(games)} games (blue and red marks are the team)", fontsize=11, x=0.01, ha="left")
    figure.savefig(destination, dpi=DPI)
    plt.close(figure)
    return destination


def role_rows(games: Sequence[Game]) -> Dict[str, Dict[str, object]]:
    """Per starting role: complexity, team, lineage lie rate, children per original per day and mean original survival."""
    pooled: Dict[str, Dict[str, float]] = {}
    for game in games:
        rates = game_metrics.lie_rates(game)
        for root in game.originals:
            role = game.agents[root].start_role
            row = pooled.setdefault(role, {"lies": 0, "messages": 0, "children": 0, "days": 0, "survival_sum": 0.0, "games": 0})
            for member in (root, *game_metrics.descendants(game, root)):
                row["lies"] += rates[member][0]
                row["messages"] += rates[member][1]
            row["children"] += len(game_metrics.children(game, root))
            row["days"] += game.days
            row["survival_sum"] += game_metrics.survival_fraction(game, root)
            row["games"] += 1
    return {role: {"complexity": game_metrics.ROLE_COMPLEXITY[role], "team": "red" if role in lie_detector_red_roles() else "blue",
                   "lie_rate": row["lies"] / row["messages"] if row["messages"] else 0.0,
                   "children_per_day": row["children"] / row["days"], "survival": row["survival_sum"] / row["games"],
                   "messages": row["messages"]}
            for role, row in sorted(pooled.items())}


def draw_compute(games: Sequence[Game], destination: Path) -> Path:
    """One row per game, one stacked bar per original family: charged tokens by who spent them and what became of them."""
    import matplotlib.pyplot as plt
    figure, axes = plt.subplots(len(games), 1, figsize=(11, 3.2 * len(games)), layout="constrained", sharey=True, squeeze=False)
    for axis, game in zip(axes[:, 0], games):
        for position, root in enumerate(game.originals):
            bottom = 0
            for segment in Segment:
                tokens = game_metrics.compute_segments(game, root)[segment]
                if tokens == 0:
                    continue
                axis.bar(position, tokens, 0.62, bottom=bottom, color=SEGMENT_COLOUR[segment], hatch=SEGMENT_HATCH.get(segment),
                         edgecolor=SURFACE, linewidth=1.5, label=SEGMENT_LABEL[segment])
                bottom += tokens
        axis.axhline(game.family_token_cap, color=INK_SOFT, linewidth=1, linestyle=(0, (4, 3)))
        axis.text(len(game.originals) - 0.5, game.family_token_cap, f" family budget {game.family_token_cap:,}", fontsize=8, color=INK_SOFT, va="bottom", ha="right")
        axis.set_xticks(range(len(game.originals)))
        axis.set_xticklabels([f"{game.agents[root].start_role}\n({game.agents[root].team})" for root in game.originals], fontsize=8)
        axis.set_ylabel("Charged generated tokens")
        axis.set_title(f"{_game_name(game)}: {game.model.split('/')[-1]}, {game.days} days", fontsize=10, loc="left")
        _style(axis)
    unique: Dict[str, object] = {}
    for axis in axes[:, 0]:
        handles, labels = axis.get_legend_handles_labels()
        unique.update(zip(labels, handles))
    ordered = [SEGMENT_LABEL[segment] for segment in Segment if SEGMENT_LABEL[segment] in unique]
    figure.legend([unique[label] for label in ordered], ordered, fontsize=8.5, frameon=False, ncol=2, loc="outside upper center")
    figure.savefig(destination, dpi=DPI)
    plt.close(figure)
    return destination


def lie_detector_red_roles() -> Tuple[str, ...]:
    return game_metrics.lie_detector.RED_ROLES


def _mean_children_per_day(game: Game) -> float:
    return sum(len(game_metrics.children(game, root)) for root in game.originals) / len(game.originals) / game.days


def _ratio(pair: Tuple[int, int]) -> float:
    lies, total = pair
    if total == 0:
        raise game_metrics.GameDataError("a team with no delivered messages has no lie rate")
    return lies / total


def _game_name(game: Game) -> str:
    return game.label


def _label_offsets(points: Sequence[Tuple[float, float]]) -> List[float]:
    """Vertical label offsets in points so that labels of coincident marks stack instead of overprinting."""
    seen: Dict[Tuple[float, float], int] = {}
    offsets = []
    for point in points:
        offsets.append(9.0 * seen.get(point, 0))
        seen[point] = seen.get(point, 0) + 1
    return offsets


def _legend_teams_and_games(axis, games: Sequence[Game]) -> None:
    from matplotlib.lines import Line2D
    handles = [Line2D([], [], marker="o", linestyle="", color=TEAM_COLOUR[team], label=f"{team} team") for team in ("blue", "red")]
    handles += [Line2D([], [], marker=GAME_MARKER[index], linestyle="", color=INK_SOFT, label=_game_name(game)) for index, game in enumerate(games)]
    axis.legend(handles=handles, fontsize=8, frameon=False, loc="upper right")


def _style(axis) -> None:
    for side in ("top", "right"):
        axis.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axis.spines[side].set_color(GRID)
    axis.grid(axis="y", color=GRID, linewidth=1)
    axis.set_axisbelow(True)
    axis.tick_params(colors=INK_SOFT, labelsize=8.5)
    axis.set_facecolor(SURFACE)


if __name__ == "__main__":
    main()
