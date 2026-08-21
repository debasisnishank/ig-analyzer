#!/usr/bin/env python3
"""Generate PWA icons for the dashboard.

Build-time only — Pillow is NOT a runtime dependency. Re-run if the brand
changes:  ./venv/bin/pip install pillow && python scripts/make_icons.py

Draws the "night instrument cluster" mark: a rising telemetry line in the amber
backlight over the deep blue-black ground, matching base.html.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "static" / "icons"
OUT.mkdir(parents=True, exist_ok=True)

GROUND = (11, 14, 20)       # --ground  #0B0E14
SURFACE = (27, 34, 48)      # --raised  #1B2230
AMBER = (233, 169, 59)      # --data-lit #E9A93B
AMBER_DIM = (194, 128, 9)   # --data    #C28009
HAIRLINE = (46, 57, 73)     # --hairline-2


def _rounded(size: int, radius_frac: float, bg) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(size * radius_frac)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=bg)
    return img


def _draw_chart(img: Image.Image, inset: float) -> None:
    """A rising polyline with a lit end-dot, drawn in the safe zone."""
    size = img.size[0]
    d = ImageDraw.Draw(img)
    pad = int(size * inset)
    w = size - 2 * pad
    h = size - 2 * pad
    # Normalised points of a rising, slightly jagged telemetry line.
    pts_norm = [(0.0, 0.72), (0.22, 0.58), (0.42, 0.66), (0.62, 0.36), (0.82, 0.44), (1.0, 0.12)]
    pts = [(pad + x * w, pad + y * h) for x, y in pts_norm]

    # Baseline gridline for the instrument feel.
    base_y = pad + 0.86 * h
    d.line([(pad, base_y), (pad + w, base_y)], fill=HAIRLINE, width=max(2, size // 128))

    lw = max(3, size // 42)
    # Faint underglow, then the bright line.
    d.line(pts, fill=AMBER_DIM, width=lw + max(2, size // 96), joint="curve")
    d.line(pts, fill=AMBER, width=lw, joint="curve")

    # Lit end point (the "lamp").
    ex, ey = pts[-1]
    rr = max(4, size // 24)
    d.ellipse([ex - rr - lw, ey - rr - lw, ex + rr + lw, ey + rr + lw], fill=GROUND)
    d.ellipse([ex - rr, ey - rr, ex + rr, ey + rr], fill=AMBER)


def make(size: int, name: str, *, maskable: bool = False, apple: bool = False) -> None:
    # Maskable icons need content inside the ~80% safe circle → more inset and a
    # full-bleed background. Apple icons are square (no transparency) with a
    # gentle radius so iOS can apply its own mask.
    radius = 0.0 if apple else (0.0 if maskable else 0.22)
    bg = GROUND
    img = _rounded(size, radius, bg) if not maskable and not apple else Image.new(
        "RGBA", (size, size), (*GROUND, 255)
    )
    if not maskable and not apple:
        # Subtle inner panel for depth on the "any" icon.
        d = ImageDraw.Draw(img)
        m = int(size * 0.08)
        d.rounded_rectangle(
            [m, m, size - 1 - m, size - 1 - m],
            radius=int(size * 0.16),
            outline=HAIRLINE,
            width=max(1, size // 128),
        )
    _draw_chart(img, inset=0.26 if maskable else 0.2)
    out = OUT / name
    img.convert("RGBA" if not apple else "RGB").save(out)
    print(f"  wrote {out.relative_to(OUT.parent.parent)}  ({size}x{size})")


if __name__ == "__main__":
    print("Generating PWA icons...")
    make(192, "icon-192.png")
    make(512, "icon-512.png")
    make(512, "icon-maskable-512.png", maskable=True)
    make(180, "apple-touch-icon.png", apple=True)
    make(32, "favicon-32.png")
    print("Done.")
