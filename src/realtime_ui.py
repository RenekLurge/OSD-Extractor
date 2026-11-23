# -*- coding: utf-8 -*-
"""程序 2：实时提取 UI（v8）。

v8 变更：
  - 取消帧号对应（seq 全部移除），数据流回到最简：读帧线程 + 间隔采样识别线程
  - 面板单数值列：有数据 = 加粗黑；从没有过数据 = 灰色 "-"
  - 性能修复（卡顿主因）：
    ① 面板只在数值变化时更新控件（不再每 15ms 全量 config 18 个 label）
    ② 画面叠加元素常驻，只改文本与坐标（不再每帧 delete-all 重建）
  - 暂停/播完：读帧立即停，识别线程处理完当前帧后收尾
"""
import os
import queue
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
from PIL import Image, ImageTk

from ocr_engine import TraditionalReader
from logic_analyzer import analyze, logic_from_template
from roi_template import load_template, scale_rois

OCR_INTERVAL = 0.5       # 识别采样间隔（秒）


class RealtimeApp:
    def __init__(self, root):
        self.root = root
        root.title("程序 2：实时参数提取")
        root.geometry("1500x760")
        root.report_callback_exception = self._on_tk_error

        self.cap = None
        self.fps = 30
        self.template = None
        self.scaled_rois = []
        self.trad = None
        self.logic_cfg = None
        self.capture_thread = None
        self.ocr_thread = None
        self.stop_flag = threading.Event()
        self.capture_done = False
        self.frame_q = queue.Queue(maxsize=2)
        self.msg_q = queue.Queue()
        self.res_lock = threading.Lock()
        self.frame_lock = threading.Lock()
        self.latest_frame = None
        self.results = {}             # name -> {"text", "last_crop"}
        self.loop_play = True
        self.play_state = "idle"
        self.last_ocr_ms = -1
        self.stat_frame = 0
        self._ocr_done = False
        self._shown_text = {}         # 面板已显示的文本缓存（变化才刷新控件）
        self._overlay_scale = None    # 叠加元素按此比例构建过
        self._roi_items = []          # (name, box_id, text_id) 常驻叠加元素
        self._score_seen = set()
        self._last_frame_seen = 0
        self._last_progress_t = time.time()
        self._imgtk = None

        self._build_ui()
        self.log("打开视频并载入模板后点\"开始\"。素材文件名需为英文/数字（cv2 限制）。")

    # ---------------- UI ----------------
    def _build_ui(self):
        left = tk.Frame(self.root)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(left, bg="#101010", width=1040, height=640)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        ctrl = tk.Frame(left)
        ctrl.pack(fill=tk.X, padx=6, pady=3)
        ttk.Button(ctrl, text="打开视频", command=self.open_video).pack(side=tk.LEFT, padx=2)
        ttk.Button(ctrl, text="载入模板", command=self.load_tpl).pack(side=tk.LEFT, padx=2)
        ttk.Button(ctrl, text="开始", command=self.start).pack(side=tk.LEFT, padx=2)
        ttk.Button(ctrl, text="暂停", command=self.pause).pack(side=tk.LEFT, padx=2)
        self.loop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctrl, text="循环播放", variable=self.loop_var,
                        command=self._on_loop_toggle).pack(side=tk.LEFT, padx=6)
        self.box_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctrl, text="显示ROI框", variable=self.box_var).pack(side=tk.LEFT, padx=6)
        tk.Label(ctrl, text="识别间隔(s):").pack(side=tk.LEFT, padx=(10, 2))
        self.interval_var = tk.StringVar(value=str(OCR_INTERVAL))
        ttk.Entry(ctrl, textvariable=self.interval_var, width=5).pack(side=tk.LEFT)
        self.state_lab = tk.Label(ctrl, text="状态：未开始", fg="#666")
        self.state_lab.pack(side=tk.LEFT, padx=10)

        right = tk.Frame(self.root, width=400)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=8, pady=8)
        right.pack_propagate(False)
        tk.Label(right, text="参数面板（黑=有数据，灰=暂无数据）",
                 font=("", 9), fg="#555").pack(anchor=tk.W)
        self.panel = tk.Frame(right)
        self.panel.pack(fill=tk.X, pady=4)
        ttk.Separator(right).pack(fill=tk.X, pady=4)
        tk.Label(right, text="日志", font=("", 11, "bold")).pack(anchor=tk.W)
        self.log_box = tk.Text(right, height=20, state=tk.DISABLED, bg="#f5f5f5")
        self.log_box.pack(fill=tk.BOTH, expand=True, pady=4)

    def _on_loop_toggle(self):
        self.loop_play = self.loop_var.get()

    def build_panel(self):
        for w in self.panel.winfo_children():
            w.destroy()
        if not self.template:
            tk.Label(self.panel, text="未载入模板", fg="#888").pack(anchor=tk.W)
            return
        tk.Label(self.panel, text="%-14s %s" % ("参数", "数值"),
                 font=("Consolas", 10)).pack(anchor=tk.W)
        self.val_labs = {}
        for r in self.scaled_rois:
            row = tk.Frame(self.panel)
            row.pack(fill=tk.X, pady=1)
            disp = r["name"] + (("(%s)" % r["unit"]) if r.get("unit") else "")
            tk.Label(row, text=disp, width=14, anchor=tk.W,
                     font=("", 10, "bold")).pack(side=tk.LEFT)
            val = tk.Label(row, text="-", anchor=tk.W, fg="#888888",
                           font=("Consolas", 12))
            val.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.val_labs[r["name"]] = val
        self._shown_text = {}

    # ---------------- 文件与模板 ----------------
    def open_video(self):
        path = filedialog.askopenfilename(
            filetypes=[("视频", "*.mp4 *.avi *.mov *.mkv *.m4s"), ("所有文件", "*.*")])
        if not path:
            return
        if any(ord(c) > 127 for c in path):
            messagebox.showerror("路径不支持",
                                 "素材路径/文件名不能含中文（cv2 限制）。\n请复制成英文名，如 samples\\demo.mp4")
            return
        self.pause()
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            messagebox.showerror("打开失败", "视频打不开。若为 .m4s 分片，先用 ffmpeg 转 .mp4")
            return
        self.cap = cap
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 30
        self.log("视频已打开：%s（%.1f FPS）" % (os.path.basename(path), self.fps))

    def load_tpl(self):
        path = filedialog.askopenfilename(filetypes=[("模板", "*.json"), ("所有", "*.*")])
        if not path:
            return
        self.template = load_template(path)
        self.logic_cfg = logic_from_template(self.template)   # 逻辑随模板走
        self.formats = self.template.get("formats") or {}     # 标签格式随模板走
        if self.cap is not None:
            w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            self.scaled_rois = scale_rois(self.template, w, h)
        else:
            self.scaled_rois = scale_rois(self.template,
                                          self.template["source_width"] or 1,
                                          self.template["source_height"] or 1)
        with self.res_lock:
            self.results = {}
        self._overlay_scale = None
        self.build_panel()
        self.log("模板已载入：%d 个 ROI（源 %sx%s -> 按当前视频比例缩放）"
                 % (len(self.scaled_rois), self.template["source_width"],
                    self.template["source_height"]))

    # ---------------- 播放控制 ----------------
    def start(self):
        if self.cap is None:
            messagebox.showwarning("缺少视频", "先打开视频")
            return
        if not self.template:
            messagebox.showwarning("缺少模板", "先载入 roi_template.json")
            return
        if (self.capture_thread and self.capture_thread.is_alive()) or \
                (self.ocr_thread and self.ocr_thread.is_alive()):
            self.log("已有播放线程在运行，先点暂停")
            return
        try:
            interval = float(self.interval_var.get())
            if interval <= 0.05:
                interval = 0.05
        except ValueError:
            interval = OCR_INTERVAL
        self.stop_flag.clear()
        self.capture_done = False
        self.frame_q = queue.Queue(maxsize=2)
        self.msg_q = queue.Queue()
        self.latest_frame = None
        self.stat_frame = 0
        self._ocr_done = False
        self._shown_text = {}
        self._score_seen = set()
        self._last_frame_seen = 0
        self._last_progress_t = time.time()
        self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.capture_thread.start()
        self.ocr_thread = threading.Thread(target=self._ocr_loop,
                                           args=(interval,), daemon=True)
        self.ocr_thread.start()
        self.play_state = "playing"
        self.state_lab.config(text="状态：播放中（模板后台初始化中…）", fg="#006600")
        self.root.after(15, self._tick)

    def pause(self):
        self.stop_flag.set()
        if self.play_state == "playing":
            self.play_state = "paused"
            self.state_lab.config(text="状态：已暂停（识别线程收尾中…）", fg="#a05000")

    # ---------------- 工作线程（不碰 tkinter） ----------------
    def _post(self, text):
        try:
            self.msg_q.put_nowait(text)
        except queue.Full:
            pass

    def _capture_loop(self):
        """读帧线程：按视频帧率持续读帧，绝不阻塞。"""
        cap = self.cap
        frame_dt = 1.0 / max(self.fps, 1)
        count = 0
        try:
            while not self.stop_flag.is_set():
                ok, frame = cap.read()
                if not ok:
                    if self.loop_play:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    break
                count += 1
                self.stat_frame = count
                with self.frame_lock:
                    self.latest_frame = frame
                if self.frame_q.full():
                    try:
                        self.frame_q.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    self.frame_q.put_nowait(frame)
                except queue.Full:
                    pass
                if count % 60 == 0:
                    self._post("已处理 %d 帧" % count)
                time.sleep(max(frame_dt - 0.001, 0))
            if not self.stop_flag.is_set():
                self._post(("state", "ended"))
        except Exception:
            self._post("读帧线程异常：\n" + traceback.format_exc())
        finally:
            self.capture_done = True

    def _ocr_loop(self, interval):
        """识别线程：按间隔对最新帧识别；暂停/播完时把当前帧处理完再收尾。"""
        try:
            while self.trad is None:
                self._post("模板初始化中…")
                t0 = time.perf_counter()
                self.trad = TraditionalReader()
                self._post("模板就绪（%.1f 秒，%d 个模板）"
                           % (time.perf_counter() - t0, len(self.trad.templates)))
            first = True
            while not self.stop_flag.is_set():
                with self.frame_lock:
                    frame = self.latest_frame
                if frame is None:
                    if self.capture_done:
                        break
                    time.sleep(0.1)
                    continue
                if first:
                    self._post("第一轮识别开始…")
                t0 = time.perf_counter()
                self._ocr_frame(frame)
                ms = int((time.perf_counter() - t0) * 1000)
                self.last_ocr_ms = ms
                if first:
                    self._post("第一轮识别完成（%d ms）" % ms)
                    first = False
                elif ms > 1500:
                    self._post("识别用时 %d ms（偏慢，可调大识别间隔）" % ms)
                if self.capture_done and not self.stop_flag.is_set():
                    break
                time.sleep(max(interval - (time.perf_counter() - t0), 0.05))
            # 暂停收尾：循环因暂停退出时，最新一帧可能还没被识别过，补一轮
            # （画面没变的框走变化门控近零开销，变了的值在这里被读出来）
            if self.stop_flag.is_set():
                with self.frame_lock:
                    frame = self.latest_frame
                if frame is not None:
                    self._post("暂停收尾：识别最后一帧…")
                    t0 = time.perf_counter()
                    self._ocr_frame(frame)
                    self.last_ocr_ms = int((time.perf_counter() - t0) * 1000)
            self._post("识别收尾完成")
        except Exception:
            self._post("识别线程异常：\n" + traceback.format_exc())

    def _ocr_frame(self, frame):
        """采样帧识别：结果挂进 results；画面没变的框沿用文本。"""
        updates = []
        with self.res_lock:
            for r in self.scaled_rois:
                name = r["name"]
                crop = frame[r["y"]:r["y"] + r["h"], r["x"]:r["x"] + r["w"]]
                if crop.size == 0:
                    continue
                res = self.results.get(name)
                gray_small = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                last_crop = res.get("last_crop") if res else None
                if (last_crop is not None and last_crop.shape == gray_small.shape
                        and float(cv2.absdiff(gray_small, last_crop).mean()) < 2.0):
                    continue
                try:
                    _, score, cells, bw = self.trad.read_detailed(crop)
                    text, _ = analyze(cells, r, self.logic_cfg, bw, self.formats)
                except Exception as e:
                    text, score = "ERR", -1.0
                    self._post("识别异常 %s: %s" % (name, e))
                if name not in self._score_seen:
                    self._score_seen.add(name)
                    self._post("ROI [%s] 首次识别分数 %.2f" % (name, score))
                if res is None:
                    res = {"text": "", "last_crop": None}
                    self.results[name] = res
                if text != res["text"]:
                    updates.append((name, text))
                res["text"] = text
                res["last_crop"] = gray_small
        for name, text in updates:
            self._post("更新：%s = %s" % (name, text))

    # ---------------- 主线程刷新 ----------------
    def _tick(self):
        try:
            while True:
                try:
                    msg = self.msg_q.get_nowait()
                except queue.Empty:
                    break
                if isinstance(msg, tuple) and msg[0] == "state":
                    if msg[1] == "ended" and self.play_state == "playing":
                        self.play_state = "ended"
                        self.state_lab.config(text="状态：播放结束（识别收尾中…）", fg="#333")
                else:
                    if msg == "识别收尾完成":
                        self._ocr_done = True
                        self._refresh_panel()
                        self._refresh_overlay()   # 画面叠加框同步收尾结果
                        self.state_lab.config(text="状态：识别已收尾", fg="#333")
                    self.log(msg)
            try:
                frame = self.frame_q.get_nowait()
            except queue.Empty:
                frame = None
            if frame is not None:
                self._show(frame)
            if self.play_state == "playing":
                self._refresh_panel()
                ocr_ms = ("%d ms" % self.last_ocr_ms) if self.last_ocr_ms >= 0 else "-- ms"
                self.state_lab.config(text="状态：播放中 帧%d  识别 %s" % (self.stat_frame, ocr_ms))
                now = time.time()
                if self.stat_frame > self._last_frame_seen:
                    self._last_frame_seen = self.stat_frame
                    self._last_progress_t = now
                elif now - self._last_progress_t > 8:
                    self.log("⚠ 8 秒无帧进展——程序疑似卡住，请把上面最后几条日志贴给 Claude")
                    self._last_progress_t = now
        except Exception:
            self.log("界面刷新异常（已自愈）：\n" + traceback.format_exc())
        if self.play_state == "playing" or not self._ocr_done:
            self.root.after(15, self._tick)

    def _refresh_panel(self):
        """只刷新变化的数值（黑=有数据，灰=暂无数据）。"""
        with self.res_lock:
            snapshot = {k: v["text"] for k, v in self.results.items()}
        for name, text in snapshot.items():
            lab = self.val_labs.get(name)
            if lab is None or self._shown_text.get(name) == text:
                continue
            self._shown_text[name] = text
            if text:
                lab.config(text=text, fg="#000000", font=("Consolas", 12, "bold"))
            else:
                lab.config(text="-", fg="#888888", font=("Consolas", 12))

    def _show(self, frame):
        h, w = frame.shape[:2]
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 10 or ch < 10:
            cw, ch = 1040, 640
        s = min(cw / w, ch / h)
        dw, dh = int(w * s), int(h * s)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb).resize((dw, dh))
        self._imgtk = ImageTk.PhotoImage(pil)
        self.canvas.delete("img")
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self._imgtk, tags="img")
        # 图像压到最底层，ROI 叠加元素常驻其上（后创建的画布元素会盖住先创建的）
        self.canvas.tag_lower("img")
        if self.box_var.get():
            # 叠加元素常驻：比例变化才重建，平时只改文本
            if self._overlay_scale != (dw, dh):
                self._overlay_scale = (dw, dh)
                self.canvas.delete("roi")
                self._roi_items = []
                for r in self.scaled_rois:
                    x0, y0 = r["x"] * s, r["y"] * s
                    x1, y1 = (r["x"] + r["w"]) * s, (r["y"] + r["h"]) * s
                    box = self.canvas.create_rectangle(x0, y0, x1, y1,
                                                       outline="#00ccff", width=2, tags="roi")
                    txt = self.canvas.create_text(x0, max(y0 - 10, 2), anchor=tk.SW,
                                                  fill="#00ccff", font=("", 9),
                                                  text=r["name"] + ": -", tags="roi")
                    self._roi_items.append((r["name"], box, txt))
            with self.res_lock:
                snapshot = {k: v["text"] for k, v in self.results.items()}
            for name, box, txt in self._roi_items:
                text = snapshot.get(name, "")
                self.canvas.itemconfig(txt, text="%s: %s" % (name, text if text else "-"))

    def _refresh_overlay(self):
        """刷新画面叠加框文字（收尾/暂停时无新帧也调用）。"""
        if not self._roi_items:
            return
        with self.res_lock:
            snapshot = {k: v["text"] for k, v in self.results.items()}
        for name, box, txt in self._roi_items:
            text = snapshot.get(name, "")
            self.canvas.itemconfig(txt, text="%s: %s" % (name, text if text else "-"))

    # ---------------- 日志 ----------------
    def _on_tk_error(self, exc, val, tb):
        self.log("界面异常：%s: %s\n%s" % (exc.__name__, val,
                                           "".join(traceback.format_tb(tb))))

    def log(self, text):
        self.log_box.config(state=tk.NORMAL)
        self.log_box.insert(tk.END, "[%s] %s\n" % (time.strftime("%H:%M:%S"), text))
        self.log_box.see(tk.END)
        self.log_box.config(state=tk.DISABLED)


def main():
    root = tk.Tk()
    RealtimeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
