# -*- coding: utf-8 -*-
"""程序 1：ROI 标注工具（离线标定）。

功能：
  1. 打开视频/图片，滑条选帧
  2. "预识别"：OCR 找出当前帧全部文字区域（红框），帮助定位 OSD
     "自动采样预识别"：对视频 25%/50%/75% 三处抽帧 OCR，日志汇总
  3. 画框与编辑：
     - 空白处按住左键拖拽 = 画新框
     - 点击已有框 = 选中（黄色高亮，四角/四边出现黄色手柄）
     - 拖动框体 = 移动；拖动手柄 = 拉伸调整
     - 选中后改右侧标签/单位下拉 = 直接应用到该框
     - 右侧列表双击 = 删除该框
  4. 保存 roi_template.json（含源分辨率坐标 + 单位），供实时程序使用

用法：python roi_annotator.py
"""
import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
from PIL import Image, ImageTk

from i18n import t
from ocr_engine import OCREngine, imread_any, TraditionalReader
from logic_analyzer import load_config, logic_from_template
from paths import base_dir
from roi_template import load_template, save_template

LABELS = ["升降速度", "距出发点水平距离", "海拔高度", "起飞高度", "录制时间",
          "飞行时间", "相机参数", "单芯电压", "总电压", "总功率", "飞行速度",
          "信号强度", "眼镜电压", "电流", "温度", "已使用电量", "图传码率",
          "链路质量", "自定义"]
UNITS = ["无", "m", "V", "W", "km/h", "A", "°C", "MAh", "Mbps", "自定义"]
# 标签 -> 默认单位 的关联（选标签自动带出单位，仍可手动改）
# 单位标注为 "" 的标签若在 TEXT_UNIT_LABELS 中，则单位靠识别文本（如 m/km）
LABEL_UNITS = {
    "升降速度": "",
    "距出发点水平距离": "",
    "海拔高度": "",
    "起飞高度": "",
    "录制时间": "",
    "飞行时间": "",
    "相机参数": "",
    "单芯电压": "V",
    "总电压": "V",
    "总功率": "W",
    "飞行速度": "km/h",
    "信号强度": "",
    "眼镜电压": "V",
    "电流": "A",
    "温度": "°C",
    "已使用电量": "MAh",
    "图传码率": "Mbps",
    "链路质量": "",
}
TEMPLATE_NAME = "roi_template.json"


class App:
    def __init__(self, root):
        self.root = root
        root.title(t("annotator_title"))
        root.geometry("1360x760")

        self.cap = None
        self.frame = None          # 原始分辨率 BGR
        self.frame_idx = 0
        self.total = 0
        self.disp_scale = 1.0      # 显示尺寸 / 原始尺寸
        self.rois = []             # 原分辨率坐标 [{"name","unit","x","y","w","h"}]
        self.ocr = None
        self.selected = None       # 选中的 ROI 下标
        self._mode = None          # None / "new" / "move" / "resize"
        self._hid = None           # resize 手柄 id
        self._box0 = None          # resize 起始框
        self._drag0 = None
        self.drag_start = None
        self.drag_rect = None
        self._syncing = False
        self._slider_lock = False
        self._imgtk = None
        self.capture_mode = False
        self._tr_cap = None
        self._cap_cells = []
        self._cap_imgs = []
        self.logic_cfg = load_config()   # 逻辑设置随模板保存（一机一模板）
        self.formats = {}                # 标签格式定义：{标签: ["??:??", ...]}

        self._build_ui()
        self.status(t("status_start"))

    # ---------------- UI ----------------
    def _build_ui(self):
        left = tk.Frame(self.root)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(left, bg="#202020", width=980, height=560)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

        bar = tk.Frame(left)
        bar.pack(fill=tk.X, padx=6, pady=3)
        self.slider = tk.Scale(bar, from_=0, to=1, orient=tk.HORIZONTAL,
                               command=self.on_slider, length=640)
        self.slider.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.frame_lab = tk.Label(bar, text=t("frame_lab") % ("-", "-", "-", "-"))
        self.frame_lab.pack(side=tk.LEFT, padx=8)

        right = tk.Frame(self.root, width=350)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=8, pady=8)
        right.pack_propagate(False)

        ttk.Button(right, text=t("btn_open"), command=self.open_file).pack(fill=tk.X, pady=2)
        ttk.Button(right, text=t("btn_ocr"), command=self.do_ocr).pack(fill=tk.X, pady=2)
        ttk.Button(right, text=t("btn_auto_ocr"), command=self.do_auto_ocr).pack(fill=tk.X, pady=2)
        ttk.Button(right, text=t("btn_clear_ocr"), command=self.clear_ocr).pack(fill=tk.X, pady=2)
        ttk.Separator(right).pack(fill=tk.X, pady=6)

        tk.Label(right, text=t("lbl_label")).pack(anchor=tk.W)
        self.label_var = tk.StringVar(value=LABELS[0])
        cb = ttk.Combobox(right, textvariable=self.label_var, values=LABELS, state="readonly")
        cb.pack(fill=tk.X, pady=2)
        cb.bind("<<ComboboxSelected>>", self._on_label_change)
        tk.Label(right, text=t("lbl_custom_label")).pack(anchor=tk.W)
        self.custom_entry = tk.Entry(right)
        self.custom_entry.pack(fill=tk.X, pady=2)
        self.custom_entry.bind("<KeyRelease>", self._on_label_change)
        tk.Label(right, text=t("lbl_unit")).pack(anchor=tk.W)
        self.unit_var = tk.StringVar(value=UNITS[0])
        ucb = ttk.Combobox(right, textvariable=self.unit_var, values=UNITS, state="readonly")
        ucb.pack(fill=tk.X, pady=2)
        ucb.bind("<<ComboboxSelected>>", self._on_unit_change)
        tk.Label(right, text=t("lbl_custom_unit")).pack(anchor=tk.W)
        self.unit_entry = tk.Entry(right)
        self.unit_entry.pack(fill=tk.X, pady=2)
        self.unit_entry.bind("<KeyRelease>", self._on_unit_change)
        self.text_unit_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text=t("lbl_text_unit"),
                        variable=self.text_unit_var,
                        command=self._on_text_unit_change).pack(anchor=tk.W, pady=2)
        tk.Label(right, text=t("hint_draw"),
                 fg="#333").pack(anchor=tk.W, pady=2)
        ttk.Separator(right).pack(fill=tk.X, pady=6)

        tk.Label(right, text=t("lbl_list")).pack(anchor=tk.W)
        self.roi_list = tk.Listbox(right, height=10)
        self.roi_list.pack(fill=tk.BOTH, expand=True, pady=2)
        self.roi_list.bind("<<ListboxSelect>>", self.on_list_select)
        self.roi_list.bind("<Double-Button-1>", self.del_roi)
        ttk.Separator(right).pack(fill=tk.X, pady=6)

        ttk.Button(right, text=t("btn_load_tpl"), command=self.load).pack(fill=tk.X, pady=2)
        ttk.Button(right, text=t("btn_save_tpl"), command=self.save).pack(fill=tk.X, pady=2)
        ttk.Button(right, text=t("btn_capture"),
                   command=self.toggle_capture).pack(fill=tk.X, pady=2)
        ttk.Button(right, text=t("btn_logic"),
                   command=self.open_logic_dialog).pack(fill=tk.X, pady=2)
        ttk.Button(right, text=t("btn_format"),
                   command=self.open_format_dialog).pack(fill=tk.X, pady=2)
        self.status_var = tk.StringVar(value="")
        tk.Label(right, textvariable=self.status_var, fg="#555",
                 wraplength=330, justify=tk.LEFT).pack(fill=tk.X, pady=4)

    # ---------------- 文件与帧 ----------------
    def open_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("Video/Image", "*.mp4 *.avi *.mov *.mkv *.m4s *.jpg *.jpeg *.png *.bmp"),
                       ("All files", "*.*")])
        if not path:
            return
        if any(ord(c) > 127 for c in path):
            messagebox.showerror("Error", t("msg_bad_path"))
            return
        ext = os.path.splitext(path)[1].lower()
        if ext in (".jpg", ".jpeg", ".png", ".bmp"):
            img = imread_any(path)
            if img is None:
                messagebox.showerror("Error", t("msg_img_fail"))
                return
            self.cap = None
            self.frame = img
            self.total = 1
            self.show_frame()
            return
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            messagebox.showerror("Error", t("msg_video_fail"))
            return
        self.cap = cap
        self.total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        self._slider_lock = True
        self.slider.config(to=max(self.total - 1, 1))
        self._slider_lock = False
        self.show_frame_at(0)

    def show_frame_at(self, idx):
        if self.cap is not None:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, img = self.cap.read()
            if not ok:
                return
            self.frame = img
        self.frame_idx = idx
        self.show_frame()

    def show_frame(self):
        if self.frame is None:
            return
        h, w = self.frame.shape[:2]
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 10 or ch < 10:
            cw, ch = 980, 560
        self.disp_scale = min(cw / w, ch / h)
        dw, dh = int(w * self.disp_scale), int(h * self.disp_scale)
        rgb = cv2.cvtColor(self.frame, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb).resize((dw, dh))
        self._imgtk = ImageTk.PhotoImage(pil)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self._imgtk)
        self.frame_lab.config(text=t("frame_lab") % (self.frame_idx + 1, self.total, w, h))
        self.draw_rois()

    def on_slider(self, val):
        if self._slider_lock or self.cap is None:
            return
        self.show_frame_at(int(float(val)))

    # ---------------- OCR 预识别 ----------------
    def get_engine(self):
        if self.ocr is None:
            self.status(t("status_ocr_init"))
            self.root.update_idletasks()
            try:
                self.ocr = OCREngine()
            except RuntimeError as e:
                messagebox.showerror("Error", str(e))
                return None
        return self.ocr

    def do_ocr(self):
        eng = self.get_engine()
        if eng is None or self.frame is None:
            return
        self.status("OCR running...")
        self.root.update_idletasks()
        items = eng.recognize(self.frame)
        self.canvas.delete("ocr")
        for text, score, box in items:
            pts = [(int(p[0] * self.disp_scale), int(p[1] * self.disp_scale)) for p in box]
            self.canvas.create_polygon(pts, outline="red", width=2, fill="", tags="ocr")
            self.canvas.create_text(pts[0][0], pts[0][1] - 8, anchor=tk.SW,
                                    text="%s(%.2f)" % (text, score),
                                    fill="red", font=("", 8), tags="ocr")
        self.status(t("status_ocr_done") % len(items))

    def do_auto_ocr(self):
        eng = self.get_engine()
        if eng is None or self.cap is None:
            self.status(t("status_auto_only"))
            return
        self.status("Auto sampling 3 frames...")
        self.root.update_idletasks()
        log = []
        best_idx, best_n = 0, -1
        for frac in (0.25, 0.5, 0.75):
            idx = min(int(self.total * frac), max(self.total - 1, 0))
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, img = self.cap.read()
            if not ok:
                continue
            items = eng.recognize(img)
            texts = ", ".join(t for t, _, _ in items) or "(无)"
            log.append("帧 %d (%.0f%%)：%s" % (idx + 1, frac * 100, texts))
            if len(items) > best_n:
                best_n, best_idx = len(items), idx
        self.canvas.delete("ocr")
        self.show_frame_at(best_idx)
        self.canvas.delete("ocr")
        for text, score, box in eng.recognize(self.frame):
            pts = [(int(p[0] * self.disp_scale), int(p[1] * self.disp_scale)) for p in box]
            self.canvas.create_polygon(pts, outline="red", width=2, fill="", tags="ocr")
            self.canvas.create_text(pts[0][0], pts[0][1] - 8, anchor=tk.SW,
                                    text="%s" % text, fill="red", font=("", 8), tags="ocr")
        self.status("采样完成：\n" + "\n".join(log))

    def clear_ocr(self):
        self.canvas.delete("ocr")
        self.status(t("status_ocr_clear"))

    # ---------------- ROI 画框与编辑 ----------------
    def _inside(self, r, ox, oy):
        return r["x"] <= ox <= r["x"] + r["w"] and r["y"] <= oy <= r["y"] + r["h"]

    def _hit_handle(self, r, ox, oy, tol):
        x, y, w, h = r["x"], r["y"], r["w"], r["h"]
        pts = {"nw": (x, y), "n": (x + w / 2, y), "ne": (x + w, y),
               "e": (x + w, y + h / 2), "se": (x + w, y + h),
               "s": (x + w / 2, y + h), "sw": (x, y + h), "w": (x, y + h / 2)}
        for k, (px, py) in pts.items():
            if abs(ox - px) <= tol and abs(oy - py) <= tol:
                return k
        return None

    def on_press(self, e):
        if self.frame is None:
            return
        ox, oy = e.x / self.disp_scale, e.y / self.disp_scale
        tol = 8 / self.disp_scale
        # 1) 选中框的手柄
        if self.selected is not None and self.selected < len(self.rois):
            r = self.rois[self.selected]
            hid = self._hit_handle(r, ox, oy, tol)
            if hid is not None:
                self._mode, self._hid = "resize", hid
                self._box0 = dict(r)
                self._drag0 = (ox, oy)
                return
            if self._inside(r, ox, oy):
                self._mode = "move"
                self._drag0 = (ox - r["x"], oy - r["y"])
                return
        # 2) 其他框：选中并开始拖动
        for i in range(len(self.rois) - 1, -1, -1):
            if self._inside(self.rois[i], ox, oy):
                self.selected = i
                self._sync_combos()
                self.draw_rois()
                self._mode = "move"
                self._drag0 = (ox - self.rois[i]["x"], oy - self.rois[i]["y"])
                return
        # 3) 空白处：取消选中，画新框
        self.selected = None
        self._sync_combos()
        self.draw_rois()
        self.drag_start = (e.x, e.y)
        self.drag_rect = self.canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="lime", width=2)
        self._mode = "new"

    def on_drag(self, e):
        if self.frame is None:
            return
        if self._mode == "new":
            if self.drag_rect is not None:
                x0, y0 = self.drag_start
                self.canvas.coords(self.drag_rect, x0, y0, e.x, e.y)
            return
        if self.selected is None or self.selected >= len(self.rois):
            return
        r = self.rois[self.selected]
        ox, oy = e.x / self.disp_scale, e.y / self.disp_scale
        if self._mode == "move":
            r["x"] = max(0, min(int(ox - self._drag0[0]), self.frame.shape[1] - r["w"]))
            r["y"] = max(0, min(int(oy - self._drag0[1]), self.frame.shape[0] - r["h"]))
        elif self._mode == "resize":
            b = self._box0
            x0, y0, x1, y1 = b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]
            if "w" in self._hid:
                x0 = ox
            if "e" in self._hid:
                x1 = ox
            if "n" in self._hid:
                y0 = oy
            if "s" in self._hid:
                y1 = oy
            if x1 < x0:
                x0, x1 = x1, x0
            if y1 < y0:
                y0, y1 = y1, y0
            r["x"], r["y"] = max(0, int(x0)), max(0, int(y0))
            r["w"], r["h"] = max(4, int(x1 - x0)), max(4, int(y1 - y0))
        self.draw_rois()

    def on_release(self, e):
        if self._mode == "new":
            if self.drag_rect is not None:
                x0, y0 = self.drag_start
                x1, y1 = e.x, e.y
                self.canvas.delete(self.drag_rect)
                self.drag_rect = None
                if self.capture_mode:
                    self.capture_mode = False
                    self._mode = None
                    self._capture_region(x0, y0, x1, y1)
                    return
                if abs(x1 - x0) >= 6 and abs(y1 - y0) >= 6:
                    label = self.label_var.get()
                    if label == "自定义":
                        label = self.custom_entry.get().strip() or t("unnamed")
                    unit = self.unit_var.get()
                    if unit == "自定义":
                        unit = self.unit_entry.get().strip()
                    elif unit == "无":
                        unit = ""
                    x, y = min(x0, x1), min(y0, y1)
                    w, h = abs(x1 - x0), abs(y1 - y0)
                    self.rois.append({"name": label, "unit": unit,
                                      "with_unit_letters": bool(self.text_unit_var.get()),
                                      "x": int(x / self.disp_scale),
                                      "y": int(y / self.disp_scale),
                                      "w": int(w / self.disp_scale),
                                      "h": int(h / self.disp_scale)})
                    self.selected = len(self.rois) - 1
                    self._sync_combos()
                    self.status(t("status_added") % label)
        self._mode = None
        self._hid = None
        self._box0 = None
        self.draw_rois()

    def draw_rois(self):
        self.canvas.delete("roi")
        for i, r in enumerate(self.rois):
            col = "yellow" if i == self.selected else "lime"
            x0, y0 = r["x"] * self.disp_scale, r["y"] * self.disp_scale
            x1, y1 = (r["x"] + r["w"]) * self.disp_scale, (r["y"] + r["h"]) * self.disp_scale
            self.canvas.create_rectangle(x0, y0, x1, y1, outline=col, width=2, tags="roi")
            self.canvas.create_text(x0, max(y0 - 8, 0), anchor=tk.SW, tags="roi",
                                    text="%d %s" % (i + 1, r["name"]),
                                    fill=col, font=("", 9))
        # 选中框的手柄
        if self.selected is not None and self.selected < len(self.rois):
            r = self.rois[self.selected]
            x, y = r["x"] * self.disp_scale, r["y"] * self.disp_scale
            w, h = r["w"] * self.disp_scale, r["h"] * self.disp_scale
            pts = [(x, y), (x + w / 2, y), (x + w, y), (x + w, y + h / 2),
                   (x + w, y + h), (x + w / 2, y + h), (x, y + h), (x, y + h / 2)]
            for px, py in pts:
                self.canvas.create_rectangle(px - 4, py - 4, px + 4, py + 4,
                                             fill="yellow", outline="black", tags="roi")
        self.roi_list.delete(0, tk.END)
        for i, r in enumerate(self.rois):
            unit = ("[%s]" % r["unit"]) if r.get("unit") else ""
            self.roi_list.insert(tk.END, "%d. %s%s  (%d,%d %dx%d)" %
                                 (i + 1, r["name"], unit, r["x"], r["y"], r["w"], r["h"]))
        if self.selected is not None and self.selected < len(self.rois):
            self.roi_list.selection_set(self.selected)

    def _sync_combos(self):
        self._syncing = True
        if self.selected is not None and self.selected < len(self.rois):
            r = self.rois[self.selected]
            if r["name"] in LABELS:
                self.label_var.set(r["name"])
                self.custom_entry.delete(0, tk.END)
            else:
                self.label_var.set("自定义")
                self.custom_entry.delete(0, tk.END)
                self.custom_entry.insert(0, r["name"])
            u = r.get("unit", "")
            if u in UNITS:
                self.unit_var.set(u)
                self.unit_entry.delete(0, tk.END)
            else:
                self.unit_var.set("自定义")
                self.unit_entry.delete(0, tk.END)
                self.unit_entry.insert(0, u)
            self.text_unit_var.set(bool(r.get("with_unit_letters", False)))
        self._syncing = False

    def _on_label_change(self, e=None):
        if self._syncing:
            return
        label = self.label_var.get()
        # 标签与单位联动：选标签自动带出默认单位
        if label in LABEL_UNITS:
            mapped = LABEL_UNITS[label]
            self._syncing = True
            self.unit_var.set(mapped if mapped else "无")
            self.unit_entry.delete(0, tk.END)
            self.text_unit_var.set(label in self.logic_cfg["text_unit_labels"])
            self._syncing = False
        if self.selected is not None and self.selected < len(self.rois):
            r = self.rois[self.selected]
            r["name"] = self.custom_entry.get().strip() or "未命名" if label == "自定义" else label
            u = self.unit_var.get()
            r["unit"] = self.unit_entry.get().strip() if u == "自定义" else ("" if u == "无" else u)
            r["with_unit_letters"] = bool(self.text_unit_var.get())
            self.draw_rois()

    def _on_text_unit_change(self):
        if self._syncing or self.selected is None or self.selected >= len(self.rois):
            return
        self.rois[self.selected]["with_unit_letters"] = bool(self.text_unit_var.get())
        self.draw_rois()

    def _on_unit_change(self, e=None):
        if self._syncing or self.selected is None or self.selected >= len(self.rois):
            return
        r = self.rois[self.selected]
        u = self.unit_var.get()
        r["unit"] = self.unit_entry.get().strip() if u == "自定义" else ("" if u == "无" else u)
        self.draw_rois()

    def on_list_select(self, e):
        sel = self.roi_list.curselection()
        if sel:
            self.selected = sel[0]
            self._sync_combos()
            self.draw_rois()

    def del_roi(self, e=None):
        sel = self.roi_list.curselection()
        if not sel:
            return
        i = sel[0]
        del self.rois[i]
        if self.rois:
            self.selected = min(i, len(self.rois) - 1)
        else:
            self.selected = None
        self._sync_combos()
        self.draw_rois()

    # ---------------- 逻辑设置 ----------------
    def open_logic_dialog(self):
        cfg = dict(self.logic_cfg)
        dlg = tk.Toplevel(self.root)
        dlg.title(t("logic_dlg_title"))

        tk.Label(dlg, text=t("logic_colon")).pack(anchor=tk.W, padx=8, pady=(8, 0))
        colon_vars = {}
        f1 = tk.Frame(dlg)
        f1.pack(padx=8)
        for i, lab in enumerate(LABELS[:-1]):
            v = tk.BooleanVar(value=lab in cfg["colon_labels"])
            colon_vars[lab] = v
            ttk.Checkbutton(f1, text=lab, variable=v).grid(
                row=i // 4, column=i % 4, sticky=tk.W, padx=4)

        tk.Label(dlg, text=t("logic_time")).pack(anchor=tk.W, padx=8, pady=(8, 0))
        time_vars = {}
        f2 = tk.Frame(dlg)
        f2.pack(padx=8)
        for i, lab in enumerate(LABELS[:-1]):
            v = tk.BooleanVar(value=lab in cfg["time_labels"])
            time_vars[lab] = v
            ttk.Checkbutton(f2, text=lab, variable=v).grid(
                row=i // 4, column=i % 4, sticky=tk.W, padx=4)

        tk.Label(dlg, text=t("logic_textunit")).pack(anchor=tk.W, padx=8, pady=(8, 0))
        unit_vars = {}
        f3 = tk.Frame(dlg)
        f3.pack(padx=8)
        for i, lab in enumerate(LABELS[:-1]):
            v = tk.BooleanVar(value=lab in cfg["text_unit_labels"])
            unit_vars[lab] = v
            ttk.Checkbutton(f3, text=lab, variable=v).grid(
                row=i // 4, column=i % 4, sticky=tk.W, padx=4)

        tk.Label(dlg, text=t("logic_nodec")).pack(anchor=tk.W, padx=8, pady=(8, 0))
        nodec_vars = {}
        f3b = tk.Frame(dlg)
        f3b.pack(padx=8)
        for i, lab in enumerate(LABELS[:-1]):
            v = tk.BooleanVar(value=lab in cfg.get("no_decimal_labels", []))
            nodec_vars[lab] = v
            ttk.Checkbutton(f3b, text=lab, variable=v).grid(
                row=i // 4, column=i % 4, sticky=tk.W, padx=4)

        ttk.Separator(dlg).pack(fill=tk.X, padx=8, pady=6)
        tk.Label(dlg, text=t("logic_dec")).pack(anchor=tk.W, padx=8)
        f4 = tk.Frame(dlg)
        f4.pack(padx=8)
        rows = list(zip(t("logic_rows"), ["decimal_gap_ratio", "decimal_gap_single",
                                          "decimal_standout_mult", "decimal_move_mult"]))
        ents = {}
        for i, (txt, key) in enumerate(rows):
            tk.Label(f4, text=txt).grid(row=i, column=0, sticky=tk.W, pady=1)
            e = tk.Entry(f4, width=8)
            e.insert(0, str(cfg[key]))
            e.grid(row=i, column=1, sticky=tk.W)
            ents[key] = e
        en_ins = tk.BooleanVar(value=cfg["enable_insert"])
        en_mov = tk.BooleanVar(value=cfg["enable_move"])
        en_ink = tk.BooleanVar(value=cfg.get("gap_pixel_check", True))
        ttk.Checkbutton(f4, text=t("logic_ins"), variable=en_ins).grid(row=4, column=0, sticky=tk.W)
        ttk.Checkbutton(f4, text=t("logic_mov"), variable=en_mov).grid(row=4, column=1, sticky=tk.W)
        ttk.Checkbutton(f4, text=t("logic_ink"),
                        variable=en_ink).grid(row=5, column=0, columnspan=2, sticky=tk.W)

        def do_save():
            new_cfg = dict(cfg)
            new_cfg["colon_labels"] = [l for l, v in colon_vars.items() if v.get()]
            new_cfg["time_labels"] = [l for l, v in time_vars.items() if v.get()]
            new_cfg["text_unit_labels"] = [l for l, v in unit_vars.items() if v.get()]
            new_cfg["no_decimal_labels"] = [l for l, v in nodec_vars.items() if v.get()]
            for key, e in ents.items():
                try:
                    new_cfg[key] = float(e.get())
                except ValueError:
                    messagebox.showwarning("Warning", t("logic_badnum") % key)
                    return
            new_cfg["enable_insert"] = en_ins.get()
            new_cfg["enable_move"] = en_mov.get()
            new_cfg["gap_pixel_check"] = en_ink.get()
            self.logic_cfg = new_cfg
            dlg.destroy()
            self.status(t("status_logic_saved"))

        tk.Button(dlg, text=t("logic_save"), command=do_save).pack(pady=8)

    # ---------------- 标签格式定义 ----------------
    def open_format_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title(t("fmt_dlg_title"))
        tk.Label(dlg, text=t("fmt_hint"),
                 justify=tk.LEFT).pack(padx=8, pady=4, anchor=tk.W)
        f1 = tk.Frame(dlg)
        f1.pack(padx=8, pady=2)
        tk.Label(f1, text=t("fmt_pick")).pack(side=tk.LEFT)
        label_var = tk.StringVar(value=LABELS[0])
        cb = ttk.Combobox(f1, textvariable=label_var, values=LABELS[:-1],
                          state="readonly")
        cb.pack(side=tk.LEFT, padx=4)
        lst = tk.Listbox(dlg, height=8, width=40)
        lst.pack(padx=8)

        def refresh():
            lst.delete(0, tk.END)
            for f in self.formats.get(label_var.get(), []):
                lst.insert(tk.END, f)

        cb.bind("<<ComboboxSelected>>", lambda e: refresh())
        refresh()
        f2 = tk.Frame(dlg)
        f2.pack(padx=8, pady=4)
        tk.Label(f2, text=t("fmt_new")).pack(side=tk.LEFT)
        entry = tk.Entry(f2, width=20)
        entry.pack(side=tk.LEFT, padx=4)

        def add():
            s = entry.get().strip()
            if not s:
                return
            self.formats.setdefault(label_var.get(), []).append(s)
            entry.delete(0, tk.END)
            refresh()

        def rem():
            sel = lst.curselection()
            if sel and label_var.get() in self.formats:
                del self.formats[label_var.get()][sel[0]]
                refresh()

        ttk.Button(f2, text=t("fmt_add"), command=add).pack(side=tk.LEFT)
        ttk.Button(f2, text=t("fmt_del"), command=rem).pack(side=tk.LEFT, padx=4)
        tk.Button(dlg, text=t("fmt_done"),
                  command=dlg.destroy).pack(pady=6)

    # ---------------- 字体模板采集 ----------------
    def toggle_capture(self):
        self.capture_mode = not self.capture_mode
        if self.capture_mode:
            self.status(t("status_cap_on"))
        else:
            self.status(t("status_cap_off"))

    def _capture_region(self, x0, y0, x1, y1):
        if self.frame is None:
            return
        x, y = min(x0, x1), min(y0, y1)
        w, h = abs(x1 - x0), abs(y1 - y0)
        ox, oy = int(x / self.disp_scale), int(y / self.disp_scale)
        ow, oh = int(w / self.disp_scale), int(h / self.disp_scale)
        crop = self.frame[oy:oy + oh, ox:ox + ow]
        if crop.size == 0:
            self.status(t("status_cap_fail"))
            return
        self._tr_cap = TraditionalReader()
        bw, rects = self._tr_cap.segment(crop)
        if not rects:
            self.status(t("status_cap_fail"))
            return
        self._cap_cells = [bw[y:y + h, x:x + w] for x, y, w, h in rects]
        self._show_capture_dialog()

    def _show_capture_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title(t("cap_dlg_title"))
        tk.Label(dlg, text=t("cap_dlg_prompt") % len(self._cap_cells)).pack(padx=8, pady=4)
        row = tk.Frame(dlg)
        row.pack()
        self._cap_imgs = []
        for cell in self._cap_cells:
            pil = Image.fromarray(cell).resize(
                (cell.shape[1] * 6, cell.shape[0] * 6), Image.NEAREST)
            imgtk = ImageTk.PhotoImage(pil)
            self._cap_imgs.append(imgtk)
            tk.Label(row, image=imgtk, bg="black").pack(side=tk.LEFT, padx=2)
        self._cap_entry = tk.Entry(dlg, width=40, font=("", 12))
        self._cap_entry.pack(padx=8, pady=6)
        tk.Button(dlg, text=t("cap_dlg_save"),
                  command=lambda: self._save_capture(dlg)).pack(pady=4)

    def _save_capture(self, dlg):
        truth = self._cap_entry.get().strip()
        if len(truth) != len(self._cap_cells):
            messagebox.showwarning("Warning",
                                   t("cap_dlg_mismatch") % (len(self._cap_cells), len(truth)))
            return
        import json
        import time
        custom_dir = os.path.join(base_dir(), "templates_custom")
        os.makedirs(custom_dir, exist_ok=True)
        manifest_path = os.path.join(custom_dir, "manifest.json")
        manifest = {}
        if os.path.exists(manifest_path):
            try:
                manifest = json.load(open(manifest_path, encoding="utf-8"))
            except Exception:
                manifest = {}
        seq = int(time.time() * 10) % 100000
        for i, (ch, cell) in enumerate(zip(truth, self._cap_cells)):
            fn = "cap%d_%d_%d.png" % (seq, i, ord(ch))
            cv2.imwrite(os.path.join(custom_dir, fn), cell)
            manifest.setdefault(ch, []).append(fn)
        json.dump(manifest, open(manifest_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        dlg.destroy()
        self.status(t("status_cap_saved") % len(truth))

    # ---------------- 模板存取 ----------------
    def load(self):
        path = filedialog.askopenfilename(filetypes=[("Template", "*.json"), ("All files", "*.*")])
        if not path:
            return
        t = load_template(path)
        self.rois = t["rois"]
        self.logic_cfg = logic_from_template(t)
        self.formats = t.get("formats") or {}
        self.selected = None
        self.draw_rois()
        self.status(t("status_tpl_loaded") % (len(self.rois), t["source_width"],
                                              t["source_height"]))

    def save(self):
        if not self.rois:
            messagebox.showwarning("Warning", t("msg_no_roi"))
            return
        if self.frame is None:
            return
        h, w = self.frame.shape[:2]
        # 一机一模板：ROI + 逻辑设置 + 标签格式打包进同一个模板文件
        t = {"source_width": w, "source_height": h,
             "logic": self.logic_cfg, "formats": self.formats, "rois": self.rois}
        default_out = os.path.join(base_dir(), TEMPLATE_NAME)
        out = filedialog.asksaveasfilename(
            initialdir=os.path.dirname(default_out),
            initialfile=TEMPLATE_NAME,
            defaultextension=".json",
            filetypes=[("Template", "*.json"), ("All files", "*.*")])
        if not out:
            return
        save_template(out, t)
        # 预览图（带框）
        prev = self.frame.copy()
        for r in self.rois:
            cv2.rectangle(prev, (r["x"], r["y"]), (r["x"] + r["w"], r["y"] + r["h"]),
                          (0, 255, 0), 2)
        cv2.imwrite(os.path.splitext(out)[0] + "_preview.jpg", prev)
        self.status(t("status_saved") % os.path.abspath(out))

    def status(self, text):
        self.status_var.set(text)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
