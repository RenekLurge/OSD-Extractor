# -*- coding: utf-8 -*-
"""引擎对决诊断：同一帧 × 每个 ROI × 5 种识别配方，对比精度与速度。

用法（在 src 目录下）：
  python diagnose.py <视频或图片路径> <roi_template.json>

输出每个 ROI 五种配方的 文本 + 耗时，把整屏结果贴回给 Claude 选配方。
配方：
  A 原彩色 3x        （不二值化，直接喂彩色）
  B 灰度 3x
  C 自适应二值化     （当前实时程序用的配方）
  D Otsu 二值化
  E Windows 系统 OCR （微软内置引擎，硬件加速，2x）
"""
import asyncio
import os
import sys
import tempfile
import time

import cv2

from ocr_engine import OCREngine, imread_any, preprocess
from roi_template import load_template, scale_rois

try:
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.storage import StorageFile, FileAccessMode
    WIN_OCR = True
except Exception:
    WIN_OCR = False


def upscale(img, scale):
    return cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def gray3(img):
    g = cv2.cvtColor(upscale(img, 3), cv2.COLOR_BGR2GRAY)
    return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)


def otsu3(img):
    g = cv2.cvtColor(upscale(img, 3), cv2.COLOR_BGR2GRAY)
    bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    return cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)


async def _win_ocr(path):
    f = await StorageFile.get_file_from_path_async(path)
    s = await f.open_async(FileAccessMode.READ)
    dec = await BitmapDecoder.create_async(s)
    bmp = await dec.get_software_bitmap_async()
    eng = OcrEngine.try_create_from_user_profile_languages()
    if eng is None:
        eng = OcrEngine.try_create_from_language(Language("zh-Hans"))
    if eng is None:
        return "(无可用 Windows OCR 引擎)"
    res = await eng.recognize_async(bmp)
    return " | ".join(l.text for l in res.lines)


def win_ocr(bgr, scale):
    img = upscale(bgr, scale)
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    cv2.imwrite(path, img)
    try:
        return asyncio.run(_win_ocr(path))
    finally:
        os.remove(path)


def main():
    if len(sys.argv) < 3:
        print("用法: python diagnose.py <视频或图片路径> <roi_template.json>")
        return
    media, tpl_path = sys.argv[1], sys.argv[2]
    if any(ord(c) > 127 for c in media):
        print("素材路径不能含中文，先改英文名")
        return

    ext = os.path.splitext(media)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".bmp"):
        frame = imread_any(media)
        src = "图片"
    else:
        cap = cv2.VideoCapture(media)
        if not cap.isOpened():
            print("视频打不开")
            return
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * 0.25))
        ok, frame = cap.read()
        if not ok:
            print("读帧失败")
            return
        src = "视频"

    tpl = load_template(tpl_path)
    rois = scale_rois(tpl, frame.shape[1], frame.shape[0])
    eng = OCREngine()

    print("素材: %s (%s)  帧: %dx%d  模板: %d 个 ROI"
          % (os.path.basename(media), src, frame.shape[1], frame.shape[0], len(rois)))
    print("Windows OCR 可用: %s" % ("是" if WIN_OCR else "否"))
    print("（识别中，约 20~60 秒，请等待）\n")

    for r in rois:
        x, y, w, h = r["x"], r["y"], r["w"], r["h"]
        crop = frame[y:y + h, x:x + w]
        if crop.size == 0:
            print("\n[%s] 框越界，跳过" % r["name"])
            continue
        print("\n[%s] 框 %dx%d @ (%d,%d)" % (r["name"], w, h, x, y))
        recipes = [
            ("A 原彩色3x", lambda: eng.recognize(upscale(crop, 3))),
            ("B 灰度3x", lambda: eng.recognize(gray3(crop))),
            ("C 自适应二值", lambda: eng.recognize(preprocess(crop), binary=True)),
            ("D Otsu二值", lambda: eng.recognize(otsu3(crop), binary=True)),
        ]
        for name, fn in recipes:
            t0 = time.time()
            try:
                items = fn()
                texts = " ".join(t for t, _ in items)
            except Exception as e:
                texts = "ERR %s" % e
            print("  %-12s %6d ms | %r" % (name, int((time.time() - t0) * 1000), texts))
        t0 = time.time()
        try:
            texts = win_ocr(crop, 2) if WIN_OCR else "(未安装 winsdk)"
        except Exception as e:
            texts = "ERR %s" % e
        print("  %-12s %6d ms | %r" % ("E Win系统OCR", int((time.time() - t0) * 1000), texts))


if __name__ == "__main__":
    main()
