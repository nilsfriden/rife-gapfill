# rife-gapfill

Finds periodically missing frames in a video and fills each gap with a
[RIFE](https://github.com/nihui/rife-ncnn-vulkan)-interpolated frame.

Built for AI-generated video (e.g. Seedance) that drops one frame at every
generation block boundary. The motion jumps once per block, typically every
24 frames, while the frame rate looks normal. The tool detects the cadence by
itself, works at any frame rate and any period, and writes ProRes with the
original frames untouched.

![RIFE Gapfill GUI](docs/screenshot.png)

## How it works

1. **Analysis:** decodes a downscaled grey copy and measures the mean
   absolute difference between consecutive frames, the same idea as a
   1-frame offset difference layer in Resolve. Each value is divided by the
   median of its neighbours to get a local "spike ratio".
2. **Cadence:** tests every period and phase and picks the grid where the
   ratio is consistently high. A gap shows up as a step about 1.5 to 2 times
   the normal motion.
3. **Tracking:** walks the grid and snaps each gap to the strongest spike
   within ±1 frame, so slow drift is followed. Still shots on the grid are
   filled too, which keeps the timing even.
4. **Render:** decodes full-quality RGB48 and inserts one RIFE frame before
   each gap. The output is ProRes 422 / 422 HQ / 4444. RIFE works in 8 bits,
   so its correction is laid on top of a 16-bit blend; areas without motion
   keep full precision.

### Output frame rate

Each gap gets one new frame, so a clip with a gap every *P* frames gains one
frame per *P*.

| Mode | Result |
|---|---|
| Same as source | Same fps; the clip gets longer (24 fps: 8.04 s → 8.375 s) |
| Restore original duration | fps × (P+1)/P; same duration (24 → 25 fps for P = 24) |
| Custom | Any rate |

Audio is not carried over.

## Requirements

- Windows (the GUI and RIFE binary). The core script also runs on Linux and macOS.
- [ffmpeg + ffprobe](https://ffmpeg.org/) (`winget install Gyan.FFmpeg`).
  The tool finds them on PATH, next to the exe, or in the WinGet package folder.
- [rife-ncnn-vulkan](https://github.com/nihui/rife-ncnn-vulkan/releases).
  Runs on any GPU through Vulkan. The GUI can download it for you.
- To run from source: Python 3.9+ with `numpy` and `pillow`.

## GUI

```
pip install numpy pillow
python gapfill_gui.py
```

Add clips, choose the output frame rate and codec, then click **Analyze** to
check the detection or **Render** to process. Hover over any control and its
help text appears in the panel above the buttons. Settings are saved in
`%APPDATA%\RifeGapfill\settings.json`.

### Build a standalone exe

```
pip install pyinstaller numpy pillow
pyinstaller --onefile --windowed --name RifeGapfill gapfill_gui.py
```

Copy `dist\RifeGapfill.exe` next to the `rife-ncnn-vulkan-…-windows` folder,
or use **Download…** in the GUI.

## Command line

```
python rife_gapfill.py clip.mp4 --analyze-only
python rife_gapfill.py clip.mp4
python rife_gapfill.py clip.mp4 --out-fps restore --prores 4444
```

Useful options:

| Option | Meaning |
|---|---|
| `--analyze-only` | Detect and write a per-frame CSV report; no render |
| `--period N` | Force a known period instead of detecting |
| `--spikes-only` | Fill only clear jumps, not grid positions in still shots |
| `--snap N` | Frames a gap may drift from the grid (default 1) |
| `--threshold`, `--min-strength`, `--cut-ratio` | Detection tuning |
| `--rife`, `--model`, `--ffmpeg`, `--ffprobe` | Tool paths if not found automatically |

Run `python rife_gapfill.py -h` for the full list.

## License

MIT. RIFE and rife-ncnn-vulkan are separate projects with their own licenses.
