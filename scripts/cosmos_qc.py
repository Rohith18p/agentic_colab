"""Automatic QC for every finished queue clip (Raj via Claude). Runs next to the queue (needs numpy + opencv).

For each <root>/<job>/out.mp4 without a qc.json, it writes qc.json with:
  flicker  std over frames of the mean grey level / its mean. Real GAUGE footage is ~0.005 and good Nano clips ~0.01;
           E6's band-ringing artifact (latent frame 0 mixed into a temporal band) was ~0.5 and fooled a period tool.
  motion   mean absolute grey difference between consecutive frames (0-255 scale), to spot frozen clips.
  frames, mean_grey, flags  ("flicker" when flicker > FLICKER_MAX).
The queue attaches qc.json to the job listing, and `cosmos_queue.py fetch` saves it as <name>.qc.json.
Run: python scripts/cosmos_qc.py --root ~/queue/jobs   (loops; --once for a single pass)
"""
import argparse, json, time
from pathlib import Path
import numpy as np
import cv2

FLICKER_MAX = 0.08


def qc(mp4: Path) -> dict:
    cap = cv2.VideoCapture(str(mp4)); g, prev, diffs = [], None, []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        grey = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
        small = cv2.resize(grey, (grey.shape[1] // 4, grey.shape[0] // 4), interpolation=cv2.INTER_AREA)
        g.append(float(grey.mean()))
        if prev is not None:
            diffs.append(float(np.abs(small - prev).mean()))
        prev = small
    if not g:
        return {"frames": 0, "flags": ["unreadable"]}
    g = np.array(g); flicker = float(g.std() / (g.mean() + 1e-9))
    out = {"frames": len(g), "mean_grey": round(float(g.mean()), 2), "flicker": round(flicker, 4),
           "motion": round(float(np.mean(diffs)) if diffs else 0.0, 3), "flicker_max": FLICKER_MAX, "flags": []}
    if flicker > FLICKER_MAX:
        out["flags"].append("flicker")
    return out


def sweep(root: Path) -> int:
    n = 0
    for mp4 in root.glob("*/out.mp4"):
        dest = mp4.parent / "qc.json"
        if dest.exists():
            continue
        try:
            res = qc(mp4)
        except Exception as e:  # never stop the loop on one bad file
            res = {"flags": ["qc_error"], "error": str(e)[:200]}
        tmp = mp4.parent / "qc.json.tmp"; tmp.write_text(json.dumps(res, indent=1)); tmp.replace(dest); n += 1
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--root", required=True); ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=10.0); a = ap.parse_args()
    root = Path(a.root).expanduser()
    while True:
        k = sweep(root)
        if k:
            print(time.strftime("%H:%M:%S"), "qc", k, "clip(s)", flush=True)
        if a.once:
            break
        time.sleep(a.interval)
