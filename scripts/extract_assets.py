"""Cut the site's reference illustrations into the transparent layers the animated pages need, without redrawing them.

Usage: /usr/bin/python3 scripts/extract_assets.py
Reads docs/assets/source/{hands.webp,gate.png,title.webp} and writes docs/assets/{hands-foreground.png,
gate-left.png,gate-right.png,title.png} plus docs/assets/preview/*.png for checking the masks by eye.

Hands: a pixel belongs to the hands when its chroma (max - min of RGB after a 5 px median) is at least
HAND_CHROMA, or when it is dark and within OUTLINE_REACH px of such a pixel (the inked outlines); enclosed
holes (rings, gems, line gaps) are filled by flood-filling the background from the image border.
Gate and title: black ink on white, so alpha = 255 - luminance, scaled so the ink body is fully opaque.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "assets" / "source"
OUTPUT = ROOT / "docs" / "assets"
PREVIEW = OUTPUT / "preview"
HAND_CHROMA = 30          # pink skin measures 40-57, gold 117; the checkerboard stays under 15 after the median
OPENING_PX = 2            # colour fringes along checkerboard edges are under 5 px thick; fingers and drips are wider
PINK_RED_OVER_GREEN = 35  # skin has R - G of 43-59; the fringes 14-32
PINK_CHROMA = 40
OUTLINE_REACH_PX = 2      # dark pixels this close to the coloured body are the drawn outline, not the checkerboard
OUTLINE_MAX_LUMINANCE = 110
MEDIAN_PX = 5
FEATHER_PX = 0.9
INK_GAIN = 1.25           # lifts the gate's grey anti-aliased ink to full opacity without widening it
DIAMOND_WIDTH_PX = 292    # horizontal diagonal of one checkerboard diamond in the 736 px hands image (measured)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    PREVIEW.mkdir(parents=True, exist_ok=True)
    hands = Image.open(SOURCE / "hands.webp").convert("RGB")
    alpha = hands_alpha(hands)
    write_rgba(hands, alpha, OUTPUT / "hands-foreground.png")
    preview_over(hands, alpha, PREVIEW / "hands-on-grey.png", (120, 120, 120))
    Image.fromarray(alpha).save(PREVIEW / "hands-mask.png")
    print("hands: foreground pixels", int((alpha > 127).sum()), "of", alpha.size)
    print("checkerboard rows:", checkerboard_transitions(hands))
    print("checkerboard colours:", checkerboard_colours(hands, alpha))
    gate = Image.open(SOURCE / "gate.png").convert("RGB")
    ink = ink_alpha(gate)
    half = gate.width // 2
    write_ink(ink[:, :half], OUTPUT / "gate-left.png")
    write_ink(ink[:, half:], OUTPUT / "gate-right.png")
    print("gate: posts at x =", post_columns(ink), "of width", gate.width, "split at", half)
    title = Image.open(SOURCE / "title.webp")
    if title.mode != "RGBA":
        raise RuntimeError(f"title.webp must carry its own alpha channel, got mode {title.mode}")
    title_alpha = np.asarray(title.split()[-1])
    box = ink_bounding_box(title_alpha, margin_px=24)
    write_ink(title_alpha[box[1]:box[3], box[0]:box[2]], OUTPUT / "title.png")
    print("title: cropped to", box, "from", title.size)
    print("black diamond centres (image px):", black_diamond_centres(hands, alpha))


def hands_alpha(image: Image.Image) -> np.ndarray:
    """Coloured body, opened to drop the thin colour fringes along checkerboard edges, plus strongly pink thin parts."""
    smooth = np.asarray(image.filter(ImageFilter.MedianFilter(MEDIAN_PX))).astype(np.int16)
    chroma = smooth.max(axis=2) - smooth.min(axis=2)
    luminance = smooth.mean(axis=2)
    red_over_green = smooth[..., 0] - smooth[..., 1]
    body = _opened(chroma >= HAND_CHROMA, OPENING_PX)
    pink = (red_over_green >= PINK_RED_OVER_GREEN) & (chroma >= PINK_CHROMA)
    core = body | pink
    near_core = _grown(core, OUTLINE_REACH_PX)
    outline = near_core & (luminance < OUTLINE_MAX_LUMINANCE)
    mask = fill_holes(core | outline)
    feathered = Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.GaussianBlur(FEATHER_PX))
    return np.asarray(feathered)


def _grown(mask: np.ndarray, radius_px: int) -> np.ndarray:
    return np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(2 * radius_px + 1))) > 0


def _opened(mask: np.ndarray, radius_px: int) -> np.ndarray:
    """Erode then dilate: structures thinner than 2 * radius_px + 1 disappear, everything else keeps its outline."""
    eroded = np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.MinFilter(2 * radius_px + 1))) > 0
    return _grown(eroded, radius_px)


def checkerboard_colours(image: Image.Image, alpha: np.ndarray) -> dict:
    """Mean colour of the dark and the light squares outside the hands, for the CSS pattern."""
    pixels = np.asarray(image).astype(np.int16)
    luminance = pixels.mean(axis=2)
    outside = alpha < 8
    dark = outside & (luminance < 60)
    light = outside & (luminance > 150)
    return {"dark": tuple(pixels[dark].mean(axis=0).round().astype(int).tolist()),
            "light": tuple(pixels[light].mean(axis=0).round().astype(int).tolist())}


def fill_holes(mask: np.ndarray) -> np.ndarray:
    """Everything the border cannot reach through background pixels is inside the hands."""
    # fromarray shares the array's read-only buffer and floodfill then writes nothing; copy() makes it writable.
    canvas = Image.fromarray(np.where(mask, 255, 0).astype(np.uint8)).copy()
    width, height = canvas.size
    seeds = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1), (0, height // 2), (width - 1, height // 2),
             (width // 2, height - 1)]
    for seed in seeds:
        if canvas.getpixel(seed) == 0:
            ImageDraw.floodfill(canvas, seed, 128)
    filled = np.asarray(canvas)
    if not (filled == 128).any():
        raise RuntimeError("flood fill reached no background pixel; the mask would cover the whole frame")
    return filled != 128


def ink_alpha(image: Image.Image) -> np.ndarray:
    luminance = np.asarray(image).astype(np.float32).mean(axis=2)
    return np.clip((255 - luminance) * INK_GAIN, 0, 255).astype(np.uint8)


def write_rgba(image: Image.Image, alpha: np.ndarray, destination: Path) -> None:
    rgba = np.dstack([np.asarray(image), alpha])
    Image.fromarray(rgba).save(destination, optimize=True)


def write_ink(alpha: np.ndarray, destination: Path) -> None:
    black = np.zeros(alpha.shape + (3,), dtype=np.uint8)
    Image.fromarray(np.dstack([black, alpha])).save(destination, optimize=True)


def black_diamond_centres(image: Image.Image, alpha: np.ndarray) -> list:
    """Centres of black diamonds cut by the bottom rows: width w at a row means the centre is (D - w) / 2 rows away."""
    luminance = np.asarray(image.convert("L")).astype(np.int16)
    centres = []
    for row in (735, 720):
        dark = (luminance[row] < 90) & (alpha[row] < 128)
        edges = np.flatnonzero(dark[1:] != dark[:-1])
        for start, end in zip(edges[:-1], edges[1:]):
            if dark[start + 1]:
                width = end - start
                centres.append((row, round((start + end) / 2, 1), width, round(row - (DIAMOND_WIDTH_PX - width) / 2, 1)))
    return centres


def preview_over(image: Image.Image, alpha: np.ndarray, destination: Path, colour: tuple) -> None:
    background = Image.new("RGB", image.size, colour)
    background.paste(image, mask=Image.fromarray(alpha))
    background.save(destination)


def checkerboard_transitions(image: Image.Image) -> dict:
    """Black-to-light crossings along rows free of hands, to read the diamond period and phase."""
    luminance = np.asarray(image.convert("L")).astype(np.int16)
    found = {}
    for row in (700, 720, 735):
        dark = luminance[row] < 90
        crossings = np.flatnonzero(dark[1:] != dark[:-1]).tolist()
        found[row] = crossings
    return found


def post_columns(ink: np.ndarray) -> list:
    """Columns whose lower half is almost solid ink: the vertical posts of the gate."""
    lower = ink[ink.shape[0] // 2:, :] > 200
    solid = np.flatnonzero(lower.mean(axis=0) > 0.6).tolist()
    groups, start = [], None
    for index, column in enumerate(solid):
        if start is None:
            start = column
        if index + 1 == len(solid) or solid[index + 1] != column + 1:
            groups.append((start + column) // 2)
            start = None
    return groups


def ink_bounding_box(alpha: np.ndarray, *, margin_px: int) -> tuple:
    rows = np.flatnonzero(alpha.max(axis=1) > 40)
    columns = np.flatnonzero(alpha.max(axis=0) > 40)
    return (max(columns[0] - margin_px, 0), max(rows[0] - margin_px, 0),
            min(columns[-1] + margin_px, alpha.shape[1]), min(rows[-1] + margin_px, alpha.shape[0]))


if __name__ == "__main__":
    main()
