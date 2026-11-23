# OSD-Extractor

Real-time text extraction from video OSD (On-Screen Display) overlays — altitude, voltage, speed, flight time, camera parameters, etc. — displayed live alongside the playing video.

**No neural network OCR at all**: pure traditional image processing (Otsu binarization → connected-component glyph segmentation → template matching with caching → rule-based logic correction), millisecond-level on CPU.

> 中文文档见 [doc/](doc/)（需求分析 / 方案选择 / 使用流程 / 开发历程 / 不足说明）。

## Features

- **Two-program workflow**: Annotator (calibrate ROIs, labels, units once) → Realtime extractor (play & extract)
- **One machine, one template**: ROIs + logic rules + label formats packed into a single template JSON
- **Logic analysis module**: unit-after-digits rule, colon allow-list, time auto-grouping, single-decimal-point policy (standout-gap detection + white-pixel verification), label format language (`?` = one digit, `*` = any digits split by separator position)
- **Font-adaptive**: template bank (Bahnschrift etc.) + capture font templates from real footage (pixel-exact matching, 0.9999+)
- **Performance**: change gating (unchanged ROIs skip recognition), quantized cache, vectorized template matching (height-grouped pre-normalization + batched correlation, 12x speedup), dual-thread capture/recognition
- **Bilingual UI**: Chinese by default; set `OSD_LANG=en` or drop a `lang.txt` containing `en` next to the program for English
- **Zero-dependency release**: PyInstaller exes (see `build.bat`)

## Quick start

Release binaries (Windows 10/11 x64, no Python needed): built via `build.bat` into `dist/`.

From source:

```bash
pip install -r requirements.txt        # opencv-python pillow numpy (rapidocr optional)
python src/roi_annotator.py            # Program 1: annotate ROIs
python src/realtime_ui.py              # Program 2: realtime extraction
```

Usage walkthrough: [doc/使用流程.md](doc/使用流程.md) (中文).

## How it works

```
video/image → frame sampling (interval) → ROI crop (template) → change gate
→ preprocessing (gray / upscale / Otsu) → glyph segmentation (merged-char splitting)
→ template matching (cache + vectorized) → logic analysis (unit / colon / decimal / format)
→ panel + on-screen overlay output
```

## Repository layout

```
src/
  roi_annotator.py    # Program 1: ROI annotator (font capture, logic & format dialogs)
  realtime_ui.py      # Program 2: realtime extractor UI
  ocr_engine.py       # traditional matching engine (segmentation / matching / cache)
  logic_analyzer.py   # rule-based post-processing (configurable, saved per template)
  roi_template.py     # template load/save & scaling
  i18n.py             # zh/en UI strings
  paths.py            # frozen/source path base
doc/                  # requirements, design choices, usage, dev history (中文)
samples/              # your own footage goes here (not tracked)
roi_template.json     # the device template (ROIs + logic + formats)
run_annotator.bat / run_realtime.bat   # dev launchers
build.bat             # PyInstaller packaging
```

## Template format

```json
{
  "source_width": 1918, "source_height": 1078,
  "logic": {"colon_labels": ["Time"], "time_labels": ["Time"],
            "text_unit_labels": ["Altitude"], "decimal_gap_ratio": 0.4},
  "formats": {"Time": ["??:??"], "Camera": ["*:*"]},
  "rois": [{"name": "Altitude", "unit": "m", "with_unit_letters": true,
            "x": 895, "y": 158, "w": 216, "h": 63}]
}
```

Full evolution & pitfalls log: [doc/开发历程.md](doc/开发历程.md) · Known limitations: [doc/不足说明.md](doc/不足说明.md)

## License

MIT © 2026 龚界霖 (Jielin Gong)
