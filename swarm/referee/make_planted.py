"""Make planted clips with known physics for the referee.

Renders simple static-camera scenes at Cosmos3-Nano's output format (832x480,
24 fps) with known constants: free fall (g, and restitution e when it
bounces), pendulums (period_s) and slides down an incline (a, along the
slope). Constants are deliberately not Earth's, so a tool that assumes
g = 9.81 fails.

    python swarm/referee/make_planted.py --store <shared clip store> --secret <referee-only dir>

Writes <store>/planted/*.mp4 and <store>/planted/planted.json (public), and
<secret>/answers.json (keep this off any machine that runs an agent).
Needs numpy, opencv-python and ffmpeg.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from pathlib import Path

import cv2
import numpy as np

W, H, FPS = 832, 480, 24
SHIFT = 4  # subpixel drawing precision for cv2 (coordinates scaled by 2**SHIFT)
S = 1 << SHIFT


def background(rng: np.random.Generator, floor_y: int | None) -> np.ndarray:
    base = np.linspace(rng.uniform(150, 200), rng.uniform(110, 150), H)[:, None, None]
    tint = rng.uniform(0.85, 1.1, size=3)[None, None, :]
    img = np.repeat(np.repeat(base, W, axis=1), 3, axis=2) * tint
    noise = cv2.GaussianBlur(rng.normal(0, 18, (H, W)).astype(np.float32), (0, 0), 6)
    img += noise[:, :, None]
    for _ in range(rng.integers(2, 5)):  # a few static props on the wall
        x, y = int(rng.uniform(0, W - 120)), int(rng.uniform(20, H - 200))
        w, h = int(rng.uniform(40, 120)), int(rng.uniform(30, 120))
        img[y:y + h, x:x + w] = img[y:y + h, x:x + w] * 0.75 + rng.uniform(40, 120, 3) * 0.25
    if floor_y is not None:
        img[floor_y:] = img[floor_y:] * 0.55 + np.array([60, 70, 80]) * 0.45
    return np.clip(img, 0, 255).astype(np.uint8)


def ball(img: np.ndarray, x: float, y: float, r: float, color: tuple[int, int, int]) -> None:
    c = (int(round(x * S)), int(round(y * S)))
    cv2.circle(img, c, int(round(r * S)), color, -1, cv2.LINE_AA, SHIFT)
    hi = tuple(min(255, int(v * 1.5 + 40)) for v in color)
    cv2.circle(img, (int(round((x - r * 0.35) * S)), int(round((y - r * 0.35) * S))), int(round(r * 0.35 * S)),
               hi, -1, cv2.LINE_AA, SHIFT)


def finish(frames: list[np.ndarray], rng: np.random.Generator) -> list[np.ndarray]:
    out = []
    for f in frames:
        noisy = f.astype(np.float32) + rng.normal(0, 2.0, f.shape)
        out.append(np.clip(noisy, 0, 255).astype(np.uint8))
    return out


def write_mp4(path: Path, frames: list[np.ndarray]) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", str(path)]
    subprocess.run(cmd, input=b"".join(f.tobytes() for f in frames), check=True)


def drop(rng: np.random.Generator, g: float, e: float | None, seconds: float) -> list[np.ndarray]:
    ppm, r_m = 220.0, 0.05
    floor_y = int(H - 50)
    bg = background(rng, floor_y)
    x = rng.uniform(250, 580)
    h0 = rng.uniform(1.4, 1.7)  # height of the ball's bottom above the floor, m
    color = tuple(int(v) for v in rng.uniform(30, 200, 3))
    y, v, dt = h0, 0.0, 1e-4
    frames, t, next_frame = [], 0.0, 0.0
    while len(frames) < int(seconds * FPS):
        if t >= next_frame - 1e-12:
            img = bg.copy()
            ball(img, x, floor_y - (y + r_m) * ppm, r_m * ppm, color)
            frames.append(img)
            next_frame += 1.0 / FPS
        v -= g * dt
        y += v * dt
        if y < 0:
            y = 0.0
            v = -v * e if e is not None and abs(v) > 0.05 else 0.0
        t += dt
    return finish(frames, rng)


def pendulum(rng: np.random.Generator, g: float, length: float, amp_deg: float, seconds: float) -> tuple[list[np.ndarray], float]:
    ppm, r_m = 330.0, 0.04
    pivot = (W / 2, 36.0)
    bg = background(rng, None)
    cv2.rectangle(bg, (int(pivot[0] - 60), 22), (int(pivot[0] + 60), 34), (70, 70, 70), -1)
    color = tuple(int(v) for v in rng.uniform(30, 200, 3))
    th, om, dt = math.radians(amp_deg), 0.0, 1e-4
    frames, t, next_frame = [], 0.0, 0.0
    crossings: list[float] = []
    prev_th = th
    while t < seconds:
        if t >= next_frame - 1e-12 and len(frames) < int(seconds * FPS):
            img = bg.copy()
            bx = pivot[0] + length * ppm * math.sin(th)
            by = pivot[1] + length * ppm * math.cos(th)
            cv2.line(img, (int(pivot[0] * S), int(pivot[1] * S)), (int(bx * S), int(by * S)), (40, 40, 40), 1,
                     cv2.LINE_AA, SHIFT)
            ball(img, bx, by, r_m * ppm, color)
            frames.append(img)
            next_frame += 1.0 / FPS
        # RK4 on the full nonlinear pendulum
        def f(a: float, w: float) -> tuple[float, float]:
            return w, -(g / length) * math.sin(a)
        k1 = f(th, om)
        k2 = f(th + dt / 2 * k1[0], om + dt / 2 * k1[1])
        k3 = f(th + dt / 2 * k2[0], om + dt / 2 * k2[1])
        k4 = f(th + dt * k3[0], om + dt * k3[1])
        th += dt / 6 * (k1[0] + 2 * k2[0] + 2 * k3[0] + k4[0])
        om += dt / 6 * (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1])
        t += dt
        if prev_th > 0 >= th:  # downward zero crossing, interpolated
            crossings.append(t - dt * th / (th - prev_th))
        prev_th = th
    period = float(np.mean(np.diff(crossings)))
    return finish(frames, rng), period


def slide(rng: np.random.Generator, g: float, angle_deg: float, mu: float, seconds: float) -> tuple[list[np.ndarray], float]:
    ppm, block_l, block_h, ramp_l = 280.0, 0.12, 0.06, 2.4
    th = math.radians(angle_deg)
    a = g * (math.sin(th) - mu * math.cos(th))
    bottom = (W / 2 + ramp_l * math.cos(th) * ppm / 2, H - 40.0)
    top = (bottom[0] - ramp_l * math.cos(th) * ppm, bottom[1] - ramp_l * math.sin(th) * ppm)
    bg = background(rng, int(bottom[1]))
    ramp = np.array([[top[0], top[1]], [bottom[0], bottom[1]], [top[0], bottom[1]]]) * S
    cv2.fillPoly(bg, [ramp.astype(np.int32)], (90, 110, 140), cv2.LINE_AA, SHIFT)
    color = tuple(int(v) for v in rng.uniform(30, 200, 3))
    ux, uy = math.cos(th), math.sin(th)  # down-slope unit vector (image coords)
    nx, ny = math.sin(th), -math.cos(th)  # outward normal
    frames = []
    for i in range(int(seconds * FPS)):
        t = i / FPS
        s = min(0.15 + 0.5 * a * t * t, ramp_l - block_l - 0.05)
        cx, cy = top[0] + s * ppm * ux, top[1] + s * ppm * uy
        corners = [(cx, cy), (cx + block_l * ppm * ux, cy + block_l * ppm * uy),
                   (cx + block_l * ppm * ux + block_h * ppm * nx, cy + block_l * ppm * uy + block_h * ppm * ny),
                   (cx + block_h * ppm * nx, cy + block_h * ppm * ny)]
        img = bg.copy()
        cv2.fillPoly(img, [(np.array(corners) * S).astype(np.int32)], color, cv2.LINE_AA, SHIFT)
        frames.append(img)
    return finish(frames, rng), a


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--store", required=True, help="shared clip store; clips go to <store>/planted/")
    p.add_argument("--secret", required=True, help="referee-only directory for answers.json")
    p.add_argument("--seed", type=int, default=20261031)
    args = p.parse_args()
    rng = np.random.default_rng(args.seed)
    out = Path(args.store) / "planted"
    out.mkdir(parents=True, exist_ok=True)
    Path(args.secret).mkdir(parents=True, exist_ok=True)

    clips, answers = [], {}
    order = rng.permutation(16)  # clip ids don't reveal generation order
    specs = (
        [("drop", dict(g=g, e=None)) for g in (9.81, 6.0, 3.7, 14.5)]
        + [("drop", dict(g=g, e=e)) for g, e in ((9.81, 0.7), (5.2, 0.8), (12.0, 0.55), (7.5, 0.65))]
        + [("pendulum", dict(g=g, length=L, amp=amp)) for g, L, amp in
           ((9.81, 0.9, 20), (4.0, 0.6, 25), (13.0, 1.1, 15), (7.0, 0.75, 30))]
        + [("slide", dict(g=g, angle=ang, mu=mu)) for g, ang, mu in
           ((9.81, 30, 0.25), (6.5, 35, 0.2), (12.0, 22, 0.15), (8.0, 28, 0.35))]
    )
    for k, (kind, spec) in enumerate(specs):
        clip_id = f"planted-{order[k]:02d}"
        if kind == "drop":
            frames = drop(rng, spec["g"], spec["e"], 2.5 if spec["e"] else 1.6)
            truth = {"g": spec["g"], **({"e": spec["e"]} if spec["e"] else {})}
            notes = "A ball is released from rest in the first frame and falls. Ball diameter 0.10 m."
            params = list(truth)
        elif kind == "pendulum":
            frames, period = pendulum(rng, spec["g"], spec["length"], spec["amp"], 6.0)
            truth = {"period_s": round(period, 4)}
            notes = f"A pendulum released from rest. String length (pivot to bob centre) {spec['length']} m; bob diameter 0.08 m."
            params = ["period_s"]
        else:
            frames, a = slide(rng, spec["g"], spec["angle"], spec["mu"], 2.0)
            truth = {"a": round(a, 4)}
            notes = "A block is released from rest on an incline. Block length along the slope 0.12 m. Report acceleration along the slope."
            params = ["a"]
        path = out / f"{clip_id}.mp4"
        write_mp4(path, frames)
        clips.append({"clip_id": clip_id, "path": str(path), "fps": FPS, "size": f"{W}x{H}", "frames": len(frames),
                      "params": params, "units": {"g": "m/s^2", "e": "ratio", "period_s": "s", "a": "m/s^2"},
                      "notes": notes + " Static camera facing the motion plane; no lens distortion."})
        answers[clip_id] = truth
    clips.sort(key=lambda c: c["clip_id"])
    (out / "planted.json").write_text(json.dumps({"clips": clips}, indent=2))
    (Path(args.secret) / "answers.json").write_text(json.dumps(answers, indent=2))
    print(f"wrote {len(clips)} clips to {out} and answers to {args.secret}/answers.json")


if __name__ == "__main__":
    main()
