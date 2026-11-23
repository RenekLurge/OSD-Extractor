# -*- coding: utf-8 -*-
"""ROI 模板的读写与缩放。模板 JSON 结构：
{
  "source_width": 1920, "source_height": 1080,
  "rois": [
    {"name": "航向角", "x": 100, "y": 50, "w": 200, "h": 40},
    ...
  ]
}
坐标为源素材分辨率；实时程序按当前视频分辨率等比缩放。
"""
import json


def load_template(path):
    with open(path, "r", encoding="utf-8") as f:
        t = json.load(f)
    t.setdefault("source_width", None)
    t.setdefault("source_height", None)
    return t


def save_template(path, template):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)


def scale_rois(template, video_w, video_h):
    """模板坐标 -> 当前视频坐标（按比例缩放，兼容分辨率变化）。"""
    tw = template["source_width"] or video_w
    th = template["source_height"] or video_h
    sx, sy = video_w / tw, video_h / th
    out = []
    for r in template["rois"]:
        out.append({
            "name": r["name"],
            "unit": r.get("unit", ""),
            "with_unit_letters": bool(r.get("with_unit_letters", False)),
            "x": int(r["x"] * sx), "y": int(r["y"] * sy),
            "w": max(4, int(r["w"] * sx)), "h": max(4, int(r["h"] * sy)),
        })
    return out
