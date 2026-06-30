#!/usr/bin/env python3
"""run3 report: 3-way per prompt — pure-baseline (IR-off, run1) | run2 structure (untuned) |
run3 structure (tuned, size-aware clamp; harbor uses the _rerun cell).

Usage: python3 tools/ir_ab_report3.py <run3> <run2> <run1>
Outputs under <run3>: figures3/figure3_prompt_NN.png, metrics3_report.md, metrics3.csv
"""
import sys, os, json, glob, csv, textwrap

run3, run2, run1 = sys.argv[1].rstrip('/'), sys.argv[2].rstrip('/'), sys.argv[3].rstrip('/')

def cell3(n):
    return "prompt_03_hybrid_solver-structure_vanilla_rerun" if n == 3 else "prompt_%02d_hybrid_solver-structure_vanilla" % n
COLS = [
    ("pure-baseline (IR-off)", run1, lambda n: "prompt_%02d_hybrid_ir-off_vanilla" % n),
    ("run2 structure (untuned)", run2, lambda n: "prompt_%02d_hybrid_solver-structure_vanilla" % n),
    ("run3 structure (tuned)", run3, cell3),
]
labels = [c[0] for c in COLS]

def lj(p):
    try:
        return json.load(open(p))
    except Exception:
        return None

def metrics(d):
    m = lj(os.path.join(d, "metrics.json"))
    if m is None:
        s = lj(os.path.join(d, "actual_summary.json")) or {}
        m = s.get("metrics")
    return m or {}

def views(d):
    out = {}
    for p in sorted(glob.glob(os.path.join(d, "comparison_views", "view_*.png"))):
        b = os.path.basename(p)[:-4]; parts = b.split("_", 2)
        out[parts[2] if len(parts) >= 3 else b] = p
    return out

rows = []
for n in range(1, 10):
    for (lab, root, fn) in COLS:
        d = os.path.join(root, fn(n))
        if not os.path.isdir(d):
            continue
        m = metrics(d)
        rows.append({"prompt": n, "variant": lab, "checked": m.get("checked", 0),
                     "struct_rate": m.get("structural_collision_rate", 0), "coll_rate": m.get("collision_rate", 0),
                     "floating": m.get("floating", 0), "float_rate": m.get("floating_rate", 0)})

with open(os.path.join(run3, "metrics3.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["prompt", "variant", "checked", "struct_rate", "coll_rate", "floating", "float_rate"])
    w.writeheader()
    [w.writerow(r) for r in rows]

def mean(lab, k):
    v = [r[k] for r in rows if r["variant"] == lab and r["checked"]]
    return sum(v) / len(v) if v else 0.0

L = ["# IR run3 — tuned structure vs untuned vs no-IR", "",
     "Structural collision rate (lower=better). run3 = size-aware clamp + densified planner; harbor = _rerun cell.", "",
     "## Overall means", "", "| variant | struct.coll.rate | floating.rate |", "|---|---|---|"]
for lab in labels:
    L.append("| %s | %.3f | %.3f |" % (lab, mean(lab, "struct_rate"), mean(lab, "float_rate")))
L += ["", "## Per-prompt structural collision rate", "",
      "| prompt | " + " | ".join(labels) + " |", "|---" + "|---" * len(labels) + "|"]
for n in range(1, 10):
    cells = []
    for lab in labels:
        r = next((r for r in rows if r["prompt"] == n and r["variant"] == lab), None)
        cells.append("%.3f" % r["struct_rate"] if r else "-")
    L.append("| %d | %s |" % (n, " | ".join(cells)))
open(os.path.join(run3, "metrics3_report.md"), "w").write("\n".join(L) + "\n")

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception as e:
    print("PIL missing (%s); metrics written, no figures" % e); sys.exit(0)
figdir = os.path.join(run3, "figures3"); os.makedirs(figdir, exist_ok=True)
font = ImageFont.load_default()
COLW, PAD, LBLW, TITLE_H, HEADER_H = 380, 8, 80, 56, 18
colcolor = [(150, 0, 0), (150, 90, 0), (0, 90, 150)]
made = 0
for n in range(1, 10):
    cols = []
    ptext = ""
    for (lab, root, fn) in COLS:
        d = os.path.join(root, fn(n)); vi = views(d) if os.path.isdir(d) else {}
        if not ptext and os.path.isdir(d):
            ptext = (lj(os.path.join(d, "request.json")) or {}).get("message", "")
        cols.append(vi)
    keys = []
    for vi in cols:
        for k in vi:
            if k not in keys:
                keys.append(k)
    if not keys:
        continue
    rim = []
    for k in keys:
        row = []
        for vi in cols:
            p = vi.get(k); im = None
            if p and os.path.exists(p):
                try:
                    im = Image.open(p).convert("RGB"); w, h = im.size; im = im.resize((COLW, max(1, int(h * COLW / w))))
                except Exception:
                    im = None
            row.append(im)
        rim.append((k, row))
    rowh = [max([im.size[1] for im in r if im] + [50]) for _, r in rim]
    W = LBLW + len(COLS) * (COLW + PAD) + PAD
    H = TITLE_H + HEADER_H + sum(rowh) + PAD * (len(rim) + 2)
    canvas = Image.new("RGB", (W, H), (255, 255, 255)); dr = ImageDraw.Draw(canvas)
    dr.text((PAD, 4), "Scene %d  —  pure-baseline(IR-off) | run2 structure (untuned) | run3 structure (tuned)" % n, fill=(0, 0, 0), font=font)
    for i, line in enumerate(textwrap.wrap(ptext, 175)[:2]):
        dr.text((PAD, 18 + i * 11), line, fill=(70, 70, 70), font=font)
    y = TITLE_H
    for ci, lab in enumerate(labels):
        dr.text((LBLW + ci * (COLW + PAD) + PAD, y), lab, fill=colcolor[ci % 3], font=font)
    y += HEADER_H
    for (k, r), rh in zip(rim, rowh):
        dr.text((4, y + rh // 2), k[:13], fill=(0, 0, 0), font=font)
        for ci, im in enumerate(r):
            x = LBLW + ci * (COLW + PAD) + PAD
            canvas.paste(im, (x, y)) if im else dr.text((x + 10, y + 10), "(no image)", fill=(190, 0, 0), font=font)
        y += rh + PAD
    canvas.save(os.path.join(figdir, "figure3_prompt_%02d.png" % n)); made += 1
print("metrics3_report.md + metrics3.csv + %d figures (figures3/) written" % made)
