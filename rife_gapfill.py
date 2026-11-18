#!/usr/bin/env python3
"""
rife_gapfill.py  v2.0

Finds missing and repeated frames in a video and rebuilds an even cadence:
repeated frames are dropped and missing frames are interpolated with RIFE.
Handles periodic gaps (AI video that drops one frame per generation block),
random drops (recorders that lose frames in high-frame-rate modes), repeated
frames, and repeats that stand in for a missing frame.
Usable as a command-line tool and as a module (used by gapfill_gui.py).

How it works
  1. Analysis (one pass, downscaled grey):
       - DIS optical flow between every pair of frames. The step speed is the
         median flow inside the area that actually changed, so both camera
         moves and a small moving object are measured.
       - Mean absolute difference, for repeats (noise-level difference) and
         for cuts/flashes (flow cannot explain the change).
  2. Timeline model: every step is given a length in frame intervals.
       0 = repeated frame, 1 = normal, k = k-1 frames missing.
     The expected speed of each step comes from its neighbours (median of a
     symmetric window, so steady acceleration is followed). A step that is a
     whole multiple of the expected speed is a gap.
  3. Periodic prior: if gaps fall on a regular grid, borderline steps on the
     grid are accepted too, and the grid can be filled through still shots.
  4. Every event gets a confidence. Events above the threshold are applied
     automatically; the rest are listed for review.
  5. Render: original frames pass through untouched, repeats are dropped,
     missing frames are interpolated with RIFE at the right time positions,
     ProRes output at an even frame rate.

Requirements
  - ffmpeg + ffprobe (found automatically on PATH, next to the script/exe, or
    in the WinGet package folder; or pass --ffmpeg / --ffprobe)
  - Python 3.9+, numpy, pillow, opencv-python-headless
  - rife-ncnn-vulkan: https://github.com/nihui/rife-ncnn-vulkan/releases
    (v4 models are needed to insert more than one frame in a gap)

Changes
  2.0  New analysis: optical flow + timeline model. Detects random and
       periodic missing frames (1-3 in a row), repeated frames and repeat +
       jump; confidence per event; review support; multi-frame RIFE inserts.
  1.1  Importable functions for the GUI; tool auto-detection; fps in output
       name; no console windows; cancel support.
  1.0  First version (periodic gaps only).
"""

import argparse
import csv
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import List, Optional

import numpy as np
from PIL import Image

try:
    import cv2
except ImportError:  # reported when analysis starts
    cv2 = None

VERSION = "2.0"

NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW


class GapfillError(Exception):
    pass


class Cancelled(Exception):
    pass


@dataclass
class Options:
    ffmpeg: str = ""
    ffprobe: str = ""
    rife: str = ""
    model: str = "rife-v4.6"
    gpu: Optional[int] = None
    prores: str = "hq"            # 422 | hq | 4444
    out_fps: str = "same"         # same | keep | number   ("restore" = keep)
    # detection
    analysis_width: int = 480
    window: int = 5               # neighbours on each side for expected speed
    gap_ratio: float = 1.5        # step / expected speed to count as a gap
    grid_ratio: float = 1.3       # same, on a detected periodic grid
    min_speed: float = 0.3        # px/step at analysis width; slower = not judgeable
    repeat_ratio: float = 0.25    # diff / local diff below this = repeated frame
    max_insert: int = 3           # longest gap that is filled (frames)
    min_conf: float = 0.5         # events at or above are applied automatically
    detect_repeats: bool = True
    periodic: bool = True         # use a periodic grid when one is found
    fill_static: bool = True      # on a periodic grid, also fill still shots
    period: Optional[int] = None  # force a period


@dataclass
class Event:
    frame: int                    # source frame index the step leads into
    kind: str                     # missing | repeat | long | cut
    count: int = 0                # frames missing (missing/long)
    ratio: float = 1.0            # step speed / expected speed
    conf: float = 0.0
    grid: bool = False
    apply: bool = False
    note: str = ""

    def label(self):
        if self.kind == "missing":
            return f"missing {self.count}"
        if self.kind == "long":
            return f"gap {self.count}+ (not filled)"
        return self.kind


# ------------------------------------------------------------ tool finding

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def find_tool(name):
    """Locate ffmpeg/ffprobe/rife-ncnn-vulkan. Returns path or ''."""
    exe = name + (".exe" if os.name == "nt" else "")
    base = app_dir()
    for p in (os.path.join(base, exe), os.path.join(base, "bin", exe)):
        if os.path.isfile(p):
            return p
    w = shutil.which(name)
    if w:
        return w
    pats = []
    if name.startswith("rife"):
        pats.append(os.path.join(base, "rife-ncnn-vulkan*", exe))
        pats.append(os.path.join(base, "*", "rife-ncnn-vulkan*", exe))
    else:
        la = os.environ.get("LOCALAPPDATA")
        if la:
            pats.append(os.path.join(la, "Microsoft", "WinGet", "Packages",
                                     "*FFmpeg*", "*", "bin", exe))
        pats.append(r"C:\ffmpeg\bin\\" + exe)
    for pat in pats:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return ""


def resolve_tools(opt):
    if not opt.ffmpeg:
        opt.ffmpeg = find_tool("ffmpeg")
    if not opt.ffprobe:
        if opt.ffmpeg:
            cand = os.path.join(os.path.dirname(opt.ffmpeg),
                                "ffprobe" + (".exe" if os.name == "nt" else ""))
            if os.path.isfile(cand):
                opt.ffprobe = cand
        if not opt.ffprobe:
            opt.ffprobe = find_tool("ffprobe")
    if not opt.ffmpeg or not opt.ffprobe:
        raise GapfillError("ffmpeg/ffprobe not found. Set the ffmpeg path.")


def list_models(rife_exe):
    d = os.path.dirname(os.path.abspath(rife_exe)) if rife_exe else ""
    if not d or not os.path.isdir(d):
        return []
    return sorted(x for x in os.listdir(d)
                  if x.startswith("rife") and os.path.isdir(os.path.join(d, x)))


# ----------------------------------------------------------------- probing

def probe(path, ffprobe):
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
           "-show_entries",
           "stream=width,height,r_frame_rate,avg_frame_rate,pix_fmt,"
           "color_primaries,color_transfer,color_space,color_range:stream_tags=timecode"
           ":format_tags=timecode",
           "-of", "json", path]
    res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, **NOWIN)
    if res.returncode != 0:
        raise GapfillError("ffprobe failed: " + res.stderr.decode(errors="replace"))
    j = json.loads(res.stdout)
    streams = j.get("streams") or []
    if not streams:
        raise GapfillError("no video stream found")
    s = streams[0]
    tc = (s.get("tags") or {}).get("timecode") or (j.get("format", {}).get("tags") or {}).get("timecode")
    s["timecode"] = tc
    return s


def color_params(info):
    cs = info.get("color_space") or "bt709"
    if cs in ("unknown", "reserved"):
        cs = "bt709"
    matrix = {"bt709": "bt709", "smpte170m": "bt601", "bt470bg": "bt601",
              "bt2020nc": "bt2020", "bt2020c": "bt2020"}.get(cs, "bt709")
    prim = info.get("color_primaries") or "bt709"
    trc = info.get("color_transfer") or "bt709"
    if prim == "unknown":
        prim = "bt709"
    if trc == "unknown":
        trc = "bt709"
    rng = "pc" if info.get("color_range") == "pc" else "tv"
    return matrix, rng, prim, trc, cs


def read_frames(stream, nbytes):
    while True:
        buf = stream.read(nbytes)
        if len(buf) < nbytes:
            return
        yield buf


def _err_text(f):
    try:
        f.seek(0)
        return f.read().decode(errors="replace").strip()
    except Exception:
        return ""


def frames_to_tc(n, fps, start=None):
    rate = int(round(float(fps)))
    base = 0
    if start:
        try:
            h, m, s, f = [int(x) for x in start.replace(";", ":").split(":")]
            base = ((h * 60 + m) * 60 + s) * rate + f
        except ValueError:
            base = 0
    n = base + n
    f = n % rate
    ts = n // rate
    return f"{ts // 3600 % 24:02d}:{ts // 60 % 60:02d}:{ts % 60:02d}:{f:02d}"


# ---------------------------------------------------------------- analysis

def measure(path, info, opt, log=print, progress=None, cancel=None):
    """One decode pass. Returns per-step arrays (index i = step from i-1 to i)."""
    if cv2 is None:
        raise GapfillError("OpenCV is missing (pip install opencv-python-headless)")
    W, H = int(info["width"]), int(info["height"])
    aw = min(opt.analysis_width, W)
    ah = max(2, int(round(aw * H / W / 2)) * 2)
    cmd = [opt.ffmpeg, "-v", "error", "-i", path, "-map", "0:v:0",
           "-fps_mode", "passthrough",
           "-vf", f"scale={aw}:{ah}:flags=area,format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    errf = tempfile.TemporaryFile()
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=errf, **NOWIN)
    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    gx, gy = np.meshgrid(np.arange(aw, dtype=np.float32), np.arange(ah, dtype=np.float32))
    diff, speed, area, explain, resid = [0.0], [0.0], [0.0], [1.0], [0.0]
    prev = None
    n = 0
    est = None
    try:
        nb = info.get("nb_frames")
        est = int(nb) if nb and str(nb).isdigit() else None
    except Exception:
        est = None
    try:
        for buf in read_frames(p.stdout, aw * ah):
            if cancel and cancel():
                p.kill()
                raise Cancelled()
            f = np.frombuffer(buf, np.uint8).reshape(ah, aw)
            if prev is not None:
                ad = np.abs(f.astype(np.int16) - prev.astype(np.int16)).astype(np.float32)
                d = float(ad.mean())
                if d < 1e-3:                               # identical frame
                    diff.append(d); speed.append(0.0); area.append(0.0); explain.append(1.0)
                    resid.append(0.0)
                else:
                    flow = dis.calc(prev, f, None)
                    mag = np.hypot(flow[..., 0], flow[..., 1])
                    thr = max(4.0, 3.0 * float(np.median(ad)))
                    m = ad > thr
                    a = float(m.mean())
                    s = float(np.median(mag[m])) if a > 0.003 else float(np.percentile(mag, 98))
                    warped = cv2.remap(f, gx + flow[..., 0], gy + flow[..., 1],
                                       cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
                    res = np.abs(warped.astype(np.float32) - prev)
                    if a > 0.003:
                        # how much of the change in the changed area the flow explains
                        e = 1.0 - float(res[m].mean()) / max(float(ad[m].mean()), 1e-6)
                    else:
                        e = 1.0                                # nothing changed beyond noise
                    diff.append(d); speed.append(s); area.append(a)
                    explain.append(e)
                    # what is left after motion compensation: sensor/grain noise for a
                    # real frame, close to zero for a repeated one
                    resid.append(float(np.median(res)))
            prev = f
            n += 1
            if progress and n % 10 == 0:
                progress(n, est or n)
    finally:
        p.wait()
    if p.returncode not in (0, None) and not (cancel and cancel()):
        raise GapfillError("ffmpeg failed during analysis: " + _err_text(errf))
    return {"diff": np.array(diff), "speed": np.array(speed), "area": np.array(area),
            "explain": np.array(explain), "resid": np.array(resid), "aw": aw}


def _side_stats(vals, valid, seg, i, w):
    """Neighbour speeds left and right of step i, within the same shot."""
    n = len(vals)
    L = [j for j in range(max(1, i - w), i) if valid[j] and seg[j] == seg[i]]
    R = [j for j in range(i + 1, min(n, i + w + 1)) if valid[j] and seg[j] == seg[i]]
    return L, R


def _trend_sigma(js, v, i, med):
    """Relative spread of neighbour speeds around a straight-line trend."""
    if len(js) < 4 or med <= 0:
        return 0.1
    js = np.asarray(js, float)
    keep = np.abs(v / med - 1.0) < 0.6           # ignore other gaps in the window
    if keep.sum() >= 4:
        js, v = js[keep], v[keep]
    a, b = np.polyfit(js - i, v, 1)
    res = (v - (a * (js - i) + b)) / med
    return max(float(np.median(np.abs(res))) * 1.4826, 0.04)


def _find_period(score, pmin=3, pmax=240):
    """score: log-ratio per step (0 = normal). Returns (P, phase, strength)."""
    n = len(score)
    best = None
    res = []
    for P in range(pmin, min(pmax, n // 4) + 1):
        bs, bp = -9.0, 1
        for ph in range(1, P + 1):
            s = float(np.median(score[ph::P]))
            if s > bs:
                bs, bp = s, ph
        res.append((P, bp, bs))
    if not res:
        return None
    top = max(s for _, _, s in res)
    for P, ph, s in res:
        if s >= top * 0.85:
            best = (P, ph, float(np.exp(s)))
            break
    return best


def _expect(vals, k, valid, seg, opt, floor):
    """Expected value per step from its neighbours (normalised by step length k).
    Returns pred, ratio, rmin (against the larger side), side agreement, sigma."""
    n = len(vals)
    pred = np.zeros(n)
    ratio = np.ones(n)
    rmin = np.ones(n)
    side = np.ones(n)
    sig = np.full(n, 0.1)
    norm = vals / np.maximum(k, 1)
    for i in range(1, n):
        if not valid[i]:
            continue
        L, R = _side_stats(norm, valid, seg, i, opt.window)
        js = L + R
        if len(js) < 2:
            continue
        v = norm[js]
        med = float(np.median(v))
        if med < floor:
            continue
        pred[i] = med
        ratio[i] = vals[i] / med
        if len(L) >= 2 and len(R) >= 2:
            ml, mr = float(np.median(norm[L])), float(np.median(norm[R]))
            rmin[i] = vals[i] / max(ml, mr, 1e-6)
            side[i] = min(ml, mr) / max(ml, mr, 1e-6)
        else:
            rmin[i] = ratio[i]
        sig[i] = _trend_sigma(js, v, i, med)
    return pred, ratio, rmin, side, sig


def classify(m, opt, fps):
    diff, speed, explain, area = m["diff"], m["speed"], m["explain"], m["area"]
    resid = m.get("resid", np.zeros(len(diff)))
    n = len(diff)
    events = {}

    # --- cuts / flashes: big change that flow cannot explain
    seg = np.zeros(n, int)
    cut = np.zeros(n, bool)
    s_id = 0
    for i in range(1, n):
        lo, hi = max(1, i - 8), min(n - 1, i + 8)
        loc = np.median(np.delete(diff[lo:hi + 1], i - lo)) if hi > lo else diff[i]
        if explain[i] < 0.35 and diff[i] > 2.5 * max(loc, 0.5):
            cut[i] = True
            s_id += 1
            events[i] = Event(i, "cut", ratio=0, conf=1.0, note="cut or flash, left alone")
        seg[i] = s_id

    # --- is optical flow reliable here? (it explains most of the change)
    flow_ok = np.zeros(n, bool)
    for i in range(1, n):
        js = [j for j in range(max(1, i - 8), min(n, i + 9))
              if seg[j] == seg[i] and not cut[j] and diff[j] > 1e-3]
        if js:
            flow_ok[i] = float(np.median(explain[js])) >= 0.5

    base_valid = ~cut
    base_valid[0] = False

    # --- first estimate of expected speed / change (median: robust to single outliers)
    k = np.ones(n)
    sp0 = _expect(speed, k, base_valid, seg, opt, opt.min_speed)
    df0 = _expect(diff, k, base_valid, seg, opt, 0.15)

    # --- repeats: no change while the neighbours on both sides move
    rep = np.zeros(n, bool)
    if opt.detect_repeats:
        for i in range(1, n):
            if cut[i] or df0[0][i] <= 0:
                continue
            L, R = _side_stats(diff, base_valid, seg, i, opt.window)
            if len(L) < 2 or len(R) < 2:
                continue
            if flow_ok[i] and (np.median(speed[L]) < opt.min_speed
                               or np.median(speed[R]) < opt.min_speed):
                continue          # a still stretch: a repeat would be invisible and harmless
            dr = diff[i] / df0[0][i]
            sr = speed[i] / sp0[0][i] if sp0[0][i] > 0 else 1.0
            nb_area = float(np.median(area[L + R]))
            ar = area[i] / nb_area if nb_area > 0.003 else 1.0
            nb_res = float(np.median(resid[L + R]))
            rr = resid[i] / nb_res if nb_res > 0.3 else 0.0   # noise not measurable: no veto
            if rr > 0.5:
                continue          # fresh sensor noise: a real frame that barely moved
            if (dr < opt.repeat_ratio or (flow_ok[i] and sr < 0.2 and dr < 0.6)
                    or (flow_ok[i] and sr < 0.35 and dr < 0.7 and ar < 0.2)):
                rep[i] = True
                conf = 0.5 + 0.5 * float(np.clip(1.0 - min(dr / opt.repeat_ratio, sr / 0.35, ar / 0.2), 0, 1))
                note = f"change {dr:.2f}x of neighbours"
                if flow_ok[i]:
                    note += f", motion {sr:.2f}x"
                events[i] = Event(i, "repeat", ratio=0.0, conf=conf, note=note)

    # --- step length k: iterate with repeats excluded and gaps normalised
    valid = base_valid & ~rep
    k = np.ones(n)
    k[rep] = 0
    for _ in range(3):
        sp = _expect(speed, k, valid, seg, opt, opt.min_speed)
        df = _expect(diff, k, valid, seg, opt, 0.15)
        use_flow = flow_ok & (sp[0] > 0)
        ratio = np.where(use_flow, sp[1], df[1])
        rmin = np.where(use_flow, sp[2], df[2])
        side = np.where(use_flow, sp[3], df[3])
        sig = np.where(use_flow, sp[4], df[4])
        pred = np.where(use_flow, sp[0], np.where(flow_ok, 0.0, df[0]))
        newk = np.ones(n)
        newk[rep] = 0
        big = valid & (pred > 0) & (ratio >= opt.gap_ratio) & (rmin >= 1.3)
        newk[big] = np.maximum(2, np.round(ratio[big]))
        if np.array_equal(newk, k):
            break
        k = newk

    # --- gaps
    judge = valid & (pred > 0)
    jump_limit = (opt.max_insert + 1) * 1.6
    for i in np.where(judge & (ratio >= opt.gap_ratio) & (rmin >= 1.3))[0]:
        q = float(ratio[i])
        kk = int(max(2, round(q)))
        sep = (q - 1.0) / sig[i]
        fit = 1.0 - min(1.0, abs(q - kk) / 0.5)
        agree = float(np.clip((side[i] - 0.4) / 0.4, 0, 1))
        conf = float(np.clip((sep - 3.0) / 4.0, 0, 1) * (0.5 + 0.5 * fit) * agree)
        how = "motion" if use_flow[i] else "change"
        if q > jump_limit:
            events[i] = Event(i, "cut", ratio=q, conf=1.0,
                              note=f"{how} {q:.1f}x expected: cut or sudden change, left alone")
        elif kk - 1 > opt.max_insert:
            events[i] = Event(i, "long", count=kk - 1, ratio=q, conf=0.0,
                              note=f"{how} {q:.2f}x expected: gap too long to fill")
        else:
            note = f"{how} {q:.2f}x expected"
            if agree < 1:
                note += ", speed changes here"
            if not use_flow[i] and kk > 2:
                note += ", count uncertain"
                conf *= 0.6
            events[i] = Event(i, "missing", count=kk - 1, ratio=q, conf=conf, note=note)

    # repeat followed by a jump: the jump is the frame the repeat replaced
    for i in list(events):
        e = events[i]
        if e.kind == "repeat" and (i + 1) in events and events[i + 1].kind == "missing":
            events[i + 1].conf = max(events[i + 1].conf, min(1.0, e.conf + 0.2))
            events[i + 1].note += ", after a repeat"

    # --- periodic grid
    period = None
    if opt.periodic or opt.period:
        score = np.zeros(n)
        score[judge] = np.log(np.clip(ratio[judge], 0.25, 8.0))
        for i, e in events.items():
            if e.kind == "repeat":
                score[i] = np.log(2.0)
        if opt.period:
            P = opt.period
            ph = 1 + int(np.argmax([np.median(score[j::P]) for j in range(1, P + 1)]))
            cad = (P, ph, float(np.exp(np.median(score[ph::P]))))
        else:
            cad = _find_period(score) if n >= 12 else None
        if cad:
            P, ph, strength = cad
            grid = list(range(ph, n, P))
            evs = [i for i, e in events.items() if e.kind in ("missing", "repeat") and e.conf >= 0.3]
            on = set()
            for g in grid:
                for d in (-1, 0, 1):
                    if (g + d) in events and events[g + d].kind in ("missing", "repeat"):
                        on.add(g + d)
            hits = len(on)
            strong = (strength >= 1.25 and len(grid) >= 4 and hits >= max(4, 0.6 * len(grid))
                      and hits >= 0.6 * max(1, len(evs)))
            if strong or opt.period:
                period = {"P": P, "phase": ph, "strength": strength, "hits": hits, "size": len(grid)}
                g = ph
                while g < n:
                    lo, hi = max(1, g - 1), min(n - 1, g + 1)
                    ev_here = [j for j in range(lo, hi + 1)
                               if j in events and events[j].kind in ("missing", "repeat")]
                    cands = [j for j in range(lo, hi + 1) if valid[j] and pred[j] > 0
                             and ratio[j] >= opt.grid_ratio]
                    if ev_here:
                        j = ev_here[0]
                        events[j].grid = True
                        events[j].conf = max(events[j].conf, 0.9)
                        if events[j].kind == "repeat" and (j + 1) in events:
                            events[j + 1].grid = True
                            events[j + 1].conf = max(events[j + 1].conf, 0.9)
                    elif cands:
                        j = max(cands, key=lambda x: ratio[x])
                        q = float(ratio[j])
                        events[j] = Event(j, "missing", count=max(1, int(round(q)) - 1), ratio=q,
                                          conf=0.85, grid=True, note=f"step {q:.2f}x expected, on grid")
                    else:
                        j = g
                        if (opt.fill_static and 0 < j < n and valid[j] and pred[j] == 0
                                and j not in events):
                            events[j] = Event(j, "missing", count=1, ratio=1.0, conf=0.6, grid=True,
                                              note="still shot, filled to keep the grid")
                    g = j + P

    # a periodic source drops the same number of frames each time; weak
    # off-grid candidates are noise next to a clear grid
    if period:
        for i in [i for i, e in events.items()
                  if e.kind == "missing" and not e.grid and e.conf < 0.3]:
            del events[i]
        counts = [e.count for e in events.values() if e.grid and e.kind == "missing" and e.conf >= 0.5]
        if counts:
            mode = max(set(counts), key=counts.count)
            if counts.count(mode) >= 0.7 * len(counts):
                for e in events.values():
                    if e.grid and e.kind == "missing" and e.count != mode:
                        e.note += f", count {e.count} -> {mode} from grid"
                        e.count = mode

    out = sorted(events.values(), key=lambda e: e.frame)
    for e in out:
        e.apply = e.kind in ("missing", "repeat") and e.conf >= opt.min_conf
    stats = {"ratio": ratio, "pred": pred, "judge": judge, "period": period,
             "flow_ok": flow_ok}
    return out, stats


def analyze(path, opt, log=print, cancel=None, progress=None):
    resolve_tools(opt)
    info = probe(path, opt.ffprobe)
    fps = Fraction(info.get("r_frame_rate") or info["avg_frame_rate"])
    log(f"{os.path.basename(path)}")
    log(f"  {info['width']}x{info['height']}  {float(fps):.3f} fps  {info.get('pix_fmt')}")
    t0 = time.time()
    m = measure(path, info, opt, log, progress, cancel)
    n = len(m["diff"])
    log(f"  {n} frames analysed in {time.time() - t0:.0f} s")
    events, stats = classify(m, opt, fps)
    an = {"path": path, "info": info, "fps": fps, "n": n, "m": m, "events": events,
          "stats": stats, "ok": True}
    log("  " + summary(an))
    return an


def summary(an):
    ev = an["events"]
    miss = [e for e in ev if e.kind == "missing"]
    reps = [e for e in ev if e.kind == "repeat"]
    longs = [e for e in ev if e.kind == "long"]
    cuts = [e for e in ev if e.kind == "cut"]
    applied = [e for e in ev if e.apply]
    s = (f"{len(miss)} gaps ({sum(e.count for e in miss)} frames missing), {len(reps)} repeats"
         f"{f', {len(longs)} long gaps' if longs else ''}{f', {len(cuts)} cuts' if cuts else ''}"
         f"; {len(applied)} to apply, {len(ev) - len(applied) - len(cuts)} for review")
    per = an["stats"].get("period")
    if per:
        s += f"\n  periodic: every {per['P']} frames from frame {per['phase']} ({per['hits']}/{per['size']} on grid)"
    return s


def out_count(an):
    n = an["n"]
    for e in an["events"]:
        if e.apply:
            if e.kind == "missing":
                n += e.count
            elif e.kind == "repeat":
                n -= 1
    return n


def out_fps_for(an, opt):
    fps = an["fps"]
    mode = "keep" if opt.out_fps == "restore" else opt.out_fps
    if mode == "same":
        return fps
    if mode == "keep":
        return (fps * Fraction(out_count(an), an["n"])).limit_denominator(1001)
    return Fraction(mode).limit_denominator(1001)


def fps_label(f):
    v = float(f)
    return f"{int(round(v))}fps" if abs(v - round(v)) < 1e-6 else f"{v:.3f}fps"


def default_output(path, out_fps, out_dir=None):
    stem = os.path.splitext(os.path.basename(path))[0]
    d = out_dir or os.path.dirname(os.path.abspath(path))
    return os.path.join(d, f"{stem}_fixed_{fps_label(out_fps)}.mov")


def write_csv(an, csv_path):
    fps, tc = an["fps"], an["info"].get("timecode")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["frame", "timecode", "type", "frames", "ratio", "confidence", "grid", "apply", "note"])
        for e in an["events"]:
            w.writerow([e.frame, frames_to_tc(e.frame, fps, tc), e.kind, e.count, f"{e.ratio:.2f}",
                        f"{e.conf:.2f}", int(e.grid), int(e.apply), e.note])


def write_steps_csv(an, csv_path):
    m, st = an["m"], an["stats"]
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["frame", "diff", "speed", "expected", "ratio", "explain"])
        for i in range(an["n"]):
            w.writerow([i, f"{m['diff'][i]:.3f}", f"{m['speed'][i]:.3f}", f"{st['pred'][i]:.3f}",
                        f"{st['ratio'][i]:.3f}", f"{m['explain'][i]:.3f}"])


# ------------------------------------------------------------------ render

class Rife:
    def __init__(self, exe, model, gpu, tmp, log=print):
        if not exe or not os.path.isfile(exe):
            raise GapfillError(f"RIFE executable not found: {exe}")
        self.exe = exe
        base = os.path.dirname(os.path.abspath(exe))
        m = model if os.path.isdir(model) else os.path.join(base, model)
        if not os.path.isdir(m):
            raise GapfillError(f"RIFE model folder not found: {m}")
        self.model = m
        self.v4 = "v4" in os.path.basename(m)
        self.v4_alt = None
        if not self.v4:
            alts = sorted(glob.glob(os.path.join(base, "rife-v4*")))
            self.v4_alt = alts[-1] if alts else None
        self.gpu = gpu
        self.tmp = tmp
        self.log = log

    def _run(self, a8, b8, t, model):
        pa, pb, po = (os.path.join(self.tmp, x) for x in ("a.png", "b.png", "o.png"))
        Image.fromarray(a8).save(pa, compress_level=1)
        Image.fromarray(b8).save(pb, compress_level=1)
        if os.path.exists(po):
            os.remove(po)
        cmd = [self.exe, "-0", pa, "-1", pb, "-o", po, "-m", model]
        if abs(t - 0.5) > 1e-6:
            cmd += ["-s", f"{t:.6f}"]
        if self.gpu is not None:
            cmd += ["-g", str(self.gpu)]
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, **NOWIN)
        if res.returncode != 0 or not os.path.exists(po):
            raise GapfillError("RIFE failed:\n" + res.stderr.decode(errors="replace"))
        return np.asarray(Image.open(po).convert("RGB"), dtype=np.int32)

    def interp(self, a16, b16, t=0.5):
        a8 = (a16 >> 8).astype(np.uint8)
        b8 = (b16 >> 8).astype(np.uint8)
        model = self.model
        if abs(t - 0.5) > 1e-6 and not self.v4:
            if not self.v4_alt:
                raise GapfillError("this RIFE model only supports one frame per gap; use a v4 model")
            model = self.v4_alt
        o8 = self._run(a8, b8, t, model)
        # 16-bit precision: 16-bit blend + RIFE's 8-bit correction on top
        blend16 = (1 - t) * a16.astype(np.float32) + t * b16.astype(np.float32)
        blend8 = (1 - t) * a8.astype(np.float32) + t * b8.astype(np.float32)
        out = blend16 + (o8 - blend8) * 257.0
        return np.clip(np.round(out), 0, 65535).astype("<u2")


def render(an, out_path, opt, log=print, progress=None, cancel=None):
    resolve_tools(opt)
    info = an["info"]
    applied = {e.frame: e for e in an["events"] if e.apply and e.kind in ("missing", "repeat")}
    out_fps = out_fps_for(an, opt)
    W, H = int(info["width"]), int(info["height"])
    matrix, rng, prim, trc, cs = color_params(info)
    prof, pixfmt = {"hq": ("3", "yuv422p10le"),
                    "4444": ("4", "yuv444p10le"),
                    "422": ("2", "yuv422p10le")}[opt.prores]
    flags = "accurate_rnd+full_chroma_int"
    total = an["n"]
    n_ins = sum(e.count for e in applied.values() if e.kind == "missing")
    n_rep = sum(1 for e in applied.values() if e.kind == "repeat")
    log(f"  rendering -> {os.path.basename(out_path)}  {float(out_fps):.3f} fps, ProRes {opt.prores}, "
        f"+{n_ins} interpolated, -{n_rep} repeats, no audio")

    tmp = tempfile.mkdtemp(prefix="gapfill_")
    rife = Rife(opt.rife, opt.model, opt.gpu, tmp, log) if n_ins else None
    dec_err, enc_err = tempfile.TemporaryFile(), tempfile.TemporaryFile()
    dec = subprocess.Popen(
        [opt.ffmpeg, "-v", "error", "-i", an["path"], "-map", "0:v:0",
         "-fps_mode", "passthrough",
         "-vf", f"scale=in_color_matrix={matrix}:in_range={rng}:flags={flags},format=rgb48le",
         "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=dec_err, **NOWIN)
    enc = subprocess.Popen(
        [opt.ffmpeg, "-v", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb48le", "-s", f"{W}x{H}",
         "-r", str(out_fps), "-i", "-",
         "-vf", f"scale=out_color_matrix={matrix}:out_range=tv:flags={flags},format={pixfmt}",
         "-c:v", "prores_ks", "-profile:v", prof, "-vendor", "apl0",
         "-color_primaries", prim, "-color_trc", trc, "-colorspace", cs,
         out_path],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=enc_err, **NOWIN)

    prev = None            # last original frame written
    i = written = 0
    t0 = time.time()
    ok = False
    try:
        for buf in read_frames(dec.stdout, W * H * 6):
            if cancel and cancel():
                raise Cancelled()
            e = applied.get(i)
            if e and e.kind == "repeat" and prev is not None:
                i += 1
                if progress:
                    progress(i, total)
                continue
            cur = np.frombuffer(buf, "<u2").reshape(H, W, 3)
            if e and e.kind == "missing" and prev is not None:
                k = e.count + 1
                for j in range(1, k):
                    mid = rife.interp(prev, cur, j / k)
                    enc.stdin.write(np.ascontiguousarray(mid, "<u2").tobytes())
                    written += 1
            enc.stdin.write(buf)
            written += 1
            prev = cur
            i += 1
            if progress:
                progress(i, total)
            elif i % 50 == 0:
                print(f"  frame {i}/{total}  {i / (time.time() - t0):.1f} fps", end="\r")
        ok = True
    finally:
        if not ok:
            dec.kill()
        try:
            enc.stdin.close()
        except OSError:
            pass
        enc.wait()
        dec.wait()
        shutil.rmtree(tmp, ignore_errors=True)
        if not ok and os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass
    if dec.returncode != 0:
        raise GapfillError("ffmpeg decode failed: " + _err_text(dec_err))
    if enc.returncode != 0:
        raise GapfillError("ffmpeg encode failed: " + _err_text(enc_err))
    log(f"  wrote {written} frames from {i} source frames in {time.time() - t0:.0f} s")
    return out_path


def extract_frame(path, n, fps, ffmpeg, width=320):
    """One frame as an RGB numpy array (for GUI thumbnails)."""
    t = (n - 0.25) / float(fps)          # just before frame n: accurate seek outputs frame n
    cmd = [ffmpeg, "-v", "error", "-ss", f"{max(0.0, t):.6f}", "-i", path, "-map", "0:v:0",
           "-frames:v", "1", "-vf", f"scale={width}:-2,format=rgb24", "-f", "rawvideo", "-"]
    res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, **NOWIN)
    if res.returncode != 0 or not res.stdout:
        return None
    h = len(res.stdout) // (width * 3)
    return np.frombuffer(res.stdout[: width * 3 * h], np.uint8).reshape(h, width, 3)


# --------------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=f"rife_gapfill v{VERSION}")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", help="output .mov (default <name>_fixed_<fps>.mov)")
    ap.add_argument("--rife", help="path to rife-ncnn-vulkan executable (auto if omitted)")
    ap.add_argument("--model", default="rife-v4.6")
    ap.add_argument("--gpu", type=int)
    ap.add_argument("--prores", choices=["422", "hq", "4444"], default="hq")
    ap.add_argument("--out-fps", default="same",
                    help="'same' (default), 'keep' (keep duration) or a number")
    ap.add_argument("--analyze-only", action="store_true")
    ap.add_argument("--csv", help="write the event list to this CSV")
    ap.add_argument("--steps-csv", help="write per-step measurements to this CSV")
    ap.add_argument("--min-conf", type=float, default=0.5, help="apply events at or above (0-1)")
    ap.add_argument("--all", action="store_true", help="apply every missing/repeat event")
    ap.add_argument("--gap-ratio", type=float, default=1.5)
    ap.add_argument("--max-insert", type=int, default=3)
    ap.add_argument("--no-repeats", action="store_true")
    ap.add_argument("--no-periodic", action="store_true")
    ap.add_argument("--no-fill-static", action="store_true")
    ap.add_argument("--period", type=int)
    ap.add_argument("--analysis-width", type=int, default=480)
    ap.add_argument("--ffmpeg", default="")
    ap.add_argument("--ffprobe", default="")
    a = ap.parse_args()

    opt = Options(ffmpeg=a.ffmpeg, ffprobe=a.ffprobe, rife=a.rife or find_tool("rife-ncnn-vulkan"),
                  model=a.model, gpu=a.gpu, prores=a.prores, out_fps=a.out_fps,
                  min_conf=0.0 if a.all else a.min_conf, gap_ratio=a.gap_ratio,
                  max_insert=a.max_insert, detect_repeats=not a.no_repeats,
                  periodic=not a.no_periodic, fill_static=not a.no_fill_static,
                  period=a.period, analysis_width=a.analysis_width)
    print(f"rife_gapfill v{VERSION}")
    try:
        an = analyze(a.input, opt)
        fps, tc = an["fps"], an["info"].get("timecode")
        for e in an["events"]:
            print(f"   {'*' if e.apply else ' '} {e.frame:6d}  {frames_to_tc(e.frame, fps, tc)}  "
                  f"{e.label():24s} conf {e.conf:.2f}  {e.note}")
        print("   (* = applied)")
        if a.csv or a.analyze_only:
            p = a.csv or os.path.splitext(a.input)[0] + "_events.csv"
            write_csv(an, p)
            print(f"  events written to {p}")
        if a.steps_csv:
            write_steps_csv(an, a.steps_csv)
        if a.analyze_only:
            return
        out = a.output or default_output(a.input, out_fps_for(an, opt))
        render(an, out, opt)
        print("  done")
    except GapfillError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
