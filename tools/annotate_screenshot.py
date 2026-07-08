#!/usr/bin/env python3
"""Annotate a staged-build screenshot for the per-tier VLM reflect agent (staged builder, Phase 3).

Pinhole-projects the solved plan's world points into the screenshot and draws:
  - numbered MARKS at each spawned actor's origin (color by status: ok / defect / spawn_failed),
  - GHOST footprints (dashed) at the planned positions of not-yet-placed (scheduled) objects,
  - a projected 10 m ground GRID + a NORTH arrow,
  - a tier BANNER ("tier k/K placed") + a legend mapping mark# -> plan id -> asset.

Set-of-Mark grounding: the reflect agent references objects by MARK NUMBER, and every mark's world
position is known, so its edits stay grounded (no "move it left a bit").

Usage:
  annotate_screenshot.py <config.json> <in.png> <out.png>
config.json = { camera:{location:[x,y,z],rotation:[roll,pitch,yaw],fov_h_deg,width,height},
                items:[{mark,id,asset,tier,status,x_cm,y_cm,w_m,d_m,scheduled}],
                tier, tiers, scene }
Coordinates: UE left-handed, Z-up, centimetres. rotation degrees [roll,pitch,yaw].
"""
import json, math, sys

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception as e:
    sys.stderr.write("annotate_screenshot: PIL required (%s)\n" % e); sys.exit(3)

DEG = math.pi / 180.0

# Status → mark color (RGBA).
STATUS_COLOR = {
    "placed-ok": (60, 200, 90, 255), "ok": (60, 200, 90, 255),
    "placed-defect": (235, 70, 70, 255), "colliding": (235, 70, 70, 255), "floating": (235, 160, 40, 255),
    "spawn_failed": (150, 150, 150, 255), "solver_blocked": (200, 120, 220, 255),
    "scheduled": (80, 160, 235, 255),
}
TIER_GHOST = {1: (90, 170, 255), 2: (150, 200, 120), 3: (210, 180, 90)}


def _basis(roll, pitch, yaw):
    """UE camera basis vectors (roll≈0 for our cameras). forward=+X, right=+Y, up=+Z, left-handed."""
    p, y = pitch * DEG, yaw * DEG
    cf = math.cos(p)
    fwd = (cf * math.cos(y), cf * math.sin(y), math.sin(p))
    right = (-math.sin(y), math.cos(y), 0.0)         # horizontal right (roll 0)
    # up = right × forward (left-handed) → gives a stable image-up
    up = (right[1] * fwd[2] - right[2] * fwd[1],
          right[2] * fwd[0] - right[0] * fwd[2],
          right[0] * fwd[1] - right[1] * fwd[0])
    return fwd, right, up


def make_projector(cam):
    C = cam["location"]
    roll, pitch, yaw = cam.get("rotation", [0, -82, 0])
    fov_h = float(cam.get("fov_h_deg", 90.0))
    W, H = int(cam["width"]), int(cam["height"])
    fwd, right, up = _basis(roll, pitch, yaw)
    thx = math.tan(fov_h * DEG / 2.0)
    thy = thx * (H / float(W))          # vertical fov derived from aspect (UE FieldOfView is horizontal)

    def project(x, y, z):
        rel = (x - C[0], y - C[1], z - C[2])
        f = rel[0] * fwd[0] + rel[1] * fwd[1] + rel[2] * fwd[2]
        if f <= 1.0:
            return None                  # behind / at the camera
        r = rel[0] * right[0] + rel[1] * right[1] + rel[2] * right[2]
        u = rel[0] * up[0] + rel[1] * up[1] + rel[2] * up[2]
        sx = (r / f) / thx
        sy = (u / f) / thy
        px = (0.5 + 0.5 * sx) * W
        py = (0.5 - 0.5 * sy) * H
        return (px, py, f)
    return project, W, H


def _font(sz):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try: return ImageFont.truetype(p, sz)
        except Exception: pass
    return ImageFont.load_default()


def _dashed_poly(draw, pts, color, width=2, dash=8):
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        if seg < 1: continue
        n = max(1, int(seg / dash))
        for k in range(0, n, 2):
            t0, t1 = k / n, min(1.0, (k + 1) / n)
            draw.line([(a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0),
                       (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)], fill=color, width=width)


def annotate(cfg, in_png, out_png):
    img = Image.open(in_png).convert("RGBA")
    project, W, H = make_projector(cfg["camera"])
    # scale the source image to the camera's declared W×H if needed
    if img.size != (W, H):
        img = img.resize((W, H))
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    f_sm, f_md = _font(max(11, W // 90)), _font(max(14, W // 64))

    # 10 m ground grid (z=0)
    step = 1000.0  # cm
    items = cfg.get("items", [])
    xs = [it["x_cm"] for it in items] or [0]
    ys = [it["y_cm"] for it in items] or [0]
    lo = min(-6000, int(min(xs)) - 1000); hi = max(6000, int(max(xs)) + 1000)
    loy = min(-6000, int(min(ys)) - 1000); hiy = max(6000, int(max(ys)) + 1000)
    gx = int(math.floor(lo / step) * step)
    while gx <= hi:
        seg = []
        yy = loy
        while yy <= hiy:
            p = project(gx, yy, 0)
            if p: seg.append((p[0], p[1]))
            yy += step / 4
        if len(seg) > 1: d.line(seg, fill=(255, 255, 255, 55), width=1)
        gx += step
    gy = int(math.floor(loy / step) * step)
    while gy <= hiy:
        seg = []
        xx = lo
        while xx <= hi:
            p = project(xx, gy, 0)
            if p: seg.append((p[0], p[1]))
            xx += step / 4
        if len(seg) > 1: d.line(seg, fill=(255, 255, 255, 55), width=1)
        gy += step

    # ghost footprints for scheduled (not-yet-placed) items; solid marks for placed.
    for it in items:
        x, y = it["x_cm"], it["y_cm"]
        scheduled = bool(it.get("scheduled"))
        status = "scheduled" if scheduled else str(it.get("status", "ok"))
        color = STATUS_COLOR.get(status, STATUS_COLOR["ok"])
        w = float(it.get("w_m", 1.0)) * 100.0; ddp = float(it.get("d_m", 1.0)) * 100.0
        if scheduled:
            corners = [(x - w / 2, y - ddp / 2), (x + w / 2, y - ddp / 2), (x + w / 2, y + ddp / 2), (x - w / 2, y + ddp / 2)]
            proj = [project(cx, cy, 0) for cx, cy in corners]
            if all(proj):
                _dashed_poly(d, [(p[0], p[1]) for p in proj], TIER_GHOST.get(int(it.get("tier", 2)), (150, 150, 150)) + (200,))
        p = project(x, y, 20)
        if not p: continue
        px, py = p[0], p[1]
        if px < -40 or px > W + 40 or py < -40 or py > H + 40: continue
        rr = 9 if not scheduled else 6
        d.ellipse([px - rr, py - rr, px + rr, py + rr], fill=color, outline=(20, 20, 20, 255), width=2)
        mk = str(it.get("mark", ""))
        tb = d.textbbox((0, 0), mk, font=f_sm)
        d.text((px - (tb[2] - tb[0]) / 2, py - (tb[3] - tb[1]) / 2 - 1), mk, fill=(15, 15, 15, 255), font=f_sm)

    # north arrow (world +X = North in this convention) at top-right
    o = project(0, 0, 0)
    npt = project(2000, 0, 0)
    if o and npt:
        ax, ay = W - 70, 70
        vx, vy = npt[0] - o[0], npt[1] - o[1]
        vl = math.hypot(vx, vy) or 1
        vx, vy = vx / vl * 34, vy / vl * 34
        d.line([(ax, ay), (ax + vx, ay + vy)], fill=(255, 255, 255, 230), width=3)
        d.text((ax + vx, ay + vy), "N", fill=(255, 255, 255, 230), font=f_md)

    # tier banner + legend
    tier, tiers = cfg.get("tier", "?"), cfg.get("tiers", "?")
    banner = "tier %s/%s placed" % (tier, tiers)
    d.rectangle([0, 0, W, 26], fill=(0, 0, 0, 150))
    d.text((8, 5), banner + "   ·   " + str(cfg.get("scene", ""))[:70], fill=(255, 255, 255, 255), font=f_md)

    out = Image.alpha_composite(img, ov).convert("RGB")
    out.save(out_png)
    return True


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.stderr.write("usage: annotate_screenshot.py <config.json> <in.png> <out.png>\n"); sys.exit(2)
    with open(sys.argv[1]) as fh:
        cfg = json.load(fh)
    annotate(cfg, sys.argv[2], sys.argv[3])
    print("annotated -> " + sys.argv[3])
