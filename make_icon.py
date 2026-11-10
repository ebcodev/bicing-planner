#!/usr/bin/env python3
"""
make_icon.py - draws the app icon (a white bicycle on a blue circle) into assets/.

    uv run python make_icon.py

Writes assets/bicing_planner.ico (16-256 px, used by the .exe and the window) and a PNG preview.
It is a generic bike drawing, not Bicing's logo.
"""

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024                      # drawn large, then scaled down smoothly
BLUE = (31, 106, 165, 255)       # same blue as the app's buttons
WHITE = (255, 255, 255, 255)
OUT = Path(__file__).resolve().parent / "assets"


def draw() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((24, 24, SIZE - 24, SIZE - 24), fill=BLUE)

    s = SIZE / 256                                   # design on a 256 grid
    P = lambda x, y: (x * s, y * s)                  # noqa: E731
    w = int(11 * s)                                  # line width
    rear, front = (78, 162), (178, 162)              # wheel hubs
    crank, seat, head = (120, 162), (104, 104), (166, 104)
    r = 38                                           # wheel radius
    for cx, cy in (rear, front):
        d.ellipse((*P(cx - r, cy - r), *P(cx + r, cy + r)), outline=WHITE, width=w)
    for a, b in ((rear, crank), (crank, seat), (seat, rear), (seat, head), (head, crank), (head, front)):
        d.line((*P(*a), *P(*b)), fill=WHITE, width=w, joint="curve")
    d.line((*P(92, 92), *P(118, 92)), fill=WHITE, width=int(13 * s))     # saddle
    d.line((*P(162, 86), *P(186, 82)), fill=WHITE, width=int(11 * s))    # handlebar
    d.line((*P(166, 104), *P(170, 86)), fill=WHITE, width=w)             # stem
    for x, y in (rear, front, crank):
        d.ellipse((*P(x - 7, y - 7), *P(x + 7, y + 7)), fill=WHITE)      # hubs and crank
    return img


def main():
    OUT.mkdir(exist_ok=True)
    img = draw()
    img.resize((256, 256), Image.LANCZOS).save(OUT / "bicing_planner.png")
    img.save(OUT / "bicing_planner.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", OUT / "bicing_planner.ico")


if __name__ == "__main__":
    main()
