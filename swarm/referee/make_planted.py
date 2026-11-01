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


# --- REST / MOVING at the end of the clip --------------------------------------------------------
# Label rule, stated in every clip's notes: REST if the ball does not move at all during the last
# 1.0 s; MOVING if the ball's centre travels at least one ball diameter during the last 0.5 s.
# Nothing in between is generated, so no clip is borderline.
REST_PPM, REST_R_M = 220.0, 0.05
REST_STILL_S, REST_MOVE_S = 1.0, 0.5
REST_KINDS = {"REST": ("settle_bounce", "settle_roll", "static"), "MOVING": ("bouncing", "rolling", "late_start")}


def ball_path(g: float, e: float, x0: float, h0: float, vx0: float, decel: float, n: int,
              start_s: float = 0.0) -> np.ndarray:
    """Per-frame (x, height of the ball's bottom) in metres. A ball dropped from h0 with horizontal
    speed vx0 bounces with restitution e, then rolls with constant deceleration until it stops.
    With start_s > 0 it sits still until then (as if nudged)."""
    x, y, vx, vy, dt = x0, h0, 0.0 if start_s else vx0, 0.0, 1e-4
    out, t, k = [], 0.0, 0
    while len(out) < n:
        if t >= k / FPS - 1e-12:
            out.append((x, y))
            k += 1
        if t < start_s:
            t += dt
            continue
        if start_s and vx == 0.0 and t - dt < start_s:
            vx = vx0
        if y > 0 or vy > 0:
            vy -= g * dt
            y += vy * dt
            if y < 0:
                y = 0.0
                vy = -vy * e if abs(vy) > 0.15 else 0.0
        if y == 0 and vy == 0 and vx != 0:  # rolling
            s = np.sign(vx)
            vx = s * max(0.0, abs(vx) - decel * dt)
        x += vx * dt
        t += dt
    return np.array(out)


def rest_label(path_px: np.ndarray, r_px: float) -> str | None:
    """REST, MOVING, or None (borderline, or the ball leaves the frame: resample)."""
    if (path_px[:, 0] < r_px + 4).any() or (path_px[:, 0] > W - r_px - 4).any() or (path_px[:, 1] < r_px + 4).any():
        return None
    still = path_px[-int(REST_STILL_S * FPS) - 1:]
    if np.abs(still - still[-1]).max() < 1e-6:
        return "REST"
    move = path_px[-int(REST_MOVE_S * FPS) - 1:]
    if np.linalg.norm(np.diff(move, axis=0), axis=1).sum() >= 2 * r_px:
        return "MOVING"
    return None


def rest_clip(rng: np.random.Generator, kind: str, distractor: str, n: int) -> tuple[list[np.ndarray], str]:
    floor_y = H - 50
    r_px = REST_R_M * REST_PPM
    for _ in range(2000):
        g = 9.81
        x0 = rng.uniform(1.0, 2.8)
        if kind == "static":
            m = ball_path(g, 0.5, x0, 0.0, 0.0, 0.0, n)
        elif kind == "settle_bounce":
            m = ball_path(g, rng.uniform(0.25, 0.55), x0, rng.uniform(0.6, 1.6), rng.uniform(-0.15, 0.15), 2.0, n)
        elif kind == "settle_roll":
            m = ball_path(g, rng.uniform(0.25, 0.5), x0, rng.uniform(0.3, 1.2), rng.choice([-1, 1]) * rng.uniform(0.3, 0.9),
                          rng.uniform(0.4, 1.5), n)
        elif kind == "bouncing":
            m = ball_path(g, rng.uniform(0.8, 0.93), x0, rng.uniform(1.0, 1.7), rng.uniform(-0.05, 0.05), 0.5, n)
        elif kind == "rolling":
            m = ball_path(g, 0.4, x0, rng.uniform(0.0, 0.3), rng.choice([-1, 1]) * rng.uniform(0.3, 0.8),
                          rng.uniform(0.0, 0.08), n)
        else:  # late_start: still for most of the clip, then rolls away during the last ~1 s
            m = ball_path(g, 0.4, x0, 0.0, rng.choice([-1, 1]) * rng.uniform(0.4, 0.9), rng.uniform(0.0, 0.3), n,
                          start_s=n / FPS - rng.uniform(0.7, 1.4))
        px = np.stack([m[:, 0] * REST_PPM, floor_y - (m[:, 1] + REST_R_M) * REST_PPM], axis=1)
        label = rest_label(px, r_px)
        if label is not None and kind in REST_KINDS[label]:
            break
    else:
        raise RuntimeError(f"could not sample a {kind} clip")
    bg = background(rng, floor_y)
    color = tuple(int(v) for v in rng.uniform(30, 200, 3))
    box_y, box_x0, box_v = rng.uniform(60, 200), rng.uniform(0, W - 300), rng.uniform(40, 90) * rng.choice([-1, 1])
    phase = rng.uniform(0, 2 * math.pi)
    frames = []
    for i, (x, y) in enumerate(px):
        img = bg.copy()
        if distractor == "box":  # something else moves all clip long, so whole-frame stillness is not REST
            bx = (box_x0 + box_v * i / FPS) % (W - 60)
            cv2.rectangle(img, (int(bx), int(box_y)), (int(bx) + 60, int(box_y) + 40), (60, 90, 150), -1)
        ball(img, x, y, r_px, color)
        if distractor == "flicker":  # lighting changes, the ball doesn't
            img = np.clip(img.astype(np.float32) * (1 + 0.07 * math.sin(2 * math.pi * 1.3 * i / FPS + phase)),
                          0, 255).astype(np.uint8)
        frames.append(img)
    return finish(frames, rng), label


def rest_set(rng: np.random.Generator, out: Path, per_kind: int) -> tuple[list[dict], dict]:
    jobs = [(kind, ("none", "box", "flicker")[j % 3]) for kinds in REST_KINDS.values() for kind in kinds
            for j in range(per_kind)]
    order = rng.permutation(len(jobs))
    clips, answers = [], {}
    for k, (kind, distractor) in enumerate(jobs):
        clip_id = f"rest-{order[k]:02d}"
        n = int(rng.choice([93, 121, 189]))
        frames, label = rest_clip(rng, kind, distractor, n)
        path = out / f"{clip_id}.mp4"
        write_mp4(path, frames)
        clips.append({"clip_id": clip_id, "path": str(path), "fps": FPS, "size": f"{W}x{H}", "frames": n,
                      "params": ["end_state"], "labels": {"end_state": ["REST", "MOVING"]},
                      "notes": "One ball on a floor; other things in the scene may move or flicker. end_state is REST "
                               f"if the ball does not move at all during the last {REST_STILL_S:g} s, MOVING if its "
                               f"centre travels at least one ball diameter during the last {REST_MOVE_S:g} s (no clip is "
                               "in between). You may answer UNCLEAR; it counts as scored but not correct. Ball "
                               "diameter 0.10 m. Static camera."})
        answers[clip_id] = {"end_state": label}
    return clips, answers


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--store", required=True, help="shared clip store; clips go to <store>/planted/")
    p.add_argument("--secret", required=True, help="referee-only directory for answers.json")
    p.add_argument("--seed", type=int, default=20261031)
    p.add_argument("--add-rest", type=int, metavar="N", default=0,
                   help="only add N REST/MOVING clips per scene kind (6 kinds) to an existing planted set, "
                        "keeping its clips and answers; pick a --seed nobody has seen")
    args = p.parse_args()
    rng = np.random.default_rng(args.seed)
    out = Path(args.store) / "planted"
    out.mkdir(parents=True, exist_ok=True)
    Path(args.secret).mkdir(parents=True, exist_ok=True)

    if args.add_rest:
        man_path, ans_path = out / "planted.json", Path(args.secret) / "answers.json"
        manifest = json.loads(man_path.read_text()) if man_path.exists() else {"clips": []}
        answers = json.loads(ans_path.read_text()) if ans_path.exists() else {}
        keep = [c for c in manifest["clips"] if not c["clip_id"].startswith("rest-")]
        answers = {k: v for k, v in answers.items() if not k.startswith("rest-")}
        new_clips, new_answers = rest_set(rng, out, args.add_rest)
        clips = sorted(keep + new_clips, key=lambda c: c["clip_id"])
        man_path.write_text(json.dumps({**manifest, "clips": clips}, indent=2))
        ans_path.write_text(json.dumps({**answers, **new_answers}, indent=2))
        n_rest = sum(v["end_state"] == "REST" for v in new_answers.values())
        print(f"added {len(new_clips)} REST/MOVING clips ({n_rest} REST) to {out}; {len(clips)} clips in total")
        return

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
