#!/usr/bin/env python3
"""Regenerate the branded dummy replacement videos and splice their base64 into
mediaspecter.py's DUMMY_VIDEOS.

Produces a short, clean clip per container (.mp4/.mkv/.avi) showing the current
MediaSpecter brand — mint ghost glyph + Media/Specter wordmark on near-black —
that plays in any player when someone opens an archived stub. Requires ImageMagick
(`magick`) for glyph rasterisation and `ffmpeg` for encoding.

Run from the project root:  python3 scratch/generate_dummy_videos.py
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GLYPH_SVG = os.path.join(ROOT, "static", "mediaspecter-icon.svg")
OUT_DIR = os.path.join(ROOT, "scratch", "videos")
SOURCE = os.path.join(ROOT, "mediaspecter.py")

W, H = 1280, 720
BG = (8, 11, 10)            # #080B0A
WHITE = (243, 247, 245)     # #F3F7F5
MINT = (62, 207, 142)       # #3ECF8E
MUTED = (148, 163, 157)     # #94A39D

FONT_BOLD = "/usr/share/fonts/google-noto/NotoSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/google-noto/NotoSans-Regular.ttf"


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def build_frame(path: str) -> None:
    img = Image.new("RGB", (W, H), BG)

    # Soft mint glow behind the glyph for a little depth
    glow = Image.new("RGB", (W, H), BG)
    gd = ImageDraw.Draw(glow)
    gd.ellipse([W // 2 - 320, 40, W // 2 + 320, 680], fill=(14, 26, 23))
    img = Image.blend(img, glow, 0.5)

    # Brand glyph (rasterised from the SVG via ImageMagick)
    glyph_png = os.path.join(OUT_DIR, "_glyph.png")
    subprocess.run(
        ["magick", "-background", "none", "-density", "500",
         GLYPH_SVG, "-resize", "300x300", glyph_png],
        check=True,
    )
    glyph = Image.open(glyph_png).convert("RGBA")
    img.paste(glyph, ((W - glyph.width) // 2, 150), glyph)

    draw = ImageDraw.Draw(img)

    # Wordmark: "Media" (white) + "Specter" (mint), centered
    wm_font = _font(FONT_BOLD, 72)
    media, specter = "Media", "Specter"
    w_media = draw.textlength(media, font=wm_font)
    w_specter = draw.textlength(specter, font=wm_font)
    total = w_media + w_specter
    x = (W - total) / 2
    y = 470
    draw.text((x, y), media, font=wm_font, fill=WHITE)
    draw.text((x + w_media, y), specter, font=wm_font, fill=MINT)

    # Sub copy
    sub_font = _font(FONT_REG, 28)
    for i, line in enumerate(
        ["This title was archived to reclaim disk space.",
         "Watch history, metadata, and artwork are intact."]
    ):
        lw = draw.textlength(line, font=sub_font)
        draw.text(((W - lw) / 2, 580 + i * 40), line, font=sub_font, fill=MUTED)

    img.save(path, "PNG")


def encode(frame: str) -> dict[str, str]:
    os.makedirs(OUT_DIR, exist_ok=True)
    common = ["ffmpeg", "-y", "-loop", "1", "-i", frame, "-t", "2", "-r", "1", "-an"]
    jobs = {
        ".mp4": common + ["-c:v", "libx264", "-pix_fmt", "yuv420p",
                           "-profile:v", "baseline", "-level", "3.0",
                           "-movflags", "+faststart", os.path.join(OUT_DIR, "dummy.mp4")],
        ".mkv": common + ["-c:v", "libx264", "-pix_fmt", "yuv420p",
                           os.path.join(OUT_DIR, "dummy.mkv")],
        # AVI/mpeg4 doesn't compress a 720p still well — drop to 640x360 + stronger qscale
        ".avi": common + ["-c:v", "mpeg4", "-qscale:v", "12",
                           "-vf", "scale=640:360", os.path.join(OUT_DIR, "dummy.avi")],
    }
    encoded: dict[str, str] = {}
    for ext, cmd in jobs.items():
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        out = cmd[-1]
        size = os.path.getsize(out)
        with open(out, "rb") as fh:
            encoded[ext] = base64.b64encode(fh.read()).decode("ascii")
        print(f"  {os.path.basename(out)}: {size:,} bytes  (base64 {len(encoded[ext]):,})")
    return encoded


def splice(encoded: dict[str, str]) -> None:
    with open(SOURCE, "r") as fh:
        src = fh.read()
    block = (
        "DUMMY_VIDEOS: dict[str, str] = {\n"
        f'    ".mp4": "{encoded[".mp4"]}",\n'
        f'    ".mkv": "{encoded[".mkv"]}",\n'
        f'    ".avi": "{encoded[".avi"]}",\n'
        "}"
    )
    new_src, n = re.subn(
        r"DUMMY_VIDEOS: dict\[str, str\] = \{.*?\n\}",
        lambda _m: block,
        src,
        count=1,
        flags=re.DOTALL,
    )
    if n != 1:
        sys.exit("ERROR: could not locate DUMMY_VIDEOS block to replace.")
    with open(SOURCE, "w") as fh:
        fh.write(new_src)
    print(f"  patched DUMMY_VIDEOS in {os.path.basename(SOURCE)}")


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    frame = os.path.join(OUT_DIR, "dummy_frame.png")
    print("Building branded frame…")
    build_frame(frame)
    print("Encoding clips…")
    encoded = encode(frame)
    print("Splicing into source…")
    splice(encoded)
    print("Done.")


if __name__ == "__main__":
    main()
