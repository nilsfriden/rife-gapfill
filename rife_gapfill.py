#!/usr/bin/env python3
"""
rife_gapfill.py  v1.1

Finds periodically missing frames in a video (e.g. one dropped frame per
generated block in AI video) and fills each gap with a RIFE-interpolated frame.
Usable as a command-line tool and as a module (used by gapfill_gui.py).

How it works
  1. Analysis: decodes a downscaled grey version and measures the mean absolute
     difference between consecutive frames (same idea as a 1-frame offset
     difference layer). Each value is compared with its local median -> "ratio".
  2. Cadence: tests every period/phase combination and picks the grid where the
     ratio is consistently high. Works at any frame rate and any period.
  3. Tracking: walks the grid and snaps each gap to the strongest spike within
     +/- snap frames, so slow drift is followed.
  4. Render: decodes full-quality RGB48, inserts one RIFE frame before each gap
     frame and encodes ProRes. Original frames pass through untouched.

Requirements
  - ffmpeg + ffprobe (found automatically on PATH, next to the script/exe, or
    in the WinGet package folder; or pass --ffmpeg / --ffprobe)
  - Python 3.9+, numpy, pillow   (pip install numpy pillow)
  - rife-ncnn-vulkan: https://github.com/nihui/rife-ncnn-vulkan/releases

Changes
  1.1  Refactored into importable functions for the GUI; auto-detects ffmpeg
       and RIFE; output name includes frame rate (no silent overwrite of a
       different-fps version); no console windows for child processes on
       Windows; cancel support.
  1.0  First version.
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
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable, Optional

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from PIL import Image

VERSION = "1.1"

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
    out_fps: str = "same"         # same | restore | number
    period: Optional[int] = None
    min_period: int = 3
    max_period: int = 240
    window: int = 9
    threshold: float = 1.5
    min_strength: float = 1.3
    snap: int = 1
    spikes_only: bool = False
    cut_ratio: float = 8.0
    force: bool = False
    analysis_width: int = 320


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
           "color_primaries,color_transfer,color_space,color_range",
           "-of", "json", path]
    res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, **NOWIN)
    if res.returncode != 0:
        raise GapfillError("ffprobe failed: " + res.stderr.decode(errors="replace"))
    streams = json.loads(res.stdout).get("streams") or []
    if not streams:
        raise GapfillError("no video stream found")
    return streams[0]


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


# ---------------------------------------------------------------- analysis

def frame_diffs(path, info, opt, cancel=None):
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
    diffs = [0.0]
    prev = None
    try:
        for buf in read_frames(p.stdout, aw * ah):
            if cancel and cancel():
                p.kill()
                raise Cancelled()
            f = np.frombuffer(buf, np.uint8).astype(np.int16)
            if prev is not None:
                diffs.append(float(np.abs(f - prev).mean()))
            prev = f
    finally:
        p.wait()
    if p.returncode != 0:
        raise GapfillError("ffmpeg failed during analysis: " + _err_text(errf))
    return np.array(diffs)


def local_ratio(d, win):
    """Diff divided by the median of its neighbours (centre excluded)."""
    dd = d.copy()
    if len(dd) > 1:
        dd[0] = dd[1]
    half = win // 2
    pad = np.pad(dd, (half, half), mode="edge")
    w = np.delete(sliding_window_view(pad, win), half, axis=1)
    med = np.median(w, axis=1)
    floor = max(float(np.median(dd)) * 0.05, 0.02)
    r = dd / np.maximum(med, floor)
    r[0] = 1.0
    return r


def find_cadence(r, pmin, pmax):
    """Return (period, phase, strength). strength = typical ratio on the grid."""
    n = len(r)
    lr = np.log(np.clip(r, 0.25, 50.0))
    per_period = []
    for P in range(pmin, min(pmax, n // 3) + 1):
        best_s, best_ph = -9.0, 1
        for ph in range(1, P + 1):
            s = float(np.median(lr[ph::P]))
            if s > best_s:
                best_s, best_ph = s, ph
        per_period.append((P, best_ph, best_s))
    if not per_period:
        return None
    top = max(s for _, _, s in per_period)
    for P, ph, s in per_period:          # smallest period nearly as good
        if s >= top * 0.85:
            return P, ph, float(np.exp(s))
    return None


def track_gaps(r, P, ph, snap, weak):
    gaps = []
    n = len(r)
    g = ph
    while g < n:
        lo, hi = max(1, g - snap), min(n - 1, g + snap)
        j = lo + int(np.argmax(r[lo:hi + 1]))
        gs = j if r[j] >= weak else g
        gaps.append(gs)
        g = gs + P
    return gaps


def analyze(path, opt, log=print, cancel=None):
    resolve_tools(opt)
    info = probe(path, opt.ffprobe)
    fps = Fraction(info.get("r_frame_rate") or info["avg_frame_rate"])
    log(f"{os.path.basename(path)}")
    log(f"  {info['width']}x{info['height']}  {float(fps):.3f} fps  {info.get('pix_fmt')}")
    d = frame_diffs(path, info, opt, cancel)
    r = local_ratio(d, opt.window | 1)
    log(f"  {len(d)} frames analysed")

    if opt.period:
        P = opt.period
        lr = np.log(np.clip(r, 0.25, 50.0))
        ph = 1 + int(np.argmax([np.median(lr[k::P]) for k in range(1, P + 1)]))
        strength = float(np.exp(np.median(lr[ph::P])))
    else:
        cad = find_cadence(r, opt.min_period, opt.max_period)
        if cad is None:
            raise GapfillError("too few frames to detect a cadence")
        P, ph, strength = cad

    an = {"path": path, "info": info, "fps": fps, "d": d, "r": r,
          "P": P, "ph": ph, "strength": strength, "gaps": [], "cuts": [],
          "ok": False}
    log(f"  cadence: every {P} frames, first gap before frame {ph}, "
        f"spike {strength:.2f}x local motion")
    if strength < opt.min_strength and not opt.force:
        log("  no clear cadence found (set a period or enable force to override)")
        return an

    gaps = track_gaps(r, P, ph, opt.snap, weak=1.2)
    if opt.spikes_only:
        gaps = [g for g in gaps if r[g] >= opt.threshold]
    cuts = [g for g in gaps if r[g] >= opt.cut_ratio]
    clear = sum(1 for g in gaps if r[g] >= opt.threshold)
    intervals = np.diff(gaps) if len(gaps) > 1 else np.array([])
    drift = int(np.sum(intervals != P)) if len(intervals) else 0
    log(f"  gaps: {len(gaps)}  (clear {clear}, grid-filled {len(gaps) - clear}, "
        f"cuts {len(cuts)}, drift corrections {drift})")
    an.update(gaps=gaps, cuts=cuts, ok=True)
    return an


def out_fps_for(an, opt):
    fps, P = an["fps"], an["P"]
    if opt.out_fps == "same":
        return fps
    if opt.out_fps == "restore":
        return fps * Fraction(P + 1, P)
    return Fraction(opt.out_fps).limit_denominator(1001)


def fps_label(f):
    v = float(f)
    return f"{int(round(v))}fps" if abs(v - round(v)) < 1e-6 else f"{v:.3f}fps"


def default_output(path, out_fps, out_dir=None):
    stem = os.path.splitext(os.path.basename(path))[0]
    d = out_dir or os.path.dirname(os.path.abspath(path))
    return os.path.join(d, f"{stem}_filled_{fps_label(out_fps)}.mov")


def write_csv(an, csv_path):
    d, r = an["d"], an["r"]
    gs, cs = set(an["gaps"]), set(an["cuts"])
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["frame", "diff", "ratio", "gap", "cut"])
        for i in range(len(d)):
            w.writerow([i, f"{d[i]:.4f}", f"{r[i]:.3f}", int(i in gs), int(i in cs)])


# ------------------------------------------------------------------ render

class Rife:
    def __init__(self, exe, model, gpu, tmp):
        if not exe or not os.path.isfile(exe):
            raise GapfillError(f"RIFE executable not found: {exe}")
        self.exe = exe
        m = model
        if not os.path.isdir(m):
            m = os.path.join(os.path.dirname(os.path.abspath(exe)), model)
        if not os.path.isdir(m):
            raise GapfillError(f"RIFE model folder not found: {m}")
        self.model = m
        self.gpu = gpu
        self.tmp = tmp

    def interp(self, a16, b16):
        a8 = (a16 >> 8).astype(np.uint8)
        b8 = (b16 >> 8).astype(np.uint8)
        pa, pb, po = (os.path.join(self.tmp, x) for x in ("a.png", "b.png", "o.png"))
        Image.fromarray(a8).save(pa, compress_level=1)
        Image.fromarray(b8).save(pb, compress_level=1)
        if os.path.exists(po):
            os.remove(po)
        cmd = [self.exe, "-0", pa, "-1", pb, "-o", po, "-m", self.model]
        if self.gpu is not None:
            cmd += ["-g", str(self.gpu)]
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, **NOWIN)
        if res.returncode != 0 or not os.path.exists(po):
            raise GapfillError("RIFE failed:\n" + res.stderr.decode(errors="replace"))
        o8 = np.asarray(Image.open(po).convert("RGB"), dtype=np.int32)
        # Keep 16-bit precision: 16-bit blend + RIFE's 8-bit correction on top.
        blend16 = (a16.astype(np.int32) + b16.astype(np.int32)) // 2
        blend8 = (a8.astype(np.int32) + b8.astype(np.int32)) // 2
        out = blend16 + (o8 - blend8) * 257
        return np.clip(out, 0, 65535).astype("<u2")


def render(an, out_path, opt, log=print, progress=None, cancel=None):
    if not an.get("ok"):
        raise GapfillError("analysis found no usable cadence")
    resolve_tools(opt)
    info, gaps, cuts = an["info"], an["gaps"], an["cuts"]
    out_fps = out_fps_for(an, opt)
    W, H = int(info["width"]), int(info["height"])
    matrix, rng, prim, trc, cs = color_params(info)
    prof, pixfmt = {"hq": ("3", "yuv422p10le"),
                    "4444": ("4", "yuv444p10le"),
                    "422": ("2", "yuv422p10le")}[opt.prores]
    flags = "accurate_rnd+full_chroma_int"
    total = len(an["d"])
    log(f"  rendering -> {os.path.basename(out_path)}  "
        f"{float(out_fps):.3f} fps, ProRes {opt.prores}, no audio")

    tmp = tempfile.mkdtemp(prefix="gapfill_")
    rife = Rife(opt.rife, opt.model, opt.gpu, tmp)
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

    gapset, cutset = set(gaps), set(cuts)
    prev, i, done = None, 0, 0
    t0 = time.time()
    ok = False
    try:
        for buf in read_frames(dec.stdout, W * H * 6):
            if cancel and cancel():
                raise Cancelled()
            cur = np.frombuffer(buf, "<u2").reshape(H, W, 3)
            if i in gapset and prev is not None:
                mid = prev if i in cutset else rife.interp(prev, cur)
                enc.stdin.write(np.ascontiguousarray(mid, "<u2").tobytes())
                done += 1
            enc.stdin.write(buf)
            prev = cur
            i += 1
            if progress:
                progress(i, total)
            elif i % 100 == 0:
                print(f"  frame {i}/{total}  inserted {done}/{len(gaps)}  "
                      f"{i / (time.time() - t0):.1f} fps", end="\r")
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
    log(f"  wrote {i + done} frames ({i} original + {done} inserted) "
        f"in {time.time() - t0:.0f} s")
    return out_path


# --------------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=f"rife_gapfill v{VERSION}")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", help="output .mov (default <name>_filled_<fps>.mov)")
    ap.add_argument("--rife", help="path to rife-ncnn-vulkan executable (auto if omitted)")
    ap.add_argument("--model", default="rife-v4.6")
    ap.add_argument("--gpu", type=int)
    ap.add_argument("--prores", choices=["422", "hq", "4444"], default="hq")
    ap.add_argument("--out-fps", default="same",
                    help="'same' (default, longer clip), 'restore' (original "
                         "duration, fps*(P+1)/P) or a number")
    ap.add_argument("--analyze-only", action="store_true")
    ap.add_argument("--csv", help="write analysis CSV to this path")
    ap.add_argument("--period", type=int)
    ap.add_argument("--min-period", type=int, default=3)
    ap.add_argument("--max-period", type=int, default=240)
    ap.add_argument("--window", type=int, default=9)
    ap.add_argument("--threshold", type=float, default=1.5)
    ap.add_argument("--min-strength", type=float, default=1.3)
    ap.add_argument("--snap", type=int, default=1)
    ap.add_argument("--spikes-only", action="store_true")
    ap.add_argument("--cut-ratio", type=float, default=8.0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--analysis-width", type=int, default=320)
    ap.add_argument("--ffmpeg", default="")
    ap.add_argument("--ffprobe", default="")
    a = ap.parse_args()

    opt = Options(ffmpeg=a.ffmpeg, ffprobe=a.ffprobe, rife=a.rife or find_tool("rife-ncnn-vulkan"),
                  model=a.model, gpu=a.gpu, prores=a.prores, out_fps=a.out_fps,
                  period=a.period, min_period=a.min_period, max_period=a.max_period,
                  window=a.window, threshold=a.threshold, min_strength=a.min_strength,
                  snap=a.snap, spikes_only=a.spikes_only, cut_ratio=a.cut_ratio,
                  force=a.force, analysis_width=a.analysis_width)
    print(f"rife_gapfill v{VERSION}")
    try:
        an = analyze(a.input, opt)
        if a.csv or a.analyze_only:
            p = a.csv or os.path.splitext(a.input)[0] + "_analysis.csv"
            write_csv(an, p)
            print(f"  analysis written to {p}")
        if a.analyze_only:
            return
        if not an["ok"]:
            sys.exit(1)
        out = a.output or default_output(a.input, out_fps_for(an, opt))
        render(an, out, opt)
        print("  done")
    except GapfillError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
