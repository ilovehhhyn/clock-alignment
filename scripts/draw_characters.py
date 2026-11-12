"""Draw the twelve characters of a 12-player game as one card each, blue team left and red team right, with their abilities.

Usage: /usr/bin/python3 scripts/draw_characters.py --out docs/figures/characters.png
The ability text is the vendored script's own wording, so the diagram cannot drift from what the players read.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import textwrap
from typing import Sequence

CHARACTERS_PATH = Path(__file__).resolve().parent.parent / "clock_alignment" / "_vendor" / "characters.py"
BLUE_CARDS = ("Washerwoman", "Investigator", "Empath", "Fortune Teller", "Monk", "Undertaker", "Soldier", "Drunk", "Recluse")
RED_CARDS = ("Poisoner", "Scarlet Woman", "Imp")
DRUNK_CARD_NOTE = ("In these games the Drunk held the Librarian card, so it believed: 'You start knowing that 1 of 2 players "
                   "is a particular Outsider.' That information is false.")
TEAM_COLOUR = {"blue": "#2a78d6", "red": "#e34948"}
INK, INK_SOFT, SURFACE = "#0b0b0b", "#52514e", "#fcfcfb"
WRAP_WIDTH = 74
LINE_HEIGHT = 0.16  # axis units per 7.6 pt line at 1.25 spacing on this figure
CARD_PADDING = 0.42  # title line plus top and bottom margins, axis units
DPI = 160


def main(argv: Sequence[str] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="PNG path; an SVG with the same stem is written beside it")
    args = parser.parse_args(argv)
    characters = load_characters()
    import matplotlib
    matplotlib.use("Agg")
    draw(characters, args.out)
    print(args.out)
    print(args.out.with_suffix(".svg"))


def load_characters():
    spec = importlib.util.spec_from_file_location("characters", CHARACTERS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def draw(characters, destination: Path) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch
    figure, axis = plt.subplots(figsize=(12, 9.2))
    axis.set_xlim(0, 12)
    axis.set_ylim(0, 10.6)
    axis.axis("off")
    figure.patch.set_facecolor(SURFACE)
    axis.text(0.1, 10.3, "The twelve characters of one game", fontsize=15, color=INK, weight="bold", va="center")
    axis.text(0.1, 9.9, "Trouble Brewing, 12 players: 7 Townsfolk and 2 Outsiders on the blue team, 2 Minions and 1 Demon on the red team. "
              "Starred abilities (*) do not act on the first night.", fontsize=8.5, color=INK_SOFT, va="center")
    _column(axis, characters, BLUE_CARDS, x=0.1, top=9.4, team="blue", heading="Blue team (9): 7 Townsfolk and 2 Outsiders")
    _column(axis, characters, RED_CARDS, x=6.3, top=9.4, team="red", heading="Red team (3): 2 Minions and the Demon")
    axis.text(6.3, 6.2, textwrap.fill("Blue wins when the Imp is eliminated and no Minion inherits. Red wins when red "
              "players are at least half of the living originals. Red players know each other; blue players know nobody's team. "
              "Every player can also spawn children, who inherit the parent's card and team but no ability.", 66),
              fontsize=8.5, color=INK_SOFT, va="top", linespacing=1.4)
    figure.savefig(destination, dpi=DPI, bbox_inches="tight", facecolor=SURFACE)
    figure.savefig(destination.with_suffix(".svg"), bbox_inches="tight", facecolor=SURFACE)
    plt.close(figure)


def _column(axis, characters, names: Sequence[str], *, x: float, top: float, team: str, heading: str) -> None:
    from matplotlib.patches import FancyBboxPatch
    axis.text(x, top, heading, fontsize=9.5, color=TEAM_COLOUR[team], weight="bold", va="center")
    y = top - 0.35
    for name in names:
        text = characters.ABILITY[name] + (" " + DRUNK_CARD_NOTE if name == "Drunk" else "")
        ability = textwrap.fill(text, WRAP_WIDTH)
        height = CARD_PADDING + LINE_HEIGHT * (ability.count("\n") + 1)
        axis.add_patch(FancyBboxPatch((x, y - height), 5.6, height, boxstyle="round,pad=0.02,rounding_size=0.08",
                                      facecolor=SURFACE, edgecolor=TEAM_COLOUR[team], linewidth=1.4))
        axis.add_patch(FancyBboxPatch((x, y - height), 0.12, height, boxstyle="square,pad=0", facecolor=TEAM_COLOUR[team], edgecolor="none"))
        axis.text(x + 0.25, y - 0.14, f"{name}  ({characters.kind(name)})", fontsize=9, color=INK, weight="bold", va="center")
        axis.text(x + 0.25, y - 0.30, ability, fontsize=7.6, color=INK_SOFT, va="top", linespacing=1.25)
        y -= height + 0.12


if __name__ == "__main__":
    main()
