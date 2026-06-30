#!/usr/bin/env python3
"""Run2 report: 4-way comparison per prompt — pure-baseline (IR-off, reused from run1) +
the three IR-on solver variants (solver-baseline / solver-structure / solver-structrepair).

Usage: python3 tools/ir_ab_report2.py <run2_root> <run1_root>
Outputs under <run2_root>:
  figures/figure2_prompt_NN.png   prompt on top; 4 columns; rows = standardized views
  metrics2_report.md, metrics2.csv   structural collision / floating rates across the 4
"""
import sys, os, json, glob, csv, textwrap


def load_json(p):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def metrics(d):
    m = load_json(os.path.join(d, "metrics.json"))
    if m is None:
        s = load_json(os.path.join(d, "actual_summary.json")) or {}
        m = s.get("metrics")
    return m or {}


def views(d):
    out = {}
    for p in sorted(glob.glob(os.path.join(d, "comparison_views", "view_*.png"))):
        base = os.path.basename(p)[:-4]
        parts = base.split("_", 2)
        out[parts[2] if len(parts) >= 3 else base] = p
    return out


def main():
    if len(sys.argv) < 2:
        print("usage: ir_ab_report2.py <run2_root> [run1_root]")
        sys.exit(1)
    run2 = sys.argv[1].rstrip("/")
    run1 = sys.argv[2].rstrip("/") if len(sys.argv) > 2 else None
    COLS = [
        ("pure-baseline (IR-off)", run1, "prompt_%02d_hybrid_ir-off_vanilla"),
        ("solver-baseline", run2, "prompt_%02d_hybrid_solver-baseline_vanilla"),
        ("solver-structure", run2, "prompt_%02d_hybrid_solver-structure_vanilla"),
        ("solver-structrepair", run2, "prompt_%02d_hybrid_solver-structrepair_vanilla"),
    ]
    labels = [c[0] for c in COLS if c[1]]
    prompts = range(1, 10)

    rows = []
    for n in prompts:
        for (label, root, pat) in COLS:
            if not root:
                continue
            d = os.path.join(root, pat % n)
            if not os.path.isdir(d):
                continue
            m = metrics(d)
            rows.append({"prompt": n, "variant": label, "checked": m.get("checked", 0),
                         "struct_pairs": m.get("structural_collision_pairs", 0),
                         "struct_rate": m.get("structural_collision_rate", 0),
                         "coll_rate": m.get("collision_rate", 0), "floating": m.get("floating", 0),
                         "float_rate": m.get("floating_rate", 0), "oob": m.get("out_of_bounds", 0)})

    with open(os.path.join(run2, "metrics2.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["prompt", "variant", "checked", "struct_pairs", "struct_rate", "coll_rate", "floating", "float_rate", "oob"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    def mean(label, key):
        vs = [r[key] for r in rows if r["variant"] == label and r["checked"]]
        return sum(vs) / len(vs) if vs else 0.0

    L = ["# IR run2 — solver variants (+ pure baseline)", "",
         "Structural collision rate = offending big-object actors / checked (lower is better).", "",
         "## Overall means", "",
         "| variant | struct.coll.rate | coll.rate(all) | floating.rate |", "|---|---|---|---|"]
    for lab in labels:
        L.append("| %s | %.3f | %.3f | %.3f |" % (lab, mean(lab, "struct_rate"), mean(lab, "coll_rate"), mean(lab, "float_rate")))
    L += ["", "## Per-prompt structural collision rate", "",
          "| prompt | " + " | ".join(labels) + " |", "|---" + "|---" * len(labels) + "|"]
    for n in prompts:
        cells = []
        for lab in labels:
            r = next((r for r in rows if r["prompt"] == n and r["variant"] == lab), None)
            cells.append("%.3f" % r["struct_rate"] if r else "-")
        L.append("| %d | %s |" % (n, " | ".join(cells)))
    with open(os.path.join(run2, "metrics2_report.md"), "w") as f:
        f.write("\n".join(L) + "\n")

    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as e:
        print("PIL missing (%s); metrics written, skipping figures" % e)
        return
    figdir = os.path.join(run2, "figures")
    os.makedirs(figdir, exist_ok=True)
    font = ImageFont.load_default()
    COLW, PAD, LBLW, TITLE_H, HEADER_H = 360, 8, 80, 60, 20
    colcolor = [(150, 0, 0), (150, 90, 0), (0, 110, 0), (0, 80, 150)]
    made = 0
    for n in prompts:
        colimgs = []
        prompt_text = ""
        for (label, root, pat) in COLS:
            d = os.path.join(root, pat % n) if root else None
            vi = views(d) if d and os.path.isdir(d) else {}
            if not prompt_text and d and os.path.isdir(d):
                req = load_json(os.path.join(d, "request.json")) or {}
                prompt_text = req.get("message", "")
            colimgs.append((label, vi))
        keys = []
        for _, vi in colimgs:
            for k in vi:
                if k not in keys:
                    keys.append(k)
        if not keys:
            continue
        rows_im = []
        for k in keys:
            row = []
            for _, vi in colimgs:
                p = vi.get(k)
                im = None
                if p and os.path.exists(p):
                    try:
                        im = Image.open(p).convert("RGB")
                        w, h = im.size
                        im = im.resize((COLW, max(1, int(h * COLW / w))))
                    except Exception:
                        im = None
                row.append(im)
            rows_im.append((k, row))
        rowh = [max([im.size[1] for im in r if im] + [50]) for _, r in rows_im]
        ncol = len(COLS)
        W = LBLW + ncol * (COLW + PAD) + PAD
        H = TITLE_H + HEADER_H + sum(rowh) + PAD * (len(rows_im) + 2)
        canvas = Image.new("RGB", (W, H), (255, 255, 255))
        dr = ImageDraw.Draw(canvas)
        dr.text((PAD, 4), "Scene %d  —  pure-baseline(IR-off) | solver-baseline | solver-structure | solver-structrepair" % n, fill=(0, 0, 0), font=font)
        for i, line in enumerate(textwrap.wrap(prompt_text, 175)[:2]):
            dr.text((PAD, 18 + i * 12), line, fill=(70, 70, 70), font=font)
        y = TITLE_H
        for ci, (label, _root, _pat) in enumerate(COLS):
            dr.text((LBLW + ci * (COLW + PAD) + PAD, y), label, fill=colcolor[ci % len(colcolor)], font=font)
        y += HEADER_H
        for (k, r), rh in zip(rows_im, rowh):
            dr.text((4, y + rh // 2), k[:13], fill=(0, 0, 0), font=font)
            for ci, im in enumerate(r):
                x = LBLW + ci * (COLW + PAD) + PAD
                if im:
                    canvas.paste(im, (x, y))
                else:
                    dr.text((x + 10, y + 10), "(no image)", fill=(190, 0, 0), font=font)
            y += rh + PAD
        out = os.path.join(figdir, "figure2_prompt_%02d.png" % n)
        canvas.save(out)
        made += 1
    print("metrics2_report.md + metrics2.csv + %d figure(s) written under %s" % (made, run2))


if __name__ == "__main__":
    main()
