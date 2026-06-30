#!/usr/bin/env python3
"""Build per-prompt IR on/off comparison figures + an aggregate metrics report from an
A/B eval run dir produced by run_asset_retrieval_ab_eval.js (--ir-modes on,off).

Usage: python3 tools/ir_ab_report.py <run_root>
Outputs (under <run_root>):
  figures/figure_prompt_NN.png   prompt on top; rows = standardized views;
                                 left column = WITHOUT IR, right column = WITH IR
  metrics_report.md, metrics.csv collision / floating / boundary(OOB) rates, off vs on
"""
import sys, os, json, glob, csv, textwrap


def load_json(p):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def find_runs(run_root):
    runs = []
    summ = load_json(os.path.join(run_root, "summary.json"))
    if summ and isinstance(summ.get("runs"), list):
        for r in summ["runs"]:
            d = r.get("dir") or os.path.join(run_root, r.get("label", ""))
            runs.append({"dir": d, "prompt_index": r.get("prompt_index"),
                         "prompt": r.get("prompt", ""), "irMode": r.get("irMode"),
                         "mode": r.get("mode"), "loopMode": r.get("loopMode")})
        return runs
    # fallback: scan prompt_* dirs
    for d in sorted(glob.glob(os.path.join(run_root, "prompt_*"))):
        if not os.path.isdir(d):
            continue
        name = os.path.basename(d)
        req = load_json(os.path.join(d, "request.json")) or {}
        ir = "on" if "_ir-on" in name else ("off" if "_ir-off" in name else None)
        try:
            pidx = int(name.split("_")[1])
        except Exception:
            pidx = None
        runs.append({"dir": d, "prompt_index": pidx, "prompt": req.get("message", ""), "irMode": ir})
    return runs


def run_metrics(run):
    m = load_json(os.path.join(run["dir"], "metrics.json"))
    if m is None:
        s = load_json(os.path.join(run["dir"], "actual_summary.json")) or {}
        m = s.get("metrics")
    return m or {}


def view_images(run):
    d = os.path.join(run["dir"], "comparison_views")
    imgs = {}
    for p in sorted(glob.glob(os.path.join(d, "view_*.png"))):
        base = os.path.basename(p)[:-4]
        parts = base.split("_", 2)
        imgs[parts[2] if len(parts) >= 3 else base] = p
    if not imgs:  # fallback to the builder's own screenshots
        for i, p in enumerate(sorted(glob.glob(os.path.join(run["dir"], "screenshots", "*.png")))):
            imgs["shot_%02d" % i] = p
    return imgs


def build_figures(run_root, by_prompt):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as e:
        print("PIL not available (%s) — skipping image figures; metrics still produced." % e)
        return []
    fig_dir = os.path.join(run_root, "figures")
    os.makedirs(fig_dir, exist_ok=True)
    font = ImageFont.load_default()
    COLW, PAD, LBLW, TITLE_H, HEADER_H = 480, 10, 96, 70, 22
    made = []
    for pidx in sorted(by_prompt, key=lambda x: (x is None, x)):
        arms = by_prompt[pidx]
        off, on = arms.get("off"), arms.get("on")
        if not off and not on:
            continue
        prompt_text = (off or on).get("prompt", "")
        off_imgs = view_images(off) if off else {}
        on_imgs = view_images(on) if on else {}
        keys = list(off_imgs.keys()) + [k for k in on_imgs if k not in off_imgs]
        if not keys:
            continue
        rows = []
        for k in keys:
            row = []
            for imgs in (off_imgs, on_imgs):
                p = imgs.get(k)
                im = None
                if p and os.path.exists(p):
                    try:
                        im = Image.open(p).convert("RGB")
                        w, h = im.size
                        im = im.resize((COLW, max(1, int(h * COLW / w))))
                    except Exception:
                        im = None
                row.append(im)
            rows.append((k, row))
        rowh = [max([im.size[1] for im in r if im] + [60]) for _, r in rows]
        W = LBLW + 2 * (COLW + PAD) + PAD
        H = TITLE_H + HEADER_H + sum(rowh) + PAD * (len(rows) + 2)
        canvas = Image.new("RGB", (W, H), (255, 255, 255))
        dr = ImageDraw.Draw(canvas)
        dr.text((PAD, 6), "Scene %s  (left = WITHOUT IR, right = WITH IR)" % pidx, fill=(0, 0, 0), font=font)
        for i, line in enumerate(textwrap.wrap(prompt_text, 150)[:3]):
            dr.text((PAD, 24 + i * 13), line, fill=(70, 70, 70), font=font)
        y = TITLE_H
        dr.text((LBLW + PAD, y), "WITHOUT IR", fill=(160, 0, 0), font=font)
        dr.text((LBLW + COLW + 2 * PAD, y), "WITH IR", fill=(0, 120, 0), font=font)
        y += HEADER_H
        for (k, r), rh in zip(rows, rowh):
            dr.text((4, y + rh // 2), k[:15], fill=(0, 0, 0), font=font)
            for ci, im in enumerate(r):
                x = LBLW + ci * (COLW + PAD) + PAD
                if im:
                    canvas.paste(im, (x, y))
                else:
                    dr.text((x + 10, y + 10), "(no image)", fill=(190, 0, 0), font=font)
            y += rh + PAD
        out = os.path.join(fig_dir, "figure_prompt_%s.png" % str(pidx).zfill(2))
        canvas.save(out)
        made.append(out)
    return made


def main():
    if len(sys.argv) < 2:
        print("usage: ir_ab_report.py <run_root>")
        sys.exit(1)
    run_root = sys.argv[1]
    runs = find_runs(run_root)
    by_prompt = {}
    for r in runs:
        if r.get("irMode") in ("on", "off"):
            by_prompt.setdefault(r["prompt_index"], {})[r["irMode"]] = r

    rows = []
    for pidx in sorted(by_prompt, key=lambda x: (x is None, x)):
        for arm in ("off", "on"):
            r = by_prompt[pidx].get(arm)
            if not r:
                continue
            m = run_metrics(r)
            rows.append({"prompt": pidx, "ir": arm,
                         "checked": m.get("checked", 0), "collision_actors": m.get("collision_actors", 0),
                         "collision_pairs": m.get("collision_pairs", 0),
                         "structural_collision_pairs": m.get("structural_collision_pairs", 0),
                         "structural_collision_rate": m.get("structural_collision_rate", 0),
                         "floating": m.get("floating", 0), "out_of_bounds": m.get("out_of_bounds", 0),
                         "collision_rate": m.get("collision_rate", 0), "floating_rate": m.get("floating_rate", 0),
                         "oob_rate": m.get("oob_rate", 0)})

    with open(os.path.join(run_root, "metrics.csv"), "w", newline="") as f:
        cols = ["prompt", "ir", "checked", "collision_actors", "collision_pairs",
                "structural_collision_pairs", "structural_collision_rate",
                "floating", "out_of_bounds", "collision_rate", "floating_rate", "oob_rate"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    def mean(arm, key):
        vals = [r[key] for r in rows if r["ir"] == arm and r["checked"]]
        return sum(vals) / len(vals) if vals else 0.0

    lines = ["# IR A/B metrics — %s" % os.path.basename(run_root.rstrip("/")), "",
             "Pre-straighten, all non-infra actors. Lower is better.", "",
             "## Overall means (rate = offending actors / checked)", "",
             "| metric | WITHOUT IR | WITH IR | Δ (on−off) |", "|---|---|---|---|"]
    for key, label in [("structural_collision_rate", "structural collision rate (KEY)"), ("collision_rate", "collision rate (incl. clutter)"), ("floating_rate", "floating rate"), ("oob_rate", "boundary/OOB rate")]:
        o, n = mean("off", key), mean("on", key)
        lines.append("| %s | %.3f | %.3f | %+.3f |" % (label, o, n, n - o))
    lines += ["", "Structural = collisions involving >=1 non-clutter object (>=0.8m radius); small-prop-only overlaps are excluded (the solver intentionally allows them).",
              "", "## Per-scene", "",
              "| scene | ir | checked | struct.coll.pairs | struct.coll.rate | coll.pairs(all) | floating | OOB | float.rate | oob.rate |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| %s | %s | %d | %d | %.3f | %d | %d | %d | %.3f | %.3f |" % (
            r["prompt"], r["ir"], r["checked"], r["structural_collision_pairs"], r["structural_collision_rate"],
            r["collision_pairs"], r["floating"], r["out_of_bounds"], r["floating_rate"], r["oob_rate"]))
    with open(os.path.join(run_root, "metrics_report.md"), "w") as f:
        f.write("\n".join(lines) + "\n")

    figs = build_figures(run_root, by_prompt)
    print("metrics_report.md + metrics.csv written; %d figure(s):" % len(figs))
    for x in figs:
        print("  " + x)


if __name__ == "__main__":
    main()
