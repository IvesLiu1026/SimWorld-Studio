#!/usr/bin/env python3
"""Render a solved IR plan as a labeled top-down image for the plan-critic VLM.
Usage: render_plan.py <placed.json> <out.png>
placed.json = the solver's `placed` list: [{x_m,y_m,footprint:{width,depth},category,name,yaw_deg,isGround}]
Draws each object's footprint (rotated by yaw), colored by category, over a grid, with a legend —
so a VLM can judge density, grouping, focal structure, orientation, and empty space."""
import sys, json, math, hashlib
from PIL import Image, ImageDraw, ImageFont

placed = json.load(open(sys.argv[1]))
out = sys.argv[2]
objs = [p for p in placed if not p.get("isGround")]
ground = [p for p in placed if p.get("isGround")]
if not objs:
    Image.new("RGB", (400, 400), (240, 240, 240)).save(out); sys.exit(0)

# extent (metres) around origin
mx = max(max(abs(p.get("x_m", 0)), abs(p.get("y_m", 0))) for p in objs)
ext = max(20, math.ceil((mx + 5) / 5) * 5)
PX = 1000                      # image is PX x PX
scale = PX / (2 * ext)         # px per metre
def to_px(x, y):               # world (m, +X east, +Y north) -> image px (y down)
    return (int((x + ext) * scale), int((ext - y) * scale))

FONT = ImageFont.load_default()
def _font(sz):
    try: return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", sz)
    except Exception: return FONT
fL, fS = _font(18), _font(12)

# stable color per category
def color(cat):
    h = int(hashlib.md5((cat or "?").encode()).hexdigest(), 16)
    return (80 + h % 150, 80 + (h >> 8) % 150, 80 + (h >> 16) % 150)

MARGIN = 40
img = Image.new("RGB", (PX + 2 * MARGIN, PX + 2 * MARGIN + 30), (255, 255, 255))
d = ImageDraw.Draw(img)
# plot area bg + grid (every 10 m)
d.rectangle([MARGIN, MARGIN, MARGIN + PX, MARGIN + PX], fill=(238, 238, 240), outline=(150, 150, 150))
g = 10
gm = int(ext // g) * g
for k in range(-gm, gm + 1, g):
    x0, y0 = to_px(k, ext); x1, y1 = to_px(k, -ext)
    d.line([(MARGIN + x0, MARGIN + y0), (MARGIN + x1, MARGIN + y1)], fill=(220, 220, 224))
    xa, ya = to_px(-ext, k); xb, yb = to_px(ext, k)
    d.line([(MARGIN + xa, MARGIN + ya), (MARGIN + xb, MARGIN + yb)], fill=(220, 220, 224))

def rot(cx, cy, w, h, deg):    # 4 corners of a w x h rect centered at (cx,cy) rotated deg (world m)
    a = math.radians(deg); ca, sa = math.cos(a), math.sin(a)
    pts = []
    for dx, dy in [(-w/2, -h/2), (w/2, -h/2), (w/2, h/2), (-w/2, h/2)]:
        pts.append((cx + dx*ca - dy*sa, cy + dx*sa + dy*ca))
    return pts

cats = {}
for p in objs:
    fp = p.get("footprint") or {}
    w = max(0.4, fp.get("width", 1)); h = max(0.4, fp.get("depth", 1))
    col = color(p.get("category"))
    cats[p.get("category", "?")] = col
    corners = rot(p.get("x_m", 0), p.get("y_m", 0), w, h, p.get("yaw_deg", 0))
    poly = [(MARGIN + to_px(x, y)[0], MARGIN + to_px(x, y)[1]) for x, y in corners]
    d.polygon(poly, fill=col, outline=(30, 30, 30))

# title + N arrow + scale
d.text((MARGIN, 8), f"INTENDED PLAN (top-down)  ·  {len(objs)} objects  ·  ±{ext} m  ·  N up, +X east", fill=(0, 0, 0), font=fL)
d.text((MARGIN + PX - 60, MARGIN + 6), "N↑", fill=(0, 0, 0), font=fL)
# legend
ly = MARGIN + PX + 6; lx = MARGIN
for cat, col in sorted(cats.items(), key=lambda x: x[0]):
    d.rectangle([lx, ly, lx + 12, ly + 12], fill=col, outline=(60, 60, 60))
    d.text((lx + 15, ly - 1), cat, fill=(0, 0, 0), font=fS)
    lx += 20 + int(fS.getlength(cat)) + 16
    if lx > PX - 120: lx = MARGIN; ly += 16
img.save(out)
print(out)
