#!/usr/bin/env python3
"""Generate icon-512.png — brand system §6: eclipse motif on the navy
field (#0d1b2a), one per-product glyph. Pantry's glyph is a vending
shelf wall: three stocked shelves of selections beside the eclipse
ring — the fleet's goods, read at a glance. PIL only; deterministic."""
from PIL import Image, ImageDraw

S = 512
NAVY = (13, 27, 42, 255)
STOPS = [(15, 246, 247), (0, 212, 255), (123, 44, 191)]  # cyan->blue->purple


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)) + (255,)


def grad(t):
    return lerp(STOPS[0], STOPS[1], t * 2) if t < 0.5 else \
        lerp(STOPS[1], STOPS[2], (t - 0.5) * 2)


def main():
    img = Image.new("RGBA", (S, S), NAVY)
    d = ImageDraw.Draw(img)
    # Eclipse ring: gradient arc segments, lower-left (house motif).
    box = [64, 268, 264, 468]
    for i in range(72):
        t = i / 72
        d.arc(box, start=i * 5, end=i * 5 + 6, fill=grad(t), width=15)
    d.ellipse([116, 320, 212, 416], fill=NAVY)  # occluding body
    # The shelf wall: three shelves, stocked selections as bold blocks.
    x0, x1 = 238, 452
    shelves = [120, 218, 316]
    widths = [(52, 34, 44, 58), (38, 58, 30, 48), (46, 40, 54, 36)]
    for row, y in enumerate(shelves):
        d.line([(x0, y + 62), (x1, y + 62)], fill=grad(0.15 + row * 0.3),
               width=7)
        x = x0 + 8
        for j, w in enumerate(widths[row]):
            d.rounded_rectangle([x, y + 62 - 52, x + w, y + 62 - 4],
                                radius=5,
                                fill=grad((row * 4 + j) / 11.0))
            x += w + 12
    img.save("icon-512.png")
    print("wrote icon-512.png", img.size, img.mode)


if __name__ == "__main__":
    main()
