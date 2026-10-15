# -*- coding: utf-8 -*-
"""Background images for the defense deck (dark style of the reference talk the user shared, 10-15).
Writes assets/bg_content.png (content slides: black header band, grey band, dark bottom),
assets/wafer.png (bonded-wafer die array for cover, section dividers and the closing slide) and
assets/circuit.png (chip with traces for the research-question cards). All drawn here; no external art.
Usage: python3 make_images.py"""
import math, random
from PIL import Image, ImageDraw, ImageFilter

W, H = 1920, 1080
OUT = 'assets'


def lerp(a, b, t):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def hexrgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def vgrad(stops, w=W, h=H):
    """stops: list of (y_fraction, 'RRGGBB')"""
    im = Image.new('RGB', (w, h))
    px = im.load()
    cols = [(f, hexrgb(c)) for f, c in stops]
    for y in range(h):
        f = y / (h - 1)
        for k in range(len(cols) - 1):
            if cols[k][0] <= f <= cols[k + 1][0]:
                t = (f - cols[k][0]) / (cols[k + 1][0] - cols[k][0] or 1)
                c = lerp(cols[k][1], cols[k + 1][1], t)
                break
        for x in range(w):
            px[x, y] = c
    return im


def content_bg():
    # header band (0-10 % = the 1.9 cm header) almost black, a soft grey band below it, dark bottom
    im = vgrad([(0, '0A0A0B'), (0.098, '0D0D0E'), (0.102, '1B1C1E'), (0.30, '3A3B3F'), (0.55, '2A2B2E'), (1.0, '0E0E10')])
    # slight horizontal vignette
    v = Image.new('L', (W, H), 0)
    d = ImageDraw.Draw(v)
    d.ellipse((-W * 0.25, -H * 0.2, W * 1.25, H * 1.15), fill=255)
    v = v.filter(ImageFilter.GaussianBlur(160))
    dark = Image.new('RGB', (W, H), (8, 8, 9))
    im = Image.composite(im, dark, v)
    # keep the header band untouched by the vignette
    hb = vgrad([(0, '0A0A0B'), (1, '0D0D0E')], W, int(H * 0.1))
    im.paste(hb, (0, 0))
    im.save(f'{OUT}/bg_content.png')


def wafer():
    """die array on a wafer, purple-blue with copper pads, glowing toward the lower left"""
    random.seed(7)
    im = Image.new('RGB', (W, H), (6, 6, 9))
    layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy, R = -150, 1250, 1500           # wafer centre off-canvas (lower left)
    dw, dh, g = 150, 104, 12
    for i in range(-2, W // (dw + g) + 3):
        for j in range(-2, H // (dh + g) + 3):
            x0 = i * (dw + g) + 40; y0 = j * (dh + g) - 30
            mx, my = x0 + dw / 2, y0 + dh / 2
            r = math.hypot(mx - cx, my - cy)
            if r > R - 40:
                continue
            t = min(1.0, max(0.0, r / R))
            base = lerp((122, 64, 214), (40, 96, 214), t)          # violet -> blue
            a = int(235 - 150 * t)
            d.rounded_rectangle((x0, y0, x0 + dw, y0 + dh), radius=8, fill=base + (a,))
            # inner pad array
            for pi in range(5):
                for pj in range(3):
                    px0 = x0 + 16 + pi * 25; py0 = y0 + 18 + pj * 26
                    on = random.random() < 0.85
                    col = (236, 140, 72) if on else (160, 170, 210)
                    d.rectangle((px0, py0, px0 + 13, py0 + 11), fill=col + (int(a * (0.85 if on else 0.4)),))
            # scribe highlight
            d.line((x0, y0, x0 + dw, y0), fill=(220, 210, 255, int(a * 0.35)), width=2)
    glow = layer.filter(ImageFilter.GaussianBlur(18))
    im = Image.alpha_composite(im.convert('RGBA'), glow)
    im = Image.alpha_composite(im, layer)
    # wafer edge ring
    ring = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((cx - R, cy - R, cx + R, cy + R), outline=(190, 170, 255, 120), width=4)
    im = Image.alpha_composite(im, ring)
    im.convert('RGB').save(f'{OUT}/wafer.png')


def circuit():
    """a chip with Manhattan traces, cool grey-teal on near black"""
    random.seed(11)
    im = Image.new('RGB', (W, H), (8, 9, 11))
    lay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    chip = (300, 300, 640, 640)
    for _ in range(260):
        side = random.choice('LRTB')
        if side == 'R':
            x, y = chip[2], random.randint(chip[1] + 10, chip[3] - 10); dx, dy = 1, 0
        elif side == 'L':
            x, y = chip[0], random.randint(chip[1] + 10, chip[3] - 10); dx, dy = -1, 0
        elif side == 'T':
            x, y = random.randint(chip[0] + 10, chip[2] - 10), chip[1]; dx, dy = 0, -1
        else:
            x, y = random.randint(chip[0] + 10, chip[2] - 10), chip[3]; dx, dy = 0, 1
        pts = [(x, y)]
        for _ in range(random.randint(2, 5)):
            ln = random.randint(40, 420)
            x += dx * ln; y += dy * ln
            pts.append((x, y))
            if random.random() < 0.6:
                dx, dy = (dy, dx) if random.random() < 0.5 else (dx, dy)
                if dx == 0 and dy == 0:
                    dx = 1
        a = random.randint(50, 150)
        col = random.choice([(120, 150, 170), (90, 190, 190), (150, 160, 175)])
        d.line(pts, fill=col + (a,), width=random.choice([2, 2, 3]))
        ex, ey = pts[-1]
        d.ellipse((ex - 6, ey - 6, ex + 6, ey + 6), outline=col + (a,), width=2)
    # chip body with pins
    d.rounded_rectangle(chip, radius=14, fill=(34, 38, 46, 255), outline=(150, 165, 185, 255), width=3)
    for k in range(14):
        o = chip[0] + 20 + k * 22
        for (xa, ya, xb, yb) in ((o, chip[1] - 18, o + 10, chip[1]), (o, chip[3], o + 10, chip[3] + 18)):
            d.rectangle((xa, ya, xb, yb), fill=(170, 180, 195, 220))
        o2 = chip[1] + 20 + k * 22
        for (xa, ya, xb, yb) in ((chip[0] - 18, o2, chip[0], o2 + 10), (chip[2], o2, chip[2] + 18, o2 + 10)):
            d.rectangle((xa, ya, xb, yb), fill=(170, 180, 195, 220))
    d.rounded_rectangle((chip[0] + 50, chip[1] + 50, chip[2] - 50, chip[3] - 50), radius=8, outline=(236, 140, 72, 200), width=3)
    glow = lay.filter(ImageFilter.GaussianBlur(6))
    im = Image.alpha_composite(im.convert('RGBA'), glow)
    im = Image.alpha_composite(im, lay)
    im.convert('RGB').save(f'{OUT}/circuit.png')


if __name__ == '__main__':
    content_bg(); wafer(); circuit(); print('images written')
