# -*- coding: utf-8 -*-
"""逻辑分析模块：对识别结果做规则修正。

输入：cells = [(char, score, x, y, w, h), ...]（2x 放大坐标系，x 递增=阅读顺序）
      roi  = {"name": ..., "with_unit_letters": ...}
      config = 逻辑配置（logic_config.json，可由标注工具"逻辑设置"调整）

规则：
  1. 基础过滤：默认只留 数字/小数点/冒号；"单位从画面识别"的框额外留字母
  2. 单位分析：字母一律视为单位，单位必在数字之后（数字前的字母丢弃）
  3. 标签特化：冒号只出现在 config["colon_labels"]；
     时间标签（time_labels）无冒号时从右往左两位分组，数字间 '.' 改判冒号
  4. 小数分析：整段至多一个小数点——间距均匀不补点；补点/移点阈值均可配
"""
import json
import os

import numpy as np

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "logic_config.json")

DEFAULT_CONFIG = {
    "colon_labels": ["录制时间", "飞行时间", "相机参数"],   # 允许冒号的标签
    "time_labels": ["录制时间", "飞行时间"],               # 无冒号自动两位分组
    "text_unit_labels": ["距出发点水平距离", "海拔高度", "起飞高度"],
    "no_decimal_labels": [],        # 屏蔽小数点的标签（不补点、删识别点）
    "decimal_gap_ratio": 0.4,       # 补点最小间隔 = 0.4 x 本框字高
    "decimal_gap_single": 0.55,     # 仅两个数字时的间隔比例
    "decimal_standout_mult": 1.5,   # 突出间隔 = 1.5 x 本框中位间隔
    "decimal_move_mult": 1.3,       # 点移动条件 = 突出间隔 > 1.3 x 现点所在间隔
    "enable_insert": True,          # 允许漏检补点
    "enable_move": True,            # 允许点移动
    "gap_pixel_check": True,        # 数字间隔内无白色像素则不补点/不移点
}


def load_config(path=CONFIG_PATH):
    cfg = dict(DEFAULT_CONFIG)
    try:
        if os.path.exists(path):
            data = json.load(open(path, encoding="utf-8"))
            cfg.update({k: v for k, v in data.items() if k in cfg})
    except Exception:
        pass
    return cfg


def save_config(cfg, path=CONFIG_PATH):
    json.dump(cfg, open(path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def logic_from_template(template):
    """从模板取逻辑配置（模板内的 logic 段覆盖默认值）。一机一模板：
    每个设备的模板自带该设备的逻辑规则，缺省字段回落到默认配置。"""
    cfg = dict(DEFAULT_CONFIG)
    lg = template.get("logic") or {}
    cfg.update({k: v for k, v in lg.items() if k in cfg})
    return cfg


def _match_formats(text, fmts):
    """按格式修正：? = 一位数字；* = 不限位数（1 位以上，按原文分隔符位置分割）；
    其他字符按原样（吃掉原文里对应的非数字字符，不管它被误读成什么）。
    适配不上任何格式时返回 None（交给旧规则）。"""
    digits = "".join(c for c in text if c.isdigit())
    for fmt in fmts:
        total_q = fmt.count("?")
        total_star = fmt.count("*")
        if total_star == 0:
            # 纯 ? 格式：数字个数必须一致
            if total_q != len(digits):
                continue
            out, di = [], 0
            for ch in fmt:
                if ch == "?":
                    out.append(digits[di])
                    di += 1
                else:
                    out.append(ch)
            return "".join(out)
        # 带 * 格式：按原文结构对齐
        if len(digits) < total_q + total_star:
            continue
        out, ti = [], 0
        ok = True
        for idx, ch in enumerate(fmt):
            if ch == "?":
                if ti < len(text) and text[ti].isdigit():
                    out.append(text[ti])
                    ti += 1
                else:
                    ok = False
                    break
            elif ch == "*":
                remain_after = fmt[idx + 1:].count("?") + fmt[idx + 1:].count("*")
                n = 0
                while ti < len(text) and text[ti].isdigit():
                    ti += 1
                    n += 1
                if n < 1:
                    ok = False
                    break
                if remain_after > 0 and n > remain_after:
                    ti -= (n - remain_after)   # 多余的位数留给后面的字段
                    n = remain_after
                out.append(text[ti - n:ti])
            else:
                # 字面量：吃掉原文里一个或多个连续的非数字字符
                while ti < len(text) and not text[ti].isdigit():
                    ti += 1
                out.append(ch)
        if ok and ti == len(text):
            return "".join(out)
    return None


def analyze(cells, roi, config=None, bw=None, formats=None):
    """返回 (修正后文本, 平均分数)。

    bw：该 ROI 的二值图（2x 坐标系，字=255）。用于"间隔内白色像素"校验——
    漏检的小数点必然在数字间隔里留下墨迹，间隔全空则只是普通间距。
    传 None 时退回纯几何规则（兼容旧调用/测试）。
    formats：{标签名: [格式串,...]}，? = 一位数字，其他字符原样
    （如 "??:??" 两位比两位、"?:???" 相机参数 7:100）。定义了格式的标签
    优先按格式修正，匹配不上才走旧规则。"""
    cfg = config or DEFAULT_CONFIG
    name = roi["name"]
    keep_letters = bool(roi.get("with_unit_letters", False))
    if not cells:
        return "", 0.0

    # ---- 1) 字符级过滤（保序，位置保留） ----
    kept = []
    for ch, s, x, y, w, h in cells:
        if ch.isdigit() or ch in ".:":
            kept.append([ch, s, x, y, w, h])
        elif keep_letters and ch.isascii() and ch.isalpha():
            kept.append([ch, s, x, y, w, h])
    if not kept:
        return "", 0.0

    # ---- 2) 单位分析：字母只作单位，且必在数字之后 ----
    if keep_letters:
        first_digit = next((i for i, k in enumerate(kept) if k[0].isdigit()), None)
        if first_digit is not None and first_digit > 0:
            kept = kept[first_digit:]

    # ---- 2.5) 标签格式特化（优先于冒号/小数规则） ----
    fmts = (formats or {}).get(name)
    if fmts:
        text_now = "".join(k[0] for k in kept)
        fixed = _match_formats(text_now, fmts)
        if fixed is not None:
            return fixed, _mean_score(kept)

    # ---- 3) 标签特化：冒号 ----
    if name in cfg["colon_labels"]:
        # 数字之间的 '.' 视为冒号误读（时间与相机参数通用）
        for i in range(1, len(kept) - 1):
            if (kept[i][0] == "." and kept[i - 1][0].isdigit()
                    and kept[i + 1][0].isdigit()):
                kept[i][0] = ":"
        if name in cfg["time_labels"]:
            if not any(k[0] == ":" for k in kept):
                # 无冒号：纯数字从右往左两位分组
                digits = "".join(k[0] for k in kept if k[0].isdigit())
                grouped = []
                while len(digits) > 2:
                    grouped.insert(0, digits[-2:])
                    digits = digits[:-2]
                grouped.insert(0, digits)
                return ":".join(grouped), _mean_score(kept)
            # 多个冒号只留第一个
            first_colon = next(i for i, k in enumerate(kept) if k[0] == ":")
            kept = [k for i, k in enumerate(kept) if k[0] != ":" or i == first_colon]
            return "".join(k[0] for k in kept), _mean_score(kept)
        # 其他冒号标签（如相机参数）：保留冒号，不套时间分组规则
        return "".join(k[0] for k in kept), _mean_score(kept)
    else:
        kept = [k for k in kept if k[0] != ":"]

    # ---- 4) 小数分析：整段至多一个小数点 ----
    # 4.0 屏蔽小数点的标签：删识别点、不补点
    if name in cfg.get("no_decimal_labels", []):
        kept = [k for k in kept if k[0] != "."]
        return "".join(k[0] for k in kept), _mean_score(kept)

    def gap_has_ink(i, j):
        """两个数字单元之间的间隔里是否有白色像素（真漏检的点必有墨迹）。"""
        if bw is None:
            return True   # 无图像信息时退回几何规则
        x0 = kept[i][2] + kept[i][5]
        x1 = kept[j][2]
        y0 = min(kept[i][3], kept[j][3])
        y1 = max(kept[i][3] + kept[i][4], kept[j][3] + kept[j][4])
        if x1 - x0 < 1 or y1 - y0 < 1:
            return False
        return bool(np.any(bw[y0:y1, x0:x1] > 0))

    heights = [k[4] for k in kept if k[4] > 0]
    med_h = float(np.median(heights)) if heights else 20.0
    # 相邻数字对的间隔
    digit_idxs = [i for i, k in enumerate(kept) if k[0].isdigit()]
    pairs = []
    for a in range(len(digit_idxs) - 1):
        i, j = digit_idxs[a], digit_idxs[a + 1]
        gap = kept[j][2] - (kept[i][2] + kept[i][5])
        pairs.append((i, j, gap))
    # "突出间隔"判定：间距均匀时不补点
    # 白像素校验只在数字 <= 2 个时启用（无中位间隔可参照）；
    # 数字 > 2 个时只做字符间距比较（中位间隔本身可靠）
    use_ink = cfg["gap_pixel_check"] and len(digit_idxs) <= 2
    if pairs:
        best_pair = max(pairs, key=lambda p: p[2])
        if len(pairs) >= 2:
            median_gap = float(np.median([g for _, _, g in pairs]))
            min_standout = max(med_h * cfg["decimal_gap_ratio"],
                               median_gap * cfg["decimal_standout_mult"])
        else:
            min_standout = med_h * cfg["decimal_gap_single"]
        if (cfg["enable_insert"] and best_pair[2] >= min_standout
                and (not use_ink or gap_has_ink(*best_pair[:2]))):
            standout = best_pair
        else:
            standout = None
    else:
        standout = None
    # 识别到的有效点（前后都是数字）
    valid_dots = [d for d, k in enumerate(kept)
                  if k[0] == "." and 0 < d < len(kept) - 1
                  and kept[d - 1][0].isdigit() and kept[d + 1][0].isdigit()]
    if valid_dots:
        # 只留夹缝最大的点；若别处有更突出间隔（>move_mult 倍），把点移过去
        best_d = max(valid_dots, key=lambda d:
                     kept[d + 1][2] - (kept[d - 1][2] + kept[d - 1][5]))
        dot_gap = kept[best_d + 1][2] - (kept[best_d - 1][2] + kept[best_d - 1][5])
        move = (cfg["enable_move"] and standout is not None
                and standout[2] > dot_gap * cfg["decimal_move_mult"])
        # 记录移动目标的 x 坐标（列表删除后索引会失效，用坐标定位）
        target_x = kept[standout[1]][2] if move else None
        kept = [k for idx, k in enumerate(kept) if k[0] != "." or idx == best_d]
        if move:
            kept = [k for k in kept if k[0] != "."]
            ins = next(i for i, k in enumerate(kept) if k[2] == target_x)
            kept.insert(ins, [".", 1.0,
                              kept[ins - 1][2] + kept[ins - 1][5], kept[ins - 1][3],
                              1, int(med_h)])
    else:
        # 无有效点：全部丢弃，仅在突出间隔处补一个
        target_x = kept[standout[1]][2] if standout is not None else None
        kept = [k for k in kept if k[0] != "."]
        if target_x is not None:
            ins = next(i for i, k in enumerate(kept) if k[2] == target_x)
            kept.insert(ins, [".", 1.0,
                              kept[ins - 1][2] + kept[ins - 1][5], kept[ins - 1][3],
                              1, int(med_h)])

    return "".join(k[0] for k in kept), _mean_score(kept)


def _mean_score(kept):
    scs = [k[1] for k in kept if k[1] > 0]
    return float(np.mean(scs)) if scs else 0.0
