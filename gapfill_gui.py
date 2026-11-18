#!/usr/bin/env python3
"""
gapfill_gui.py  v2.1  -  GUI for rife_gapfill (core v2.0+)

Finds missing and repeated frames, lets you review them, and renders an even
cadence with missing frames interpolated by RIFE.

Build a Windows exe (run in the folder containing both .py files):
    pip install pyinstaller numpy pillow opencv-python-headless
    pyinstaller --onefile --windowed --name RifeGapfill gapfill_gui.py
The exe ends up in dist\\RifeGapfill.exe. Put it next to the
rife-ncnn-vulkan folder (or use Download… in the GUI). Settings are saved in
%APPDATA%\\RifeGapfill\\settings.json.

Changes
  2.1  Download button for ffmpeg (Gyan's release essentials build; only
       ffmpeg.exe and ffprobe.exe are kept). One startup offer for whichever
       tools are missing. Tools are also found when running from dist.
  2.0  Review panel: motion timeline, event list with tick boxes, thumbnails
       before/after each event; repeats and random gaps; detection settings
       for the v2 core; re-classify from cached measurements; two-column
       layout.
  1.3  Help text shown in a fixed panel inside the window.
  1.2  Dark theme in Nils-Player style, dark title bar, high-DPI aware.
  1.1  Download button for rife-ncnn-vulkan.
  1.0  First version.
"""

import base64
import glob
import io
import json
import os
import queue
import threading
import urllib.request
import zipfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image

import rife_gapfill as core

GUI_VERSION = "2.1"

RIFE_API = "https://api.github.com/repos/nihui/rife-ncnn-vulkan/releases/latest"
RIFE_FALLBACK = ("https://github.com/nihui/rife-ncnn-vulkan/releases/download/"
                 "20221029/rife-ncnn-vulkan-20221029-windows.zip")
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_MIRROR_API = "https://api.github.com/repos/GyanD/codexffmpeg/releases/latest"
UA = {"User-Agent": f"RifeGapfill/{GUI_VERSION}"}

FPS_CHOICES = {
    "Same as source (even cadence)": "same",
    "Keep original duration": "keep",
    "Custom": "custom",
}
PRORES_CHOICES = {"ProRes 422": "422", "ProRes 422 HQ": "hq", "ProRes 4444": "4444"}

# ----------------------------------------------------------- Nils-Player palette
BG = "#161616"       # bars / window
PANEL = "#101010"    # fields, log, lists
SEP = "#2C2C2C"      # borders
BTN = "#222222"
HOVER = "#303030"
TRACK = "#3A3A3A"
TEXT = "#E6E6E6"
DIM = "#8A8A8A"
DIMMER = "#5A5A5A"
ACCENT = "#FF9B2E"   # orange: missing frames
ACCENT_HI = "#FFB25C"
REPEAT = "#4FB3FF"   # blue: repeated frames
ERR = "#FF6B5A"
FONT_FAMILY = "Consolas"
FONT = (FONT_FAMILY, 9)
FONT_B = (FONT_FAMILY, 9, "bold")
THUMB_W = 240


HELP = {
    "add": "Add one or more video files. All clips in the list are analysed and rendered with the same settings.",
    "remove": "Remove the selected clips from the list.",
    "clear": "Empty the list.",
    "files": "Clips to process. Select a clip to show its events in the review panel on the right.",
    "out_source": "Write each result in the same folder as its source clip.",
    "out_folder": "Write all results to the folder next to this option.",
    "outdir": "Output folder used when 'Folder' is selected.",
    "fps": ("Frame rate of the output file.\n"
            "Same as source: an even cadence at the source rate. Missing frames make the clip longer, "
            "dropped repeats make it shorter.\n"
            "Keep original duration: the rate is adjusted so the duration stays the same, "
            "e.g. 24 -> 25 fps when one frame in 24 was missing.\n"
            "Custom: any rate you type."),
    "fps_custom": "Output frame rate for 'Custom', e.g. 24, 25 or 23.976.",
    "codec": ("ProRes 422: smaller files. ProRes 422 HQ: standard for editing and grading. "
              "ProRes 4444: full chroma resolution, largest files. Audio is not included."),
    "rife": "Path to rife-ncnn-vulkan.exe. Found automatically when it sits next to this program.",
    "download": "Download the latest rife-ncnn-vulkan for Windows from GitHub (nihui/rife-ncnn-vulkan) and set it up. A few hundred MB.",
    "model": "RIFE model. rife-v4.6 is the newest and usually the best. A v4 model is needed to fill gaps of more than one frame.",
    "ffmpeg": "Path to ffmpeg.exe. ffprobe.exe must be in the same folder. Found automatically on PATH, in the WinGet install folder, or where Download… put it.",
    "download_ffmpeg": ("Download ffmpeg and ffprobe (Gyan's release essentials build for Windows, about 110 MB) and set them up. "
                        "Only ffmpeg.exe and ffprobe.exe are kept, in an 'ffmpeg' folder next to this program."),
    "browse": "Choose the file or folder.",
    "gap_ratio": ("How much longer than expected a frame step must be to count as a gap. 2.0 means exactly one "
                  "frame missing; the default 1.5 sits halfway. Lower finds more but risks false hits."),
    "min_conf": ("Events with at least this confidence (0-1) are ticked automatically. The rest are listed "
                 "unticked for you to check. Default 0.5."),
    "max_insert": "Longest gap that is filled, in frames. Longer gaps are listed but left alone: interpolation over many frames rarely looks right.",
    "repeats": "Find repeated frames (a frame shown twice) and drop them. Only judged where there is motion on both sides; in a still shot a repeat is invisible.",
    "periodic": ("Look for a regular pattern, e.g. one frame missing every 24 in AI video. When one is found, "
                 "borderline steps on the grid are accepted too."),
    "fill_static": "On a periodic grid, also insert frames in still shots, where the gap can't be measured, so the timing stays even.",
    "period": "Leave blank to detect the pattern automatically. Type a number to force a known period, e.g. 24.",
    "analyze": ("Measure motion in every clip and list the events. Changing the detection settings and clicking "
                "Analyze again reuses the measurements, so it is instant."),
    "render": "Render every clip with the ticked events: repeats dropped, missing frames interpolated, ProRes at an even cadence. Clips not yet analysed are analysed first.",
    "cancel": "Stop the current job. A half-written output file is deleted.",
    "log": "Results and progress for each clip.",
    "timeline": ("Motion of each frame step compared with what was expected (1 = normal). Orange marks: missing frames. "
                 "Blue marks: repeated frames. Grey: cuts. The dashed line is the gap threshold. Click to jump to the nearest event."),
    "events": ("Events in the selected clip. Click the first column to tick or untick an event; ticked events are "
               "applied when rendering. Select a row to see the frames around it. Space toggles the selected rows."),
    "all": "Tick every missing-frame and repeat event in this clip.",
    "none": "Untick every event in this clip.",
    "auto": "Tick events at or above the auto-apply confidence, untick the rest.",
    "thumbs": ("Frames around the selected event, in source order. For a gap, motion jumps between the middle and right "
               "frames. For a repeat, the middle frame is the same as the left one."),
}


# ------------------------------------------------------------------ tool downloads

def exe_name(name):
    return name + (".exe" if os.name == "nt" else "")


def find_rife():
    return core.find_tool("rife-ncnn-vulkan")


def download_dir():
    """Next to the exe/script if writable, else %LOCALAPPDATA%\\RifeGapfill."""
    d = core.app_dir()
    try:
        probe = os.path.join(d, ".write_test")
        with open(probe, "w"):
            pass
        os.remove(probe)
        return d
    except OSError:
        os.makedirs(core.tools_dir(), exist_ok=True)
        return core.tools_dir()


def github_asset(api, suffix):
    """(name, url, size) of the first asset ending with suffix in a GitHub release, or None."""
    try:
        req = urllib.request.Request(api, headers={**UA, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            rel = json.load(r)
        for a in rel.get("assets", []):
            if a["name"].endswith(suffix):
                return a["name"], a["browser_download_url"], int(a.get("size") or 0)
    except Exception:
        pass
    return None


def fetch(url, path, log, progress, cancel, size=0):
    """Download url to path with progress; returns bytes downloaded."""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
        total = int(r.headers.get("Content-Length") or size or 0)
        got = 0
        while True:
            if cancel():
                raise core.Cancelled()
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            progress(got, total or got)
    log(f"  {got / 1e6:.0f} MB downloaded")
    return got


def safe_members(z, dest):
    """Zip members, refusing any path that would land outside dest."""
    root = os.path.realpath(dest)
    for m in z.infolist():
        target = os.path.realpath(os.path.join(dest, m.filename))
        if not (target == root or target.startswith(root + os.sep)):
            raise core.GapfillError(f"unsafe path in zip: {m.filename}")
    return z.infolist()


def download_rife(dest, log, progress, cancel):
    found = github_asset(RIFE_API, "-windows.zip")
    name, url, size = found or (os.path.basename(RIFE_FALLBACK), RIFE_FALLBACK, 0)
    log(f"Downloading {name}")
    log(f"  from {url}")
    part = os.path.join(dest, name + ".part")
    try:
        fetch(url, part, log, progress, cancel, size)
        log("  extracting ...")
        with zipfile.ZipFile(part) as z:
            safe_members(z, dest)
            z.extractall(dest)
    finally:
        if os.path.exists(part):
            os.remove(part)
    hits = sorted(glob.glob(os.path.join(dest, "rife-ncnn-vulkan*", exe_name("rife-ncnn-vulkan"))))
    if not hits:
        raise core.GapfillError("download finished but rife-ncnn-vulkan.exe was not found")
    log(f"  RIFE ready: {hits[-1]}")
    return hits[-1]


def download_ffmpeg(dest, log, progress, cancel):
    """Gyan's release essentials build; only ffmpeg.exe and ffprobe.exe are kept,
    in <dest>\\ffmpeg\\."""
    sources = [("ffmpeg-release-essentials.zip", FFMPEG_URL, 0)]
    mirror = github_asset(FFMPEG_MIRROR_API, "essentials_build.zip")
    if mirror:
        sources.append(mirror)
    out_dir = os.path.join(dest, "ffmpeg")
    last = None
    for name, url, size in sources:
        log(f"Downloading {name}")
        log(f"  from {url}")
        part = os.path.join(dest, "ffmpeg-download.zip.part")
        try:
            fetch(url, part, log, progress, cancel, size)
            log("  extracting ffmpeg.exe and ffprobe.exe ...")
            os.makedirs(out_dir, exist_ok=True)
            wanted = {exe_name("ffmpeg"), exe_name("ffprobe"), "LICENSE", "LICENSE.txt"}
            got = set()
            with zipfile.ZipFile(part) as z:
                for m in safe_members(z, dest):
                    base = os.path.basename(m.filename)
                    parent = os.path.basename(os.path.dirname(m.filename))
                    if base in wanted and (parent == "bin" or base.startswith("LICENSE")) and not m.is_dir():
                        with z.open(m) as src, open(os.path.join(out_dir, base), "wb") as dst:
                            while True:
                                b = src.read(1 << 20)
                                if not b:
                                    break
                                dst.write(b)
                        got.add(base)
            if exe_name("ffmpeg") in got and exe_name("ffprobe") in got:
                path = os.path.join(out_dir, exe_name("ffmpeg"))
                log(f"  ffmpeg ready: {path}")
                return path
            last = core.GapfillError("ffmpeg.exe / ffprobe.exe not found in the download")
        except core.Cancelled:
            raise
        except Exception as e:
            last = e
            log(f"  failed: {e}")
        finally:
            if os.path.exists(part):
                os.remove(part)
    raise core.GapfillError(f"ffmpeg download failed: {last}")


# ------------------------------------------------------------------ settings

def settings_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "RifeGapfill")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "settings.json")


# ------------------------------------------------------------------ theme

def set_dpi_awareness():
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def dark_titlebar(win):
    if os.name != "nt":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (new / old builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception:
        pass


def apply_theme(root):
    global FONT_FAMILY, FONT, FONT_B
    import tkinter.font as tkfont
    if FONT_FAMILY not in tkfont.families(root):
        FONT_FAMILY = "Courier New" if os.name == "nt" else "DejaVu Sans Mono"
        FONT, FONT_B = (FONT_FAMILY, 9), (FONT_FAMILY, 9, "bold")

    root.configure(bg=BG)
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL,
                 bordercolor=SEP, lightcolor=BG, darkcolor=BG, troughcolor=TRACK,
                 focuscolor=BG, selectbackground=ACCENT, selectforeground="#111111",
                 insertcolor=TEXT, font=FONT, relief="flat")
    st.configure("TFrame", background=BG)
    st.configure("TLabel", background=BG, foreground=TEXT, font=FONT)
    st.configure("Dim.TLabel", foreground=DIM)
    st.configure("Title.TLabel", foreground=ACCENT, font=(FONT_FAMILY, 13, "bold"))
    st.configure("Ver.TLabel", foreground=DIMMER)
    st.configure("Status.TLabel", foreground=DIM)
    st.configure("Section.TLabel", foreground=DIM, font=FONT_B)
    st.configure("Sep.TFrame", background=SEP)

    st.configure("TButton", background=BTN, foreground=TEXT, bordercolor=SEP,
                 lightcolor=BTN, darkcolor=BTN, padding=(10, 3), font=FONT)
    st.map("TButton",
           background=[("disabled", BG), ("pressed", TRACK), ("active", HOVER)],
           foreground=[("disabled", DIMMER)],
           bordercolor=[("disabled", SEP), ("active", ACCENT)],
           lightcolor=[("active", HOVER)], darkcolor=[("active", HOVER)])
    st.configure("Accent.TButton", background=ACCENT, foreground="#111111",
                 bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT, font=FONT_B)
    st.map("Accent.TButton",
           background=[("disabled", BTN), ("pressed", "#E0841F"), ("active", ACCENT_HI)],
           foreground=[("disabled", DIMMER)],
           bordercolor=[("disabled", SEP), ("active", ACCENT_HI)],
           lightcolor=[("active", ACCENT_HI)], darkcolor=[("active", ACCENT_HI)])

    st.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, bordercolor=SEP,
                 lightcolor=PANEL, darkcolor=PANEL, insertcolor=TEXT, padding=(4, 2))
    st.map("TEntry", bordercolor=[("focus", ACCENT), ("hover", TRACK)],
           lightcolor=[("focus", PANEL)],
           fieldbackground=[("disabled", BG)], foreground=[("disabled", DIMMER)])

    st.configure("TCombobox", fieldbackground=PANEL, background=BTN, foreground=TEXT,
                 arrowcolor=TEXT, bordercolor=SEP, lightcolor=PANEL, darkcolor=PANEL,
                 padding=(4, 2), arrowsize=12)
    st.map("TCombobox",
           fieldbackground=[("readonly", PANEL), ("disabled", BG)],
           foreground=[("readonly", TEXT), ("disabled", DIMMER)],
           selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", TEXT)],
           background=[("active", HOVER), ("pressed", TRACK)],
           bordercolor=[("focus", ACCENT), ("hover", TRACK)],
           arrowcolor=[("disabled", DIMMER), ("active", ACCENT)])
    root.option_add("*TCombobox*Listbox.background", PANEL)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#111111")
    root.option_add("*TCombobox*Listbox.font", FONT)
    root.option_add("*TCombobox*Listbox.borderWidth", 0)

    for w in ("TCheckbutton", "TRadiobutton"):
        st.configure(w, background=BG, foreground=TEXT, indicatorbackground=PANEL,
                     indicatorforeground=ACCENT, upperbordercolor=TRACK,
                     lowerbordercolor=TRACK, indicatormargin=(0, 0, 6, 0), font=FONT)
        st.map(w, background=[("active", BG)], foreground=[("active", ACCENT_HI), ("disabled", DIMMER)],
               indicatorbackground=[("pressed", HOVER), ("selected", PANEL), ("active", HOVER)],
               indicatorforeground=[("selected", ACCENT)],
               upperbordercolor=[("active", ACCENT)], lowerbordercolor=[("active", ACCENT)])

    st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL,
                 bordercolor=SEP, lightcolor=ACCENT, darkcolor=ACCENT, thickness=6)
    st.configure("Vertical.TScrollbar", background=BTN, troughcolor=PANEL, bordercolor=PANEL,
                 lightcolor=BTN, darkcolor=BTN, arrowcolor=DIM, gripcount=0, arrowsize=11)
    st.map("Vertical.TScrollbar", background=[("active", HOVER)], arrowcolor=[("active", ACCENT)])


class HelpBar:
    """Fixed help panel inside the window. Shows the text for the control
    under the mouse; falls back to a short hint."""

    DEFAULT = "Hover over a control to see what it does."

    def __init__(self, parent):
        self.frame = tk.Frame(parent, bg=ACCENT)                 # orange left edge
        inner = tk.Frame(self.frame, bg=PANEL, highlightthickness=1, highlightbackground=SEP)
        inner.pack(fill="both", expand=True, padx=(2, 0))
        self.label = tk.Label(inner, text=self.DEFAULT, justify="left", anchor="nw",
                              bg=PANEL, fg=DIM, font=FONT, padx=10, pady=6, height=5)
        self.label.pack(fill="both", expand=True)
        self.label.bind("<Configure>", lambda e: self.label.configure(wraplength=max(200, e.width - 24)))
        self.job = None

    def attach(self, widget, key):
        text = HELP.get(key, key).replace("\n\n", "\n")
        widget.bind("<Enter>", lambda e: self.show(text), add="+")
        widget.bind("<Leave>", lambda e: self.schedule_reset(), add="+")

    def show(self, text):
        if self.job:
            self.label.after_cancel(self.job)
            self.job = None
        self.label.configure(text=text, fg=TEXT)

    def schedule_reset(self):
        if self.job:
            self.label.after_cancel(self.job)
        self.job = self.label.after(150, self.reset)

    def reset(self):
        self.job = None
        self.label.configure(text=self.DEFAULT, fg=DIM)


_help_bar = None


def tip(widget, key):
    if _help_bar:
        _help_bar.attach(widget, key)
    return widget


def extra_theme(root):
    st = ttk.Style(root)
    st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                 bordercolor=SEP, lightcolor=PANEL, darkcolor=PANEL, rowheight=20, font=FONT)
    st.map("Treeview", background=[("selected", "#3A2A18")], foreground=[("selected", TEXT)])
    st.configure("Treeview.Heading", background=BG, foreground=DIM, bordercolor=SEP,
                 lightcolor=BG, darkcolor=BG, relief="flat", font=FONT_B, padding=(4, 3))
    st.map("Treeview.Heading", background=[("active", HOVER)], foreground=[("active", TEXT)])
    st.configure("Small.TButton", padding=(8, 1))
    st.configure("Clip.TLabel", foreground=TEXT, font=FONT_B)


def png_photo(arr):
    """numpy RGB -> tk.PhotoImage without ImageTk."""
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"RIFE Gapfill  v{GUI_VERSION}")
        root.minsize(1240, 820)
        self.q = queue.Queue()
        self.worker = None
        self.cancel_flag = threading.Event()
        self.files = []
        self.results = {}          # path -> analysis
        self.current = None        # path shown in the review panel
        self.sel_event = None
        self.thumb_token = 0
        self.thumb_imgs = []

        s = self.load_settings()
        rife = s.get("rife") or ""
        if not os.path.isfile(rife):
            rife = find_rife()
        self.v_rife = tk.StringVar(value=rife)
        ff = s.get("ffmpeg") or ""
        if not os.path.isfile(ff):
            ff = core.find_tool("ffmpeg")
        self.v_ffmpeg = tk.StringVar(value=ff)
        self.v_model = tk.StringVar(value=s.get("model", "rife-v4.6"))
        self.v_outmode = tk.StringVar(value=s.get("outmode", "source"))
        self.v_outdir = tk.StringVar(value=s.get("outdir", ""))
        fps = s.get("fps2", "Same as source (even cadence)")
        self.v_fps = tk.StringVar(value=fps if fps in FPS_CHOICES else "Same as source (even cadence)")
        self.v_fps_custom = tk.StringVar(value=s.get("fps_custom", "25"))
        self.v_prores = tk.StringVar(value=s.get("prores", "ProRes 422 HQ"))
        self.v_gap = tk.StringVar(value=str(s.get("gap_ratio", 1.5)))
        self.v_minconf = tk.StringVar(value=str(s.get("min_conf", 0.5)))
        self.v_maxins = tk.StringVar(value=str(s.get("max_insert", 3)))
        self.v_repeats = tk.BooleanVar(value=s.get("repeats", True))
        self.v_periodic = tk.BooleanVar(value=s.get("periodic", True))
        self.v_fillstatic = tk.BooleanVar(value=s.get("fill_static", True))
        self.v_period = tk.StringVar(value="")
        self.v_status = tk.StringVar(value="READY")
        self.v_clip = tk.StringVar(value="No clip selected")
        self.v_summary = tk.StringVar(value="Add clips and click Analyze.")
        self.v_count = tk.StringVar(value="")

        self.build()
        self.refresh_models()
        self.root.after(100, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        missing = [t for t, v in (("rife", self.v_rife), ("ffmpeg", self.v_ffmpeg)) if not v.get()]
        if missing:
            self.root.after(400, lambda: self.offer_download(missing, startup=True))

    # ------------------------------------------------------------- layout
    def section(self, parent, row, title, col=0):
        hdr = ttk.Frame(parent)
        hdr.grid(row=row, column=col, sticky="ew", pady=(10, 4))
        hdr.columnconfigure(1, weight=1)
        ttk.Label(hdr, text=title.upper(), style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Frame(hdr, style="Sep.TFrame", height=1).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        body = ttk.Frame(parent)
        body.grid(row=row + 1, column=col, sticky="nsew", padx=(2, 0))
        return body

    def build(self):
        global _help_bar
        main = ttk.Frame(self.root, padding=(14, 10, 14, 12))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=0, minsize=470)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(1, weight=1)

        # header
        top = ttk.Frame(main)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.columnconfigure(2, weight=1)
        ttk.Label(top, text="RIFE GAPFILL", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(top, text=f"  v{GUI_VERSION} · core v{core.VERSION}", style="Ver.TLabel").grid(
            row=0, column=1, sticky="sw", pady=(0, 2))
        ttk.Label(top, textvariable=self.v_status, style="Status.TLabel").grid(row=0, column=3, sticky="e")

        left = ttk.Frame(main)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 18))
        left.columnconfigure(0, weight=1)
        _help_bar = self.help = HelpBar(left)
        right = ttk.Frame(main)
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)

        self.build_left(left)
        self.build_right(right)
        self.mono_fields(main)

    def build_left(self, left):
        # source clips
        ff = self.section(left, 0, "Source clips")
        ff.columnconfigure(0, weight=1)
        lbf = tk.Frame(ff, bg=PANEL, highlightthickness=1, highlightbackground=SEP)
        lbf.grid(row=0, column=0, rowspan=3, sticky="nsew")
        lbf.columnconfigure(0, weight=1)
        self.lb = tk.Listbox(lbf, height=5, selectmode="extended", bg=PANEL, fg=TEXT,
                             selectbackground=ACCENT, selectforeground="#111111",
                             highlightthickness=0, borderwidth=0, activestyle="none",
                             font=FONT, exportselection=False)
        self.lb.grid(row=0, column=0, sticky="nsew", padx=4, pady=3)
        sb = ttk.Scrollbar(lbf, orient="vertical", command=self.lb.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.lb.configure(yscrollcommand=sb.set)
        self.lb.bind("<<ListboxSelect>>", lambda e: self.on_clip_select())
        tip(self.lb, "files")
        tip(ttk.Button(ff, text="Add…", command=self.add_files), "add").grid(row=0, column=1, sticky="ew", padx=(8, 0))
        tip(ttk.Button(ff, text="Remove", command=self.remove_files), "remove").grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=3)
        tip(ttk.Button(ff, text="Clear", command=self.clear_files), "clear").grid(row=2, column=1, sticky="new", padx=(8, 0))

        # output
        of = self.section(left, 2, "Output")
        of.columnconfigure(3, weight=1)
        tip(ttk.Radiobutton(of, text="Next to source", variable=self.v_outmode, value="source"),
            "out_source").grid(row=0, column=0, columnspan=2, sticky="w")
        tip(ttk.Radiobutton(of, text="Folder", variable=self.v_outmode, value="folder"),
            "out_folder").grid(row=0, column=2, sticky="w", padx=(14, 6))
        tip(ttk.Entry(of, textvariable=self.v_outdir, width=10), "outdir").grid(row=0, column=3, sticky="ew")
        tip(ttk.Button(of, text="…", width=3, command=self.pick_outdir), "browse").grid(row=0, column=4, padx=(6, 0))
        ttk.Label(of, text="Frame rate", style="Dim.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 0))
        cb = tip(ttk.Combobox(of, textvariable=self.v_fps, values=list(FPS_CHOICES),
                              state="readonly", width=29), "fps")
        cb.grid(row=1, column=1, columnspan=3, sticky="w", pady=(8, 0), padx=(10, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self.update_custom())
        self.e_custom = tip(ttk.Entry(of, textvariable=self.v_fps_custom, width=7), "fps_custom")
        self.e_custom.grid(row=1, column=4, sticky="e", pady=(8, 0), padx=(6, 0))
        self.update_custom()
        ttk.Label(of, text="Codec", style="Dim.TLabel").grid(row=2, column=0, sticky="w", pady=(4, 0))
        tip(ttk.Combobox(of, textvariable=self.v_prores, values=list(PRORES_CHOICES),
                         state="readonly", width=29), "codec").grid(
            row=2, column=1, columnspan=3, sticky="w", pady=(4, 0), padx=(10, 0))

        # tools
        tf = self.section(left, 4, "Tools")
        tf.columnconfigure(1, weight=1)
        ttk.Label(tf, text="RIFE", style="Dim.TLabel").grid(row=0, column=0, sticky="w")
        tip(ttk.Entry(tf, textvariable=self.v_rife, width=10), "rife").grid(row=0, column=1, sticky="ew", padx=(10, 0))
        tip(ttk.Button(tf, text="…", width=3, command=self.pick_rife), "browse").grid(row=0, column=2, padx=(6, 0))
        self.b_dl = tip(ttk.Button(tf, text="Download…", command=lambda: self.offer_download(["rife"])), "download")
        self.b_dl.grid(row=0, column=3, padx=(6, 0))
        ttk.Label(tf, text="Model", style="Dim.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        self.cb_model = tip(ttk.Combobox(tf, textvariable=self.v_model, width=16), "model")
        self.cb_model.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=4)
        ttk.Label(tf, text="ffmpeg", style="Dim.TLabel").grid(row=2, column=0, sticky="w")
        tip(ttk.Entry(tf, textvariable=self.v_ffmpeg, width=10), "ffmpeg").grid(row=2, column=1, sticky="ew", padx=(10, 0))
        tip(ttk.Button(tf, text="…", width=3, command=self.pick_ffmpeg), "browse").grid(row=2, column=2, padx=(6, 0))
        self.b_dlff = tip(ttk.Button(tf, text="Download…", command=lambda: self.offer_download(["ffmpeg"])),
                          "download_ffmpeg")
        self.b_dlff.grid(row=2, column=3, padx=(6, 0))

        # detection
        df = self.section(left, 6, "Detection")
        fields = [("Gap threshold", self.v_gap, "gap_ratio", "×"),
                  ("Auto-apply from", self.v_minconf, "min_conf", "conf"),
                  ("Fill gaps up to", self.v_maxins, "max_insert", "frames"),
                  ("Period", self.v_period, "period", "auto")]
        for k, (lab, var, key, unit) in enumerate(fields):
            r, c = divmod(k, 2)
            tip(ttk.Label(df, text=lab, style="Dim.TLabel"), key).grid(
                row=r, column=c * 3, sticky="w", padx=(0 if c == 0 else 18, 8), pady=2)
            tip(ttk.Entry(df, textvariable=var, width=5), key).grid(row=r, column=c * 3 + 1, sticky="w", pady=2)
            ttk.Label(df, text=unit, style="Ver.TLabel").grid(row=r, column=c * 3 + 2, sticky="w", padx=(4, 0))
        chk = ttk.Frame(df)
        chk.grid(row=2, column=0, columnspan=6, sticky="w", pady=(6, 0))
        tip(ttk.Checkbutton(chk, text="Repeats", variable=self.v_repeats), "repeats").pack(side="left")
        tip(ttk.Checkbutton(chk, text="Periodic grid", variable=self.v_periodic), "periodic").pack(side="left", padx=(16, 0))
        tip(ttk.Checkbutton(chk, text="Fill stills", variable=self.v_fillstatic), "fill_static").pack(side="left", padx=(16, 0))

        # help + actions
        self.help.frame.grid(row=8, column=0, sticky="ew", pady=(14, 0))
        af = ttk.Frame(left)
        af.grid(row=9, column=0, sticky="ew", pady=(12, 0))
        af.columnconfigure(3, weight=1)
        self.b_an = tip(ttk.Button(af, text="Analyze", command=lambda: self.start(False)), "analyze")
        self.b_an.grid(row=0, column=0)
        self.b_go = tip(ttk.Button(af, text="Render", style="Accent.TButton",
                                   command=lambda: self.start(True)), "render")
        self.b_go.grid(row=0, column=1, padx=6)
        self.b_cancel = tip(ttk.Button(af, text="Cancel", command=self.cancel, state="disabled"), "cancel")
        self.b_cancel.grid(row=0, column=2)
        self.pb = ttk.Progressbar(af, mode="determinate")
        self.pb.grid(row=0, column=3, sticky="ew", padx=(12, 0))
        lf = tk.Frame(left, bg=PANEL, highlightthickness=1, highlightbackground=SEP)
        lf.grid(row=10, column=0, sticky="nsew", pady=(12, 0))
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)
        self.log = tk.Text(lf, height=7, width=40, bg=PANEL, fg=TEXT, insertbackground=TEXT,
                           selectbackground=TRACK, selectforeground=TEXT, borderwidth=0,
                           highlightthickness=0, font=FONT, padx=8, pady=6, wrap="word",
                           state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=lsb.set)
        self.log.tag_configure("err", foreground=ERR)
        self.log.tag_configure("ok", foreground=ACCENT)
        self.log.tag_configure("head", foreground=TEXT, font=FONT_B)
        self.log.tag_configure("dim", foreground=DIM)
        tip(self.log, "log")
        left.rowconfigure(10, weight=1)

    def build_right(self, right):
        rv = self.section(right, 0, "Review")
        rv.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        hdr = ttk.Frame(rv)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.columnconfigure(0, weight=1)
        ttk.Label(hdr, textvariable=self.v_clip, style="Clip.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(hdr, textvariable=self.v_count, style="Dim.TLabel").grid(row=0, column=1, sticky="e")
        ttk.Label(rv, textvariable=self.v_summary, style="Dim.TLabel", justify="left").grid(
            row=1, column=0, sticky="w", pady=(2, 6))

        cf = tk.Frame(rv, bg=PANEL, highlightthickness=1, highlightbackground=SEP)
        cf.grid(row=2, column=0, sticky="ew")
        self.canvas = tk.Canvas(cf, height=120, bg=PANEL, highlightthickness=0, borderwidth=0,
                                cursor="hand2")
        self.canvas.pack(fill="x", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.draw_timeline())
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        tip(self.canvas, "timeline")

        tf = tk.Frame(rv, bg=PANEL, highlightthickness=1, highlightbackground=SEP)
        tf.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        rv.rowconfigure(3, weight=1)
        tf.columnconfigure(0, weight=1)
        tf.rowconfigure(0, weight=1)
        cols = ("on", "frame", "tc", "type", "conf", "note")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="extended", height=14)
        for c, txt, w, anc in (("on", "", 34, "center"), ("frame", "FRAME", 62, "e"),
                               ("tc", "TIMECODE", 100, "w"), ("type", "TYPE", 110, "w"),
                               ("conf", "CONF", 52, "e"), ("note", "NOTE", 300, "w")):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, minwidth=w if c != "note" else 120, anchor=anc,
                             stretch=(c == "note"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        tsb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        tsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.tag_configure("off", foreground=DIM)
        self.tree.tag_configure("cut", foreground=DIMMER)
        self.tree.tag_configure("rep", foreground=REPEAT)
        self.tree.bind("<Button-1>", self.on_tree_click, add="+")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_event_select())
        self.tree.bind("<space>", lambda e: (self.toggle_selected(), "break")[1])
        tip(self.tree, "events")

        bf = ttk.Frame(rv)
        bf.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        self.b_all = tip(ttk.Button(bf, text="Tick all", style="Small.TButton",
                                    command=lambda: self.set_ticks("all")), "all")
        self.b_all.pack(side="left")
        self.b_none = tip(ttk.Button(bf, text="Untick all", style="Small.TButton",
                                     command=lambda: self.set_ticks("none")), "none")
        self.b_none.pack(side="left", padx=6)
        self.b_auto = tip(ttk.Button(bf, text="Tick by confidence", style="Small.TButton",
                                     command=lambda: self.set_ticks("auto")), "auto")
        self.b_auto.pack(side="left")

        th = ttk.Frame(rv)
        th.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        self.thumb_labels, self.thumb_caps = [], []
        for k in range(3):
            fr = tk.Frame(th, bg=PANEL, highlightthickness=1, highlightbackground=SEP)
            fr.grid(row=0, column=k, padx=(0 if k == 0 else 8, 0), sticky="nw")
            lab = tk.Label(fr, bg=PANEL, width=THUMB_W, height=int(THUMB_W * 9 / 16),
                           bd=0, highlightthickness=0)
            img = tk.PhotoImage(width=THUMB_W, height=int(THUMB_W * 9 / 16))
            lab.configure(image=img)
            lab.image = img
            lab.pack()
            cap = tk.Label(fr, text="", bg=PANEL, fg=DIM, font=FONT, anchor="w", padx=6, pady=2)
            cap.pack(fill="x")
            tip(lab, "thumbs")
            self.thumb_labels.append(lab)
            self.thumb_caps.append(cap)


    def mono_fields(self, w):
        """ttk Entry/Combobox ignore the style font; set it per widget."""
        for c in w.winfo_children():
            if isinstance(c, (ttk.Entry, ttk.Combobox)):
                c.configure(font=FONT)
            self.mono_fields(c)

    # ------------------------------------------------------------ review
    def an(self):
        return self.results.get(self.current) if self.current else None

    def on_clip_select(self):
        sel = self.lb.curselection()
        if not sel:
            return
        self.current = self.files[sel[0]]
        self.show_review()

    def show_review(self):
        an = self.an()
        self.tree.delete(*self.tree.get_children())
        self.sel_event = None
        self.clear_thumbs()
        if not self.current:
            self.v_clip.set("No clip selected")
            self.v_summary.set("Add clips and click Analyze.")
            self.v_count.set("")
        elif not an:
            self.v_clip.set(os.path.basename(self.current))
            self.v_summary.set("Not analysed yet. Click Analyze.")
            self.v_count.set("")
        else:
            self.v_clip.set(os.path.basename(self.current))
            for idx, e in enumerate(an["events"]):
                self.tree.insert("", "end", iid=str(idx), values=self.row_values(an, e),
                                 tags=self.row_tags(e))
            self.update_summary()
        self.draw_timeline()

    def row_values(self, an, e):
        on = "" if e.kind in ("cut",) else ("[x]" if e.apply else "[ ]")
        tc = core.frames_to_tc(e.frame, an["fps"], an["info"].get("timecode"))
        conf = "" if e.kind == "cut" else f"{e.conf:.2f}"
        note = e.note + ("  [grid]" if e.grid else "")
        return (on, e.frame, tc, e.label(), conf, note)

    def row_tags(self, e):
        if e.kind == "cut" or e.kind == "long":
            return ("cut",)
        if not e.apply:
            return ("off",)
        return ("rep",) if e.kind == "repeat" else ()

    def update_summary(self):
        an = self.an()
        if not an:
            return
        ev = an["events"]
        on = [e for e in ev if e.apply]
        ins = sum(e.count for e in on if e.kind == "missing")
        rep = sum(1 for e in on if e.kind == "repeat")
        self.v_summary.set(core.summary(an).replace("\n  ", "\n"))
        self.v_count.set(f"{an['n']} -> {core.out_count(an)} frames   +{ins} interpolated  -{rep} repeats")

    def refresh_row(self, idx):
        an = self.an()
        e = an["events"][idx]
        self.tree.item(str(idx), values=self.row_values(an, e), tags=self.row_tags(e))

    def on_tree_click(self, ev):
        if self.tree.identify_region(ev.x, ev.y) != "cell":
            return
        if self.tree.identify_column(ev.x) != "#1":
            return
        row = self.tree.identify_row(ev.y)
        if row:
            self.toggle([int(row)])
            return "break"

    def toggle_selected(self):
        self.toggle([int(i) for i in self.tree.selection()])

    def toggle(self, idxs):
        an = self.an()
        if not an or (self.worker and self.worker.is_alive()):
            return
        for i in idxs:
            e = an["events"][i]
            if e.kind in ("missing", "repeat"):
                e.apply = not e.apply
                self.refresh_row(i)
        self.update_summary()
        self.draw_timeline()

    def set_ticks(self, mode):
        an = self.an()
        if not an or (self.worker and self.worker.is_alive()):
            return
        try:
            mc = float(self.v_minconf.get())
        except ValueError:
            mc = 0.5
        for i, e in enumerate(an["events"]):
            if e.kind not in ("missing", "repeat"):
                continue
            e.apply = mode == "all" or (mode == "auto" and e.conf >= mc)
            self.refresh_row(i)
        self.update_summary()
        self.draw_timeline()

    def on_event_select(self):
        sel = self.tree.selection()
        an = self.an()
        if not sel or not an:
            return
        self.sel_event = int(sel[0])
        self.draw_timeline()
        self.load_thumbs(an, an["events"][self.sel_event])

    def select_event(self, idx):
        self.tree.selection_set(str(idx))
        self.tree.focus(str(idx))
        def centre():
            kids = self.tree.get_children()
            if kids:
                self.tree.yview_moveto(max(0.0, (idx - 6) / len(kids)))
        self.root.after_idle(centre)

    # timeline -------------------------------------------------------
    def tl_geom(self):
        w = max(self.canvas.winfo_width(), 100)
        h = max(self.canvas.winfo_height(), 60)
        return w, h, 10, 10

    def draw_timeline(self):
        c = self.canvas
        c.delete("all")
        an = self.an()
        w, h, px, py = self.tl_geom()
        if not an:
            c.create_text(w // 2, h // 2, text="Motion timeline appears here after analysis",
                          fill=DIMMER, font=FONT)
            return
        n = max(an["n"], 2)
        top, bot = py + 14, h - py
        ymax = 4.0

        def X(i):
            return px + (w - 2 * px) * i / (n - 1)

        def Y(r):
            return bot - (bot - top) * min(r, ymax) / ymax

        c.create_line(px, Y(1.0), w - px, Y(1.0), fill=SEP)
        try:
            thr = float(self.v_gap.get())
        except ValueError:
            thr = 1.5
        c.create_line(px, Y(thr), w - px, Y(thr), fill=DIMMER, dash=(3, 3))
        c.create_text(w - px, Y(thr) - 7, text=f"{thr:g}x", fill=DIMMER, font=(FONT_FAMILY, 7), anchor="e")

        ratio, judge = an["stats"]["ratio"], an["stats"]["judge"]
        cols = int(w - 2 * px)
        per = n / max(cols, 1)
        for xcol in range(cols):
            a, b = int(xcol * per), int((xcol + 1) * per) + 1
            seg = [ratio[i] for i in range(max(1, a), min(n, b)) if judge[i]]
            if not seg:
                continue
            r = max(seg)
            c.create_line(px + xcol, Y(0), px + xcol, Y(r), fill=TRACK if r < thr else "#6A5A48")

        for idx, e in enumerate(an["events"]):
            x = X(e.frame)
            if e.kind == "missing":
                col = ACCENT if e.apply else "#7A4A16"
            elif e.kind == "repeat":
                col = REPEAT if e.apply else "#24506E"
            else:
                col = DIM
            c.create_line(x, top - 2, x, bot, fill=col, width=1)
            c.create_polygon(x - 4, py, x + 4, py, x, py + 7, fill=col, outline="")
            if idx == self.sel_event:
                c.create_rectangle(x - 5, py - 2, x + 5, bot + 2, outline=TEXT)

    def on_canvas_click(self, ev):
        an = self.an()
        if not an or not an["events"]:
            return
        w, h, px, py = self.tl_geom()
        n = max(an["n"], 2)
        frame = (ev.x - px) / max(w - 2 * px, 1) * (n - 1)
        idx = min(range(len(an["events"])), key=lambda k: abs(an["events"][k].frame - frame))
        self.select_event(idx)

    # thumbnails -----------------------------------------------------
    def clear_thumbs(self):
        for lab, cap in zip(self.thumb_labels, self.thumb_caps):
            img = tk.PhotoImage(width=THUMB_W, height=int(THUMB_W * 9 / 16))
            lab.configure(image=img)
            lab.image = img
            cap.configure(text="")

    def load_thumbs(self, an, e):
        self.thumb_token += 1
        token = self.thumb_token
        if e.kind == "missing" or e.kind == "long":
            frames = [e.frame - 2, e.frame - 1, e.frame]
            caps = ["before", "last before gap", "first after gap"]
        else:
            frames = [e.frame - 1, e.frame, e.frame + 1]
            caps = ["before", "repeat" if e.kind == "repeat" else "after cut", "after"]
        ff = self.v_ffmpeg.get().strip() or core.find_tool("ffmpeg")
        for cap, n, txt in zip(self.thumb_caps, frames, caps):
            cap.configure(text=f"{n}  {txt}" if 0 <= n < an["n"] else "")

        def work():
            for k, n in enumerate(frames):
                if token != self.thumb_token:
                    return
                arr = core.extract_frame(an["path"], n, an["fps"], ff, THUMB_W) if 0 <= n < an["n"] else None
                self.q.put(("thumb", (token, k, arr)))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ helpers
    def update_custom(self):
        self.e_custom.configure(state="normal" if FPS_CHOICES.get(self.v_fps.get()) == "custom" else "disabled")

    def refresh_models(self):
        models = core.list_models(self.v_rife.get())
        self.cb_model["values"] = models
        if models and self.v_model.get() not in models:
            self.v_model.set("rife-v4.6" if "rife-v4.6" in models else models[-1])

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select clips",
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.webm *.avi *.mxf"), ("All", "*.*")])
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                self.lb.insert("end", p)
        if self.files and not self.current:
            self.lb.selection_set(0)
            self.on_clip_select()

    def remove_files(self):
        for i in reversed(self.lb.curselection()):
            p = self.files[i]
            self.lb.delete(i)
            del self.files[i]
            self.results.pop(p, None)
            if p == self.current:
                self.current = None
        self.show_review()

    def clear_files(self):
        self.lb.delete(0, "end")
        self.files.clear()
        self.results.clear()
        self.current = None
        self.show_review()

    def pick_outdir(self):
        d = filedialog.askdirectory()
        if d:
            self.v_outdir.set(d)
            self.v_outmode.set("folder")

    def pick_rife(self):
        p = filedialog.askopenfilename(title="rife-ncnn-vulkan.exe",
                                       filetypes=[("Executable", "*.exe"), ("All", "*.*")])
        if p:
            self.v_rife.set(p)
            self.refresh_models()

    def pick_ffmpeg(self):
        p = filedialog.askopenfilename(title="ffmpeg.exe",
                                       filetypes=[("Executable", "*.exe"), ("All", "*.*")])
        if p:
            self.v_ffmpeg.set(p)

    def write_log(self, text):
        s = text.strip()
        if "ERROR" in s:
            tag = "err"
        elif s.startswith("done:") or s.startswith("RIFE ready") or s.startswith("ffmpeg ready"):
            tag = "ok"
        elif text and not text.startswith(" "):
            tag = "head"
        elif s.startswith("from ") or s.startswith("CSV:") or s.startswith("periodic"):
            tag = "dim"
        else:
            tag = None
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def load_settings(self):
        try:
            with open(settings_path()) as f:
                return json.load(f)
        except Exception:
            return {}

    def save_settings(self):
        s = {"rife": self.v_rife.get(), "ffmpeg": self.v_ffmpeg.get(),
             "model": self.v_model.get(), "outmode": self.v_outmode.get(),
             "outdir": self.v_outdir.get(), "fps2": self.v_fps.get(),
             "fps_custom": self.v_fps_custom.get(), "prores": self.v_prores.get(),
             "gap_ratio": self.v_gap.get(), "min_conf": self.v_minconf.get(),
             "max_insert": self.v_maxins.get(), "repeats": self.v_repeats.get(),
             "periodic": self.v_periodic.get(), "fill_static": self.v_fillstatic.get()}
        try:
            with open(settings_path(), "w") as f:
                json.dump(s, f, indent=2)
        except Exception:
            pass

    def make_options(self):
        mode = FPS_CHOICES[self.v_fps.get()]
        if mode == "custom":
            float(self.v_fps_custom.get())  # validate
            mode = self.v_fps_custom.get()
        period = self.v_period.get().strip()
        return core.Options(
            ffmpeg=self.v_ffmpeg.get().strip(), ffprobe="", rife=self.v_rife.get().strip(),
            model=self.v_model.get().strip() or "rife-v4.6",
            prores=PRORES_CHOICES[self.v_prores.get()], out_fps=mode,
            gap_ratio=float(self.v_gap.get()), min_conf=float(self.v_minconf.get()),
            max_insert=int(self.v_maxins.get()), detect_repeats=self.v_repeats.get(),
            periodic=self.v_periodic.get(), fill_static=self.v_fillstatic.get(),
            period=int(period) if period else None)

    # ------------------------------------------------------------- worker
    def start(self, do_render):
        if not self.files:
            messagebox.showinfo("RIFE Gapfill", "Add one or more clips first.")
            return
        try:
            opt = self.make_options()
        except ValueError:
            messagebox.showerror("RIFE Gapfill", "Check the numeric fields.")
            return
        if self.v_outmode.get() == "folder" and not os.path.isdir(self.v_outdir.get()):
            messagebox.showerror("RIFE Gapfill", "Output folder does not exist.")
            return
        self.save_settings()
        self.cancel_flag.clear()
        self.set_busy(True)
        outdir = self.v_outdir.get() if self.v_outmode.get() == "folder" else None
        args = (list(self.files), opt, do_render, outdir)
        self.worker = threading.Thread(target=self.run_jobs, args=args, daemon=True)
        self.worker.start()

    def run_jobs(self, files, opt, do_render, outdir):
        log = lambda t: self.q.put(("log", t))
        cancel = self.cancel_flag.is_set
        prog = lambda i, t: self.q.put(("progress", (i, t)))
        n = len(files)
        for k, path in enumerate(files, 1):
            self.q.put(("progress", (0, 1)))
            try:
                an = self.results.get(path)
                if not do_render or an is None:
                    self.q.put(("status", f"ANALYSING {k}/{n}"))
                    if an is not None:
                        core.resolve_tools(opt)
                        events, stats = core.classify(an["m"], opt, an["fps"])
                        an = dict(an, events=events, stats=stats)
                        log(os.path.basename(path))
                        log("  re-classified from saved measurements")
                        log("  " + core.summary(an))
                    else:
                        an = core.analyze(path, opt, log=log, cancel=cancel, progress=prog)
                    self.q.put(("analysed", (path, an)))
                if do_render:
                    self.q.put(("status", f"RENDERING {k}/{n}"))
                    out = core.default_output(path, core.out_fps_for(an, opt), outdir)
                    core.render(an, out, opt, log=log, progress=prog, cancel=cancel)
                    log(f"  done: {out}")
            except core.Cancelled:
                log("  cancelled")
                break
            except Exception as e:
                log(f"  ERROR: {e}")
            log("")
        self.q.put(("done", None))

    def offer_download(self, which, startup=False):
        if self.worker and self.worker.is_alive():
            return
        dest = download_dir()
        names = {"rife": "RIFE (rife-ncnn-vulkan, a few hundred MB, all models)",
                 "ffmpeg": "ffmpeg + ffprobe (Gyan's release essentials build, about 110 MB)"}
        msg = ""
        if startup:
            msg = "Not found on this computer:\n" + "\n".join("  - " + names[w] for w in which) + "\n\n"
        msg += ("Download " + (" and ".join(names[w].split(" (")[0] for w in which)) + " now?\n\n"
                + ("" if startup else names[which[0]] + "\n\n")
                + f"Saved to:\n{dest}")
        if not messagebox.askyesno("RIFE Gapfill", msg):
            return
        self.cancel_flag.clear()
        self.set_busy(True)
        self.worker = threading.Thread(target=self.run_download, args=(dest, list(which)), daemon=True)
        self.worker.start()

    def run_download(self, dest, which):
        log = lambda t: self.q.put(("log", t))
        prog = lambda i, t: self.q.put(("progress", (i, t)))
        for w in which:
            self.q.put(("status", "DOWNLOADING " + ("RIFE" if w == "rife" else "FFMPEG")))
            try:
                if w == "rife":
                    self.q.put(("rife", download_rife(dest, log, prog, self.cancel_flag.is_set)))
                else:
                    self.q.put(("ffmpeg", download_ffmpeg(dest, log, prog, self.cancel_flag.is_set)))
            except core.Cancelled:
                log("  download cancelled")
                break
            except Exception as e:
                log(f"  ERROR: download failed: {e}")
                if w == "rife":
                    log("  Manual download: github.com/nihui/rife-ncnn-vulkan/releases, then use the … button")
                else:
                    log("  Manual install: winget install Gyan.FFmpeg, or gyan.dev/ffmpeg/builds")
            log("")
        self.q.put(("done", None))

    def cancel(self):
        self.cancel_flag.set()
        self.v_status.set("CANCELLING")

    def set_busy(self, busy):
        st = "disabled" if busy else "normal"
        for b in (self.b_an, self.b_go, self.b_dl, self.b_dlff, self.b_all, self.b_none, self.b_auto):
            b.configure(state=st)
        self.b_cancel.configure(state="normal" if busy else "disabled")

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.write_log(val)
                elif kind == "status":
                    self.v_status.set(val)
                elif kind == "progress":
                    i, t = val
                    self.pb["maximum"] = max(t, 1)
                    self.pb["value"] = i
                elif kind == "analysed":
                    path, an = val
                    self.results[path] = an
                    if self.current is None:
                        self.current = path
                        if path in self.files:
                            self.lb.selection_clear(0, "end")
                            self.lb.selection_set(self.files.index(path))
                    if path == self.current:
                        self.show_review()
                elif kind == "thumb":
                    token, k, arr = val
                    if token == self.thumb_token and arr is not None:
                        img = png_photo(arr)
                        self.thumb_labels[k].configure(image=img)
                        self.thumb_labels[k].image = img
                elif kind == "rife":
                    self.v_rife.set(val)
                    self.refresh_models()
                    self.save_settings()
                elif kind == "ffmpeg":
                    self.v_ffmpeg.set(val)
                    self.save_settings()
                elif kind == "done":
                    self.set_busy(False)
                    self.v_status.set("READY")
                    self.pb["value"] = 0
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("RIFE Gapfill", "A job is running. Cancel and quit?"):
                return
            self.cancel_flag.set()
        self.save_settings()
        self.root.destroy()


def main():
    set_dpi_awareness()
    root = tk.Tk()
    apply_theme(root)
    extra_theme(root)
    root.geometry("1320x880")
    App(root)
    dark_titlebar(root)
    root.mainloop()


if __name__ == "__main__":
    main()
