#!/usr/bin/env python3
"""
gapfill_gui.py  v1.3  -  GUI for rife_gapfill (v1.1+)

Build a Windows exe (run in the folder containing both .py files):
    pip install pyinstaller numpy pillow
    pyinstaller --onefile --windowed --name RifeGapfill gapfill_gui.py
The exe ends up in dist\\RifeGapfill.exe. Put it next to the
rife-ncnn-vulkan folder (or point to RIFE in the GUI). Settings are saved in
%APPDATA%\\RifeGapfill\\settings.json.

Changes
  1.3  Help text shown in a fixed panel inside the window instead of
       floating tooltips.
  1.2  Dark theme in Nils-Player style (graphite, orange accent, Consolas),
       hover help on every control, dark Windows title bar, high-DPI aware,
       coloured log.
  1.1  Download button for rife-ncnn-vulkan (latest Windows release from
       GitHub); offered automatically when RIFE is not found at startup.
  1.0  First version.
"""

import glob
import json
import os
import queue
import threading
import urllib.request
import zipfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import rife_gapfill as core

GUI_VERSION = "1.3"

RIFE_API = "https://api.github.com/repos/nihui/rife-ncnn-vulkan/releases/latest"
RIFE_FALLBACK = ("https://github.com/nihui/rife-ncnn-vulkan/releases/download/"
                 "20221029/rife-ncnn-vulkan-20221029-windows.zip")
UA = {"User-Agent": f"RifeGapfill/{GUI_VERSION}"}

FPS_CHOICES = {
    "Same as source (longer clip)": "same",
    "Restore original duration": "restore",
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
ACCENT = "#FF9B2E"   # orange
ACCENT_HI = "#FFB25C"
ERR = "#FF6B5A"
FONT_FAMILY = "Consolas"
FONT = (FONT_FAMILY, 9)
FONT_B = (FONT_FAMILY, 9, "bold")


HELP = {
    "add": "Add one or more video files. All clips in the list are processed in turn with the same settings.",
    "remove": "Remove the selected clips from the list.",
    "clear": "Empty the list.",
    "files": "Clips to process. Select several with Ctrl or Shift.",
    "out_source": "Write each result in the same folder as its source clip.",
    "out_folder": "Write all results to the folder below.",
    "outdir": "Output folder used when 'Folder' is selected.",
    "fps": ("Frame rate of the output file.\n\n"
            "Same as source: keeps the source rate. One frame is added per gap, so the clip gets longer.\n\n"
            "Restore original duration: raises the rate by (period+1)/period so the duration stays the same, "
            "e.g. 24 -> 25 fps for a gap every 24 frames.\n\n"
            "Custom: any rate you type."),
    "fps_custom": "Output frame rate for 'Custom', e.g. 24, 25 or 23.976.",
    "codec": ("ProRes 422: smaller files.\nProRes 422 HQ: standard for editing and grading.\n"
              "ProRes 4444: full chroma resolution, largest files.\n\nAudio is not included."),
    "rife": "Path to rife-ncnn-vulkan.exe. Found automatically when it sits next to this program.",
    "download": "Download the latest rife-ncnn-vulkan for Windows from GitHub (nihui/rife-ncnn-vulkan) and set it up. A few hundred MB.",
    "model": "RIFE model. rife-v4.6 is the newest and usually the best. The older models are there for comparison.",
    "ffmpeg": "Path to ffmpeg.exe. ffprobe.exe must be in the same folder. Found automatically on PATH or in the WinGet install folder.",
    "browse": "Choose the file or folder.",
    "period": "Leave blank to detect the cadence automatically. Type a number to force a known period, e.g. 24.",
    "threshold": "How many times larger than the surrounding motion a frame step must be to count as a clear gap. Default 1.5.",
    "strength": ("How consistent the gap pattern must be before rendering is allowed. Lower it if a real cadence is "
                 "rejected, raise it to avoid false detections. Default 1.3."),
    "snap": "How many frames a gap may move away from the strict grid. 1 lets tracking follow slow drift, 0 locks it to the grid.",
    "cut": ("A gap where the step is this many times larger than normal is treated as a scene cut and filled with a copy of "
            "the previous frame instead of an interpolated one. Default 8."),
    "spikes": ("Off: fill every grid position, including still shots where the gap can't be seen, so timing stays even.\n"
               "On: fill only where a clear jump is measured."),
    "force": "Render even if the cadence looks weak. Use together with a fixed period if detection fails.",
    "csv": "Save a per-frame report (difference, ratio, gap) next to the clip, for checking against your difference layer in Resolve.",
    "analyze": "Detect the cadence and list the gaps without rendering anything.",
    "render": "Analyze and render each clip to ProRes with the gaps filled.",
    "cancel": "Stop the current job. A half-written output file is deleted.",
    "log": "Results and progress for each clip.",
}


# ------------------------------------------------------------------ RIFE download

def local_tools_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "RifeGapfill")


def rife_exe_name():
    return "rife-ncnn-vulkan" + (".exe" if os.name == "nt" else "")


def find_rife():
    p = core.find_tool("rife-ncnn-vulkan")
    if p:
        return p
    hits = sorted(glob.glob(os.path.join(local_tools_dir(), "rife-ncnn-vulkan*", rife_exe_name())))
    return hits[-1] if hits else ""


def rife_download_dir():
    """Next to the exe/script if writable, else %LOCALAPPDATA%\\RifeGapfill."""
    d = core.app_dir()
    try:
        probe = os.path.join(d, ".write_test")
        with open(probe, "w"):
            pass
        os.remove(probe)
        return d
    except OSError:
        os.makedirs(local_tools_dir(), exist_ok=True)
        return local_tools_dir()


def rife_release_asset():
    """Return (name, url, size) of the latest Windows zip."""
    try:
        req = urllib.request.Request(RIFE_API, headers={**UA, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            rel = json.load(r)
        for a in rel.get("assets", []):
            if a["name"].endswith("-windows.zip"):
                return a["name"], a["browser_download_url"], int(a.get("size") or 0)
    except Exception:
        pass
    return os.path.basename(RIFE_FALLBACK), RIFE_FALLBACK, 0


def download_rife(dest, log, progress, cancel):
    name, url, size = rife_release_asset()
    log(f"Downloading {name}")
    log(f"  from {url}")
    part = os.path.join(dest, name + ".part")
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
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
        log(f"  {got / 1e6:.0f} MB downloaded, extracting ...")
        root = os.path.realpath(dest)
        with zipfile.ZipFile(part) as z:
            for m in z.infolist():          # refuse paths escaping dest
                target = os.path.realpath(os.path.join(dest, m.filename))
                if not (target == root or target.startswith(root + os.sep)):
                    raise core.GapfillError(f"unsafe path in zip: {m.filename}")
            z.extractall(dest)
    finally:
        if os.path.exists(part):
            os.remove(part)
    hits = sorted(glob.glob(os.path.join(dest, "rife-ncnn-vulkan*", rife_exe_name())))
    if not hits:
        raise core.GapfillError("download finished but rife-ncnn-vulkan.exe was not found")
    log(f"  RIFE ready: {hits[-1]}")
    return hits[-1]


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


# ------------------------------------------------------------------ app

class App:
    def __init__(self, root):
        self.root = root
        root.title(f"RIFE Gapfill  v{GUI_VERSION}")
        root.minsize(800, 760)
        self.q = queue.Queue()
        self.worker = None
        self.cancel_flag = threading.Event()
        self.files = []

        s = self.load_settings()
        rife = s.get("rife") or ""
        if not os.path.isfile(rife):
            rife = find_rife()
        self.v_rife = tk.StringVar(value=rife)
        self.v_ffmpeg = tk.StringVar(value=s.get("ffmpeg") or core.find_tool("ffmpeg"))
        self.v_model = tk.StringVar(value=s.get("model", "rife-v4.6"))
        self.v_outmode = tk.StringVar(value=s.get("outmode", "source"))
        self.v_outdir = tk.StringVar(value=s.get("outdir", ""))
        self.v_fps = tk.StringVar(value=s.get("fps", "Same as source (longer clip)"))
        self.v_fps_custom = tk.StringVar(value=s.get("fps_custom", "24"))
        self.v_prores = tk.StringVar(value=s.get("prores", "ProRes 422 HQ"))
        self.v_spikes = tk.BooleanVar(value=s.get("spikes_only", False))
        self.v_csv = tk.BooleanVar(value=s.get("csv", False))
        self.v_period = tk.StringVar(value="")
        self.v_threshold = tk.StringVar(value=str(s.get("threshold", 1.5)))
        self.v_strength = tk.StringVar(value=str(s.get("min_strength", 1.3)))
        self.v_snap = tk.StringVar(value=str(s.get("snap", 1)))
        self.v_cut = tk.StringVar(value=str(s.get("cut_ratio", 8.0)))
        self.v_force = tk.BooleanVar(value=False)
        self.v_status = tk.StringVar(value="READY")

        self.build()
        self.refresh_models()
        self.root.after(100, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if not self.v_rife.get():
            self.root.after(400, lambda: self.offer_rife_download(startup=True))

    # ------------------------------------------------------------- layout
    def section(self, parent, row, title):
        hdr = ttk.Frame(parent)
        hdr.grid(row=row, column=0, sticky="ew", pady=(10, 4))
        hdr.columnconfigure(1, weight=1)
        ttk.Label(hdr, text=title.upper(), style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Frame(hdr, style="Sep.TFrame", height=1).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        body = ttk.Frame(parent)
        body.grid(row=row + 1, column=0, sticky="nsew", padx=(2, 0))
        return body

    def build(self):
        global _help_bar
        main = ttk.Frame(self.root, padding=(14, 10, 14, 12))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        _help_bar = self.help = HelpBar(main)

        # header bar
        top = ttk.Frame(main)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(2, weight=1)
        ttk.Label(top, text="RIFE GAPFILL", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(top, text=f"  v{GUI_VERSION} · core v{core.VERSION}", style="Ver.TLabel").grid(
            row=0, column=1, sticky="sw", pady=(0, 2))
        ttk.Label(top, textvariable=self.v_status, style="Status.TLabel").grid(row=0, column=3, sticky="e")

        # source clips
        ff = self.section(main, 1, "Source clips")
        ff.columnconfigure(0, weight=1)
        lbf = tk.Frame(ff, bg=PANEL, highlightthickness=1, highlightbackground=SEP, highlightcolor=ACCENT)
        lbf.grid(row=0, column=0, rowspan=3, sticky="nsew")
        lbf.columnconfigure(0, weight=1)
        self.lb = tk.Listbox(lbf, height=5, selectmode="extended", bg=PANEL, fg=TEXT,
                             selectbackground=ACCENT, selectforeground="#111111",
                             highlightthickness=0, borderwidth=0, activestyle="none",
                             font=FONT)
        self.lb.grid(row=0, column=0, sticky="nsew", padx=4, pady=3)
        sb = ttk.Scrollbar(lbf, orient="vertical", command=self.lb.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.lb.configure(yscrollcommand=sb.set)
        tip(self.lb, "files")
        tip(ttk.Button(ff, text="Add…", command=self.add_files), "add").grid(row=0, column=1, sticky="ew", padx=(8, 0))
        tip(ttk.Button(ff, text="Remove", command=self.remove_files), "remove").grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=3)
        tip(ttk.Button(ff, text="Clear", command=self.clear_files), "clear").grid(row=2, column=1, sticky="new", padx=(8, 0))

        # output
        of = self.section(main, 3, "Output")
        of.columnconfigure(3, weight=1)
        tip(ttk.Radiobutton(of, text="Next to source", variable=self.v_outmode, value="source"),
            "out_source").grid(row=0, column=0, columnspan=2, sticky="w")
        tip(ttk.Radiobutton(of, text="Folder", variable=self.v_outmode, value="folder"),
            "out_folder").grid(row=0, column=2, sticky="w", padx=(18, 6))
        tip(ttk.Entry(of, textvariable=self.v_outdir), "outdir").grid(row=0, column=3, sticky="ew")
        tip(ttk.Button(of, text="Browse…", command=self.pick_outdir), "browse").grid(row=0, column=4, padx=(6, 0))

        ttk.Label(of, text="Frame rate", style="Dim.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 0))
        cb = tip(ttk.Combobox(of, textvariable=self.v_fps, values=list(FPS_CHOICES),
                              state="readonly", width=30), "fps")
        cb.grid(row=1, column=1, columnspan=2, sticky="w", pady=(8, 0), padx=(10, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self.update_custom())
        self.e_custom = tip(ttk.Entry(of, textvariable=self.v_fps_custom, width=8), "fps_custom")
        self.e_custom.grid(row=1, column=3, sticky="w", pady=(8, 0), padx=(8, 0))
        self.update_custom()

        ttk.Label(of, text="Codec", style="Dim.TLabel").grid(row=2, column=0, sticky="w", pady=(4, 0))
        tip(ttk.Combobox(of, textvariable=self.v_prores, values=list(PRORES_CHOICES),
                         state="readonly", width=30), "codec").grid(
            row=2, column=1, columnspan=2, sticky="w", pady=(4, 0), padx=(10, 0))
        ttk.Label(of, text="no audio", style="Ver.TLabel").grid(row=2, column=3, sticky="w", padx=(8, 0), pady=(4, 0))

        # tools
        tf = self.section(main, 5, "Tools")
        tf.columnconfigure(1, weight=1)
        ttk.Label(tf, text="RIFE", style="Dim.TLabel").grid(row=0, column=0, sticky="w")
        tip(ttk.Entry(tf, textvariable=self.v_rife), "rife").grid(row=0, column=1, sticky="ew", padx=(10, 0))
        tip(ttk.Button(tf, text="Browse…", command=self.pick_rife), "browse").grid(row=0, column=2, padx=(6, 0))
        self.b_dl = tip(ttk.Button(tf, text="Download…", command=self.offer_rife_download), "download")
        self.b_dl.grid(row=0, column=3, padx=(6, 0))
        ttk.Label(tf, text="Model", style="Dim.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        self.cb_model = tip(ttk.Combobox(tf, textvariable=self.v_model, width=18), "model")
        self.cb_model.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=4)
        ttk.Label(tf, text="ffmpeg", style="Dim.TLabel").grid(row=2, column=0, sticky="w")
        tip(ttk.Entry(tf, textvariable=self.v_ffmpeg), "ffmpeg").grid(row=2, column=1, sticky="ew", padx=(10, 0))
        tip(ttk.Button(tf, text="Browse…", command=self.pick_ffmpeg), "browse").grid(row=2, column=2, padx=(6, 0))

        # detection
        df = self.section(main, 7, "Detection")
        fields = [("Period", self.v_period, "period", "auto"),
                  ("Spike threshold", self.v_threshold, "threshold", "×"),
                  ("Min strength", self.v_strength, "strength", "×"),
                  ("Drift snap", self.v_snap, "snap", "fr"),
                  ("Scene-cut ratio", self.v_cut, "cut", "×")]
        for k, (lab, var, key, unit) in enumerate(fields):
            r, c = divmod(k, 3)
            tip(ttk.Label(df, text=lab, style="Dim.TLabel"), key).grid(
                row=r, column=c * 3, sticky="w", padx=(0 if c == 0 else 22, 8), pady=2)
            tip(ttk.Entry(df, textvariable=var, width=6), key).grid(row=r, column=c * 3 + 1, sticky="w", pady=2)
            ttk.Label(df, text=unit, style="Ver.TLabel").grid(row=r, column=c * 3 + 2, sticky="w", padx=(4, 0))
        chk = ttk.Frame(df)
        chk.grid(row=2, column=0, columnspan=9, sticky="w", pady=(6, 0))
        tip(ttk.Checkbutton(chk, text="Only fill clear spikes", variable=self.v_spikes), "spikes").pack(side="left")
        tip(ttk.Checkbutton(chk, text="Force", variable=self.v_force), "force").pack(side="left", padx=(22, 0))
        tip(ttk.Checkbutton(chk, text="Write analysis CSV", variable=self.v_csv), "csv").pack(side="left", padx=(22, 0))

        # help panel
        self.help.frame.grid(row=9, column=0, sticky="ew", pady=(14, 0))

        # actions
        af = ttk.Frame(main)
        af.grid(row=10, column=0, sticky="ew", pady=(12, 6))
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

        # log
        lf = tk.Frame(main, bg=PANEL, highlightthickness=1, highlightbackground=SEP)
        lf.grid(row=11, column=0, sticky="nsew")
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        main.rowconfigure(11, weight=1)
        self.log = tk.Text(lf, height=11, bg=PANEL, fg=TEXT, insertbackground=TEXT,
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
        self.mono_fields(main)

    def mono_fields(self, w):
        """ttk Entry/Combobox ignore the style font; set it per widget."""
        for c in w.winfo_children():
            if isinstance(c, (ttk.Entry, ttk.Combobox)):
                c.configure(font=FONT)
            self.mono_fields(c)

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

    def remove_files(self):
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i)
            del self.files[i]

    def clear_files(self):
        self.lb.delete(0, "end")
        self.files.clear()

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
        elif s.startswith("done:") or s.startswith("RIFE ready"):
            tag = "ok"
        elif text and not text.startswith(" "):
            tag = "head"
        elif s.startswith("from ") or s.startswith("CSV:"):
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
             "outdir": self.v_outdir.get(), "fps": self.v_fps.get(),
             "fps_custom": self.v_fps_custom.get(), "prores": self.v_prores.get(),
             "spikes_only": self.v_spikes.get(), "csv": self.v_csv.get(),
             "threshold": self.v_threshold.get(), "min_strength": self.v_strength.get(),
             "snap": self.v_snap.get(), "cut_ratio": self.v_cut.get()}
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
            period=int(period) if period else None,
            threshold=float(self.v_threshold.get()),
            min_strength=float(self.v_strength.get()),
            snap=int(self.v_snap.get()), cut_ratio=float(self.v_cut.get()),
            spikes_only=self.v_spikes.get(), force=self.v_force.get())

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
        args = (list(self.files), opt, do_render, outdir, self.v_csv.get())
        self.worker = threading.Thread(target=self.run_jobs, args=args, daemon=True)
        self.worker.start()

    def run_jobs(self, files, opt, do_render, outdir, want_csv):
        log = lambda t: self.q.put(("log", t))
        cancel = self.cancel_flag.is_set
        n = len(files)
        for k, path in enumerate(files, 1):
            self.q.put(("status", f"{'RENDERING' if do_render else 'ANALYSING'} {k}/{n}"))
            self.q.put(("progress", (0, 1)))
            try:
                an = core.analyze(path, opt, log=log, cancel=cancel)
                if want_csv:
                    p = os.path.splitext(path)[0] + "_analysis.csv"
                    if outdir:
                        p = os.path.join(outdir, os.path.basename(p))
                    core.write_csv(an, p)
                    log(f"  CSV: {p}")
                if do_render and an["ok"]:
                    out = core.default_output(path, core.out_fps_for(an, opt), outdir)
                    core.render(an, out, opt, log=log,
                                progress=lambda i, t: self.q.put(("progress", (i, t))),
                                cancel=cancel)
                    log(f"  done: {out}")
            except core.Cancelled:
                log("  cancelled")
                break
            except Exception as e:
                log(f"  ERROR: {e}")
            log("")
        self.q.put(("done", None))

    def offer_rife_download(self, startup=False):
        if self.worker and self.worker.is_alive():
            return
        dest = rife_download_dir()
        msg = ("RIFE (rife-ncnn-vulkan) was not found.\n\n" if startup else "") + \
              ("Download the latest Windows release from GitHub (nihui/rife-ncnn-vulkan)?\n"
               "It is a few hundred MB and includes all models.\n\n"
               f"It will be unpacked to:\n{dest}")
        if not messagebox.askyesno("RIFE Gapfill", msg):
            return
        self.cancel_flag.clear()
        self.set_busy(True)
        self.v_status.set("DOWNLOADING RIFE")
        self.worker = threading.Thread(target=self.run_download, args=(dest,), daemon=True)
        self.worker.start()

    def run_download(self, dest):
        log = lambda t: self.q.put(("log", t))
        try:
            exe = download_rife(dest, log,
                                lambda i, t: self.q.put(("progress", (i, t))),
                                self.cancel_flag.is_set)
            self.q.put(("rife", exe))
        except core.Cancelled:
            log("  download cancelled")
        except Exception as e:
            log(f"  ERROR: download failed: {e}")
            log("  You can download it manually from "
                "github.com/nihui/rife-ncnn-vulkan/releases and use Browse…")
        log("")
        self.q.put(("done", None))

    def cancel(self):
        self.cancel_flag.set()
        self.v_status.set("CANCELLING")

    def set_busy(self, busy):
        st = "disabled" if busy else "normal"
        self.b_an.configure(state=st)
        self.b_go.configure(state=st)
        self.b_dl.configure(state=st)
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
                elif kind == "rife":
                    self.v_rife.set(val)
                    self.refresh_models()
                    self.save_settings()
                elif kind == "done":
                    self.set_busy(False)
                    self.v_status.set("READY")
                    self.pb["value"] = 0
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

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
    App(root)
    dark_titlebar(root)
    root.mainloop()


if __name__ == "__main__":
    main()
