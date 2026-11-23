# -*- coding: utf-8 -*-
"""OCR 引擎封装（RapidOCR，PP-OCRv4 模型）+ 传统模板匹配 + ROI 预处理 + 通用读图。

依赖：pip install rapidocr-onnxruntime
"""
import os

import cv2
import numpy as np

try:
    from rapidocr_onnxruntime import RapidOCR
    HAS_RAPID = True
except ImportError:
    HAS_RAPID = False

from paths import base_dir

# ---------- 传统模板匹配（毫秒级） ----------
ALPHABET = "0123456789.°'\"NSEWHDGALTm/:-"
FONT_DIR = r"C:\Windows\Fonts"
# bahnschrift = 微软的 DIN 1451 衍生字体，最接近大疆 OSD 的 DIN 风格
FONT_NAMES = ["bahnschrift.ttf", "arial.ttf", "arialbd.ttf", "consola.ttf", "courbd.ttf"]
TRAD_MIN_SCORE = 0.45   # 平均分数低于此值 -> 回退模型 OCR


class TraditionalReader:
    """固定字体 OSD 数值的传统读法：二值化 -> 连通域切字符 -> 模板匹配。

    模板在初始化时用 Windows 常见字体渲染（字符集 = 数字 + 度分秒 + 方位字母），
    字符图归一化到模板高度后做零均值归一化互相关（NCC）打分。
    read() 返回 (文本, 平均分数)，分数低时上层应回退模型 OCR。
    """

    def __init__(self):
        self.templates = []   # (char, np.uint8 二值模板)
        self._cache = {}      # 量化字符图 -> (字符, 分数)：容忍二值化噪声，命中率高
        for fname in FONT_NAMES:
            path = os.path.join(FONT_DIR, fname)
            if not os.path.exists(path):
                continue
            for size in (24, 32, 40):
                try:
                    from PIL import Image, ImageDraw, ImageFont
                    font = ImageFont.truetype(path, size)
                except Exception:
                    continue
                for ch in ALPHABET:
                    img = Image.new("L", (size + 6, size + 6), 0)
                    d = ImageDraw.Draw(img)
                    d.text((3, 3), ch, font=font, fill=255)
                    arr = np.array(img)
                    ys, xs = np.where(arr > 20)
                    if len(xs) == 0:
                        continue
                    arr = arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
                    self.templates.append((ch, arr))
        self._load_custom_templates()
        self._prep_template_groups()

    def _load_custom_templates(self):
        """加载从素材采集的自定义模板（像素级精确，天然胜过字体渲染模板）。"""
        custom_dir = os.path.join(base_dir(), "templates_custom")
        manifest = os.path.join(custom_dir, "manifest.json")
        if not os.path.exists(manifest):
            return
        try:
            import json
            data = json.load(open(manifest, encoding="utf-8"))
        except Exception:
            return
        for ch, files in data.items():
            for fn in files:
                p = os.path.join(custom_dir, fn)
                if not os.path.exists(p):
                    continue
                arr = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
                if arr is not None and arr.size > 0:
                    self.templates.append((ch, arr))

    def _prep_template_groups(self):
        """按模板高度分组，组内批量零均值归一化——匹配时一次矩阵乘算完一组。"""
        groups = {}
        for ch, tpl in self.templates:
            th, tw = tpl.shape[:2]
            if th < 3 or tw < 3:
                continue
            groups.setdefault(th, []).append((ch, tpl))
        self._tpl_groups = {}
        for th, items in groups.items():
            W = max(t.shape[1] for _, t in items)
            arr = np.zeros((len(items), th, W), dtype=np.float32)
            chars = []
            for i, (ch, tpl) in enumerate(items):
                arr[i, :th, :tpl.shape[1]] = tpl
                chars.append(ch)
            mu = arr.mean(axis=(1, 2), keepdims=True)
            sd = arr.std(axis=(1, 2), keepdims=True) + 1e-6
            arr = (arr - mu) / sd
            self._tpl_groups[th] = {"arr": arr, "chars": chars,
                                    "tws": np.array([t.shape[1] for _, t in items])}

    def _quant_key(self, cell):
        """缓存键 = 量化网格：容忍视频压缩导致的二值化微差。"""
        q = cv2.resize(cell, (14, 22), interpolation=cv2.INTER_AREA)
        q = (q > 30).astype(np.uint8)
        return q.tobytes()

    def match_char(self, cell):
        """cell: 二值单通道图。返回 (字符, 分数)。

        按高度分组批量匹配：字符图缩放到组高度，与该组全部模板
        做向量化互相关（左对齐 + 3 像素水平微移取最大），
        宽高比预筛先砍掉不可能候选。"""
        best, best_s = "?", -1.0
        h, w = cell.shape[:2]
        if h < 2 or w < 2:
            return "?", -1.0
        key = self._quant_key(cell)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        for th, g in self._tpl_groups.items():
            if th < h * 0.25 or th > h * 2.5:
                continue
            ratio = (g["tws"] / th) / max(w / h, 1e-3)
            keep = (ratio >= 0.35) & (ratio <= 3.0)
            if not keep.any():
                continue
            nw = max(int(w * th / h), 1)
            cell2 = cv2.resize(cell, (nw, th), interpolation=cv2.INTER_AREA)
            W = g["arr"].shape[2]
            canvas = np.zeros((th, nw + W + 2), dtype=np.float32)
            canvas[:, :nw] = cell2
            c = canvas - canvas.mean()
            c /= (c.std() + 1e-6)
            N = g["arr"].shape[0]
            tpl_canvas = np.zeros((N, th, nw + W + 2), dtype=np.float32)
            best_grp, best_s_grp, best_i = -1.0, -1.0, 0
            for dx in (0, 1, 2):   # 3 像素水平微移
                tpl_canvas[:, :, :] = 0
                tpl_canvas[:, :, dx:dx + W] = g["arr"]
                scores = (tpl_canvas * c[None]).mean(axis=(1, 2))
                i = int(np.argmax(scores))
                if scores[i] > best_s_grp:
                    best_s_grp = float(scores[i])
                    best_i = i
                    best_grp = float(scores[i])
            if best_s_grp > best_s:
                best_s = best_s_grp
                best = g["chars"][best_i]
        if len(self._cache) > 8000:
            self._cache.clear()
        self._cache[key] = (best, best_s)
        return best, best_s

    def read(self, bgr):
        """识别一个 ROI（素材约定：字符恒为白色）。返回 (文本, 平均分数)。"""
        text, score, _, _ = self.read_detailed(bgr)
        return text, score

    def segment(self, bgr):
        """二值化 + 连通域切字。返回 (bw, rects)，
        rects = [(x, y, w, h), ...]，x 递增（2x 放大坐标系），已滤噪声斑。"""
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        # 2x 放大帮助小字
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        _, bw = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        # 字=前景(255)，直接取连通域=字符
        n, labels, stats, cents = cv2.connectedComponentsWithStats(bw)
        comps = []
        for i in range(1, n):
            x, y, w, h, area = stats[i]
            if area >= 4 and h >= 3 and w >= 2:
                comps.append((x, y, w, h, area))
        if not comps:
            return bw, []
        comps.sort(key=lambda c: c[0])   # 按 x 排序 = 阅读顺序
        # 过滤异常大块（整块噪声）
        med_area = float(np.median([c[4] for c in comps]))
        rects = [(x, y, w, h) for x, y, w, h, area in comps if area <= med_area * 6]
        return bw, rects

    def read_detailed(self, bgr):
        """识别一个 ROI，返回 (文本, 平均分数, cells, bw)。
        cells = [(char, score, x, y, w, h), ...]，x 递增（2x 放大坐标系）；
        bw = 该 ROI 二值图（字=255），供逻辑模块做"间隔白像素"校验。

        二值化用 Otsu 全局阈值——自适应阈值在均匀背景上会把背景切成白色
        （其 -C 偏移的副作用），Otsu 无此问题；字符全白时两侧极性明确。"""
        bw, rects = self.segment(bgr)
        if not rects:
            return "", -1.0, [], bw
        cells = []   # (char, score, x, y, w, h)
        for x, y, w, h in rects:
            cell = bw[y:y + h, x:x + w]
            ch, s = self.match_char(cell)
            # 粘连处理：分数低且超宽 -> 尝试切分，分数提升才采纳
            if s < 0.55 and w > h * 1.15:
                parts = self._split_cells(bw, x, y, w, h)
                if len(parts) > 1:
                    pcs = []
                    for px, py, pw, ph in parts:
                        pc = bw[py:py + ph, px:px + pw]
                        ch2, s2 = self.match_char(pc)
                        pcs.append((ch2, s2, px, py, pw, ph))
                    if min(c[1] for c in pcs) > s:
                        cells.extend(pcs)
                        continue
            cells.append((ch, s, x, y, w, h))
        if not cells:
            return "", -1.0, [], bw
        return ("".join(c[0] for c in cells),
                float(np.mean([c[1] for c in cells])), cells, bw)

    def _split_cells(self, bw, x, y, w, h):
        """粘连字符切分：等宽基准（数字宽高比 ~0.62）+ 投影谷点微调。
        在每段等分线附近 ±20% 范围内找垂直投影最小的列（字符间缝隙）。"""
        est = max(h * 0.62, 3)
        n = max(int(round(w / est)), 1)
        if n <= 1:
            return [(x, y, w, h)]
        sub = bw[y:y + h, x:x + w]
        prof = sub.sum(axis=0)
        cuts = []
        for i in range(1, n):
            base = int(i * w / n)
            lo = max(1, int(base - 0.2 * w / n))
            hi = min(w - 1, int(base + 0.2 * w / n))
            best = lo + int(np.argmin(prof[lo:hi + 1]))
            cuts.append(best)
        cells = []
        prev = 0
        for c in cuts + [w]:
            if c - prev >= 2:
                cells.append((x + prev, y, c - prev, h))
            prev = c
        return cells


def imread_any(path):
    """读图，兼容非 ASCII 路径（cv2.imread 不支持中文路径）。"""
    if any(ord(c) > 127 for c in path):
        data = np.fromfile(path, dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    return cv2.imread(path)


def preprocess(bgr, scale=3.0):
    """放大 -> 灰度 -> 中值去噪 -> 自适应二值化 -> 小斑清除，返回单通道图。
    放大后长边封顶 960px：大 ROI 不放大过头，控制 OCR 耗时。
    中值滤波专杀椒盐噪声，形态学开运算清掉二值化后的小斑点——
    两者都能大幅减少检测阶段的候选框数量（OCR 慢的主因）。"""
    h, w = bgr.shape[:2]
    longest = max(h, w)
    if longest * scale > 960:
        scale = 960 / longest
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    big = cv2.resize(gray, (int(w * scale), int(h * scale)),
                     interpolation=cv2.INTER_CUBIC)
    blur = cv2.GaussianBlur(big, (3, 3), 0)
    bw = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY, 15, 10)
    return bw


class OCREngine:
    """RapidOCR 封装。recognize 返回 [(text, score, box4pts), ...]。"""

    def __init__(self):
        if not HAS_RAPID:
            raise RuntimeError(
                "未安装 rapidocr-onnxruntime，请先执行：\n"
                "pip install rapidocr-onnxruntime")
        self._eng = RapidOCR()

    def recognize(self, img, binary=False):
        """识别一张图（BGR 或二值化单通道）。返回 [(text, score, box), ...]。"""
        if binary and img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        result, _ = self._eng(img)
        if not result:
            return []
        return [(str(t), float(s), np.array(b, dtype=int)) for b, t, s in result]

    def recognize_roi(self, frame, x, y, w, h):
        """裁剪 ROI -> 预处理 -> OCR，返回 [(text, score), ...]。"""
        roi = frame[y:y + h, x:x + w]
        if roi.size == 0:
            return []
        bw = preprocess(roi)
        items = self.recognize(bw, binary=True)
        return [(t, s) for t, s, _ in items]

    def clean_roi_text(self, frame, x, y, w, h):
        """ROI 识别结果拼接清洗：去空白、拼接碎片。"""
        items = self.recognize_roi(frame, x, y, w, h)
        return "".join(t for t, _ in items).strip()
