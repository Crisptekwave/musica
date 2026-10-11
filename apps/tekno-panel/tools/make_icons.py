"""Draw the PWA icons (acid orb + waveform on black). Run from the tekno-panel folder: python3 tools/make_icons.py"""
import math
from PIL import Image, ImageDraw, ImageFilter

def icon(size, pad=0.0):
    S = size * 4
    img = Image.new("RGB", (S, S), (0, 0, 0))
    glow = Image.new("RGB", (S, S), (0, 0, 0))
    g = ImageDraw.Draw(glow)
    c = S / 2
    r = S * (0.36 - pad * 0.5)
    g.ellipse([c - r * 1.15, c - r * 1.15, c + r * 1.15, c + r * 1.15], fill=(60, 20, 120))
    g.ellipse([c - r, c - r, c + r, c + r], outline=(57, 255, 20), width=int(S * 0.035))
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.04))
    img.paste(glow)
    d = ImageDraw.Draw(img)
    d.ellipse([c - r, c - r, c + r, c + r], outline=(57, 255, 20), width=int(S * 0.022))
    r2 = r * 0.78
    d.ellipse([c - r2, c - r2, c + r2, c + r2], outline=(157, 77, 255), width=int(S * 0.01))
    bars = 9
    bw = r * 1.1 / bars
    for i in range(bars):
        h = r * 0.95 * (0.25 + 0.75 * abs(math.sin(i * 1.3 + 0.4))) * (1 - abs(i - 4) / 6)
        x = c - r * 0.55 + i * bw
        col = (57, 255, 20) if i % 2 == 0 else (199, 125, 255)
        d.rounded_rectangle([x + bw * 0.18, c - h / 2, x + bw * 0.82, c + h / 2], radius=bw * 0.3, fill=col)
    return img.resize((size, size), Image.LANCZOS)

icon(192).save("static/icons/icon-192.png")
icon(512).save("static/icons/icon-512.png")
icon(512, pad=0.12).save("static/icons/icon-maskable-512.png")
icon(180).save("static/icons/apple-touch-icon.png")
print("ok")
