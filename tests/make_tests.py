#!/usr/bin/env python3
"""
make_tests.py - synthetic clips with known frame errors for rife-gapfill v2.

Renders a scene on a perfect timeline, then applies errors and writes:
  <name>.mov          ProRes 422 (like an Atomos recording)
  <name>.truth.json   per source frame: true time index; list of events

Scene segments (25 fps, 960x540):
  A  0-149    camera pan, speed ramps 1 -> 14 px/frame and back (acceleration)
  B  150-269  locked-off camera, small object (~3% of frame) moving
  C  270-329  fully static (sensor noise only)
  D  330      cut to a different scene
  D  330-479  diagonal pan, steady 6 px/frame, one flash frame at 400
  E  480-599  slow pan 1.5 px/frame (hard case: small steps)
"""
import json
import os
import subprocess
import sys

import cv2
import numpy as np

W, H, FPS, N = 960, 540, 25, 600
rng = np.random.default_rng(7)


def texture(seed, w, h):
    r = np.random.default_rng(seed)
    t = np.zeros((h, w, 3), np.float32)
    for s, a in ((256, 90), (64, 50), (16, 30), (4, 18)):
        n = r.random((h // s + 2, w // s + 2, 3)).astype(np.float32)
        t += cv2.resize(n, (w, h), interpolation=cv2.INTER_CUBIC) * a
    for _ in range(140):
        c = tuple(float(x) for x in r.integers(30, 230, 3))
        p = (int(r.integers(0, w)), int(r.integers(0, h)))
        if r.random() < 0.5:
            cv2.circle(t, p, int(r.integers(8, 60)), c, -1, cv2.LINE_AA)
        else:
            q = (p[0] + int(r.integers(-120, 120)), p[1] + int(r.integers(-120, 120)))
            cv2.rectangle(t, p, q, c, -1, cv2.LINE_AA)
    return np.clip(t, 0, 255)


TEX1 = texture(1, 4200, 1400)
TEX2 = texture(2, 2600, 2000)


def crop(tex, x, y):
    m = np.float32([[1, 0, -x], [0, 1, -y]])
    return cv2.warpAffine(tex, m, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def scene_positions():
    """Camera/object position for each true time index."""
    pos = []
    x = 100.0
    for t in range(150):                        # A: ramp up and down
        v = 1 + 13 * np.sin(np.pi * t / 149) ** 2
        x += v
        pos.append(("A", x, 200.0))
    for t in range(150, 330):                   # B + C: locked off
        pos.append(("BC", x, 200.0))
    dx, dy = 300.0, 300.0
    for t in range(330, 480):                   # D: diagonal pan
        dx += 6 * 0.8
        dy += 6 * 0.6
        pos.append(("D", dx, dy))
    for t in range(480, N):                     # E: slow pan
        dx += 1.5
        pos.append(("E", dx, dy))
    return pos


def render(t, pos):
    seg, x, y = pos[t]
    if seg in ("A", "BC"):
        img = crop(TEX1, x, y)
        if 150 <= t < 270:                      # B: moving object
            ox = -70 + (t - 150) * 9.0         # enters and leaves at the frame edges
            oy = 270 + 60 * np.sin((t - 150) / 15)
            cv2.circle(img, (int(ox), int(oy)), 55, (240, 200, 60), -1, cv2.LINE_AA)
            cv2.circle(img, (int(ox) + 15, int(oy) - 10), 18, (40, 40, 160), -1, cv2.LINE_AA)
    else:
        img = crop(TEX2, x, y)
        if t == 400:                            # flash
            img = img * 1.0 + 70
    img = img + rng.normal(0, 2.0, img.shape)   # sensor noise, new each true frame
    return np.clip(img, 0, 255).astype(np.uint8)


def make_frames():
    pos = scene_positions()
    return [render(t, pos) for t in range(N)]


def apply_errors(frames, ops):
    """ops: dict true_index -> 'drop' | 'repeat' (replace with previous) | 'extra' (duplicate after).
    Returns source frames and their true times (repeat/extra carry the previous time)."""
    src, times = [], []
    for t, f in enumerate(frames):
        op = ops.get(t)
        if op == "drop":
            continue
        if op == "repeat":                      # recorder shows previous frame instead
            src.append(src[-1].copy())
            times.append(times[-1])
            continue
        src.append(f)
        times.append(t)
        if op == "extra":
            src.append(f.copy())
            times.append(t)
    return src, times


def events_from_times(times):
    ev = []
    for i in range(1, len(times)):
        k = times[i] - times[i - 1]
        if k == 0:
            ev.append({"frame": i, "type": "repeat"})
        elif k > 1:
            ev.append({"frame": i, "type": "missing", "count": k - 1})
    return ev


def write(name, src, times, outdir):
    path = os.path.join(outdir, name + ".mov")
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                          "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                          "-c:v", "prores_ks", "-profile:v", "2", "-pix_fmt", "yuv422p10le", path],
                         stdin=subprocess.PIPE)
    for f in src:
        p.stdin.write(f.tobytes())
    p.stdin.close()
    p.wait()
    ev = events_from_times(times)
    with open(os.path.join(outdir, name + ".truth.json"), "w") as f:
        json.dump({"times": times, "events": ev}, f)
    print(f"{name}: {len(src)} frames, {len(ev)} events")


def main(outdir):
    os.makedirs(outdir, exist_ok=True)
    frames = make_frames()

    write("clean", frames, list(range(N)), outdir)

    # random drops (single and multi), repeat+jump, extra duplicates, across all segments
    ops = {}
    for t in (37, 81, 118, 205, 241, 355, 437, 521, 566):
        ops[t] = "drop"
    for t in (60, 61):                           # two in a row (fast pan)
        ops[t] = "drop"
    for t in (372, 373, 374):                    # three in a row
        ops[t] = "drop"
    for t in (95, 228, 456, 540):               # repeat instead of the real frame
        ops[t] = "repeat"
    for t in (130, 190, 410, 585):              # extra duplicated frame
        ops[t] = "extra"
    src, times = apply_errors(frames, ops)
    write("random", src, times, outdir)

    # periodic: one dropped frame every 24
    ops = {t: "drop" for t in range(12, N, 24)}
    src, times = apply_errors(frames, ops)
    write("periodic", src, times, outdir)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
