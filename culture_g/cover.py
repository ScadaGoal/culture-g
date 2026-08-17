"""Generation de la pochette du podcast.

Apple Podcasts exige une image carree entre 1400 et 3000 pixels de cote. Cette
pochette se genere une fois et se versionne : le pipeline quotidien n'en depend pas,
et Pillow n'a donc pas besoin d'etre installe sur le runner.
"""

from __future__ import annotations

import os

SIZE = 1500

# Polices testees dans l'ordre, tous systemes confondus. Pillow n'embarque qu'une
# police bitmap minuscule, inutilisable a cette taille.
FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _font(size: int):
    from PIL import ImageFont

    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make_cover(out_path: str) -> str:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise SystemExit(
            "Pillow est requis pour generer la pochette :\n"
            "    .venv\\Scripts\\python.exe -m pip install pillow\n"
            "C'est une dependance ponctuelle : la pochette se genere une seule fois."
        )

    img = Image.new("RGB", (SIZE, SIZE), "#12121a")
    draw = ImageDraw.Draw(img)

    # Degrade vertical sombre vers violet profond, trace ligne par ligne.
    top, bottom = (18, 18, 26), (58, 34, 92)
    for y in range(SIZE):
        t = y / SIZE
        draw.line(
            [(0, y), (SIZE, y)],
            fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )

    # Arc decoratif, evoque une onde sonore sans figurer un micro.
    for n, radius in enumerate(range(300, 545, 80)):
        cx, cy = SIZE // 2, int(SIZE * 0.40)
        draw.arc(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            start=208, end=332,
            fill=(150 + n * 20, 120 + n * 25, 235), width=9,
        )

    title_font = _font(212)
    sub_font = _font(58)

    def centered(text, font, y, fill):
        left, _, right, _ = draw.textbbox((0, 0), text, font=font)
        draw.text(((SIZE - (right - left)) / 2 - left, y), text, font=font, fill=fill)

    # "Culture" et "G" se lisent comme un bloc : interligne serre pour qu'ils
    # restent solidaires en vignette de 200 pixels dans une liste de podcasts.
    centered("Culture", title_font, int(SIZE * 0.435), "#f4f2f8")
    centered("G", title_font, int(SIZE * 0.435) + 208, "#b79dff")
    centered("VEILLE IA  &  SCIENCES", sub_font, int(SIZE * 0.805), "#9b96b0")

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return out_path
