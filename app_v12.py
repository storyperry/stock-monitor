# -*- coding: utf-8 -*-
"""
桌面股票监控小工具 v12
修复/调整：分时周期改用 ◀/▶ 按钮循环切换 + 点周期标签弹菜单（Combobox 在无边框窗口里下拉不可用）；
     按住标题栏任意位置（含状态栏区域）即可拖动；分时图全局唯一，双击其他股票切换而不新开窗口；
     滚珠中键/滚轮只隐藏鼠标所在窗口（主窗与分时图各自独立隐身）。
数据源：腾讯行情接口（免费，无需Key）
"""
import ctypes
import json
import os
import re
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser
from tkinter import font as tkfont
import urllib.request

import keyboard
import pystray
from PIL import Image, ImageDraw, ImageTk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEFAULT_TRAY = os.path.join(BASE_DIR, "default_tray.png")
MAGIC = "#ABCDEF"  # 背景透明时占用的魔法色（配合 -transparentcolor）
TRANSPARENT_BLEND_BASE = "#1e222a"  # 背景透明时字体混色基准

# 列定义: key, 表头, 宽度
COLS = [("code", "代码", 64), ("name", "名称", 84), ("price", "现价", 62),
        ("pct", "涨跌幅", 66), ("lb", "量比", 52), ("vol", "成交量", 72),
        ("high", "最高", 58), ("low", "最低", 58)]

DEFAULT_CONFIG = {
    "stocks": ["sh600519", "sz000001", "sh510300"],
    "refresh_interval": 3,
    "window_alpha": 0.85,
    "bg_color": "#1e222a",
    "transparent_bg": False,
    "show_bar": True,
    "minimal_mode": False,
    "text_color": "#ffffff",
    "up_color": "#ff4040",
    "down_color": "#00cc66",
    "flat_color": "#c8c8c8",
    "font_alpha": 100,
    "font_size": 11,
    "row_height": 26,
    "columns": {k: True for k, _, _ in COLS},
    "hotkey": "ctrl+alt+h",
    "always_on_top": True,
    "tray_icon": "",
    "window_pos": None,
    "chart_bg_color": "#14171c",
    "chart_transparent": False,
    "chart_show_bar": True,
    "auto_hide": 0,
    "click_open": False,
    "font_family": "",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    # 补全列开关，兼容旧配置
    saved = cfg.get("columns") or {}
    cfg["columns"] = {k: bool(saved.get(k, v)) for k, v in DEFAULT_CONFIG["columns"].items()}
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def normalize_code(text):
    """把用户输入转成腾讯接口代码，如 600519 -> sh600519"""
    text = text.strip().lower().replace(" ", "")
    if re.fullmatch(r"(sh|sz|bj)\d{5,6}", text):
        return text
    if re.fullmatch(r"\d{5,6}", text):
        first = text[0]
        if first in "569":
            return "sh" + text
        if first in "0123":
            return "sz" + text
        if first in "48":
            return "bj" + text
    return None


def fetch_quotes(codes):
    """腾讯实时行情。返回 {code: dict}"""
    if not codes:
        return {}
    url = "https://qt.gtimg.cn/q=" + ",".join(codes)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=6).read().decode("gbk", "ignore")
    out = {}
    for m in re.finditer(r'v_(\w+)="([^"]*)"', raw):
        code, d = m.group(1), m.group(2).split("~")
        if len(d) > 50:
            def num(i):
                try:
                    return float(d[i])
                except (ValueError, IndexError):
                    return None
            out[code] = {
                "name": d[1],
                "price": num(3),
                "prev": num(4),
                "open": num(5),
                "vol": num(36) or num(6),   # 成交量(手)
                "amount": num(37),          # 成交额(万)
                "pct": num(32),             # 涨跌幅%
                "high": num(33),
                "low": num(34),
                "liangbi": num(49),         # 量比
                "time": d[30],
            }
    return out


def fetch_minute(code):
    """腾讯分时数据。返回 (name, prev_close, [(time, price, cumvol), ...])"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = json.loads(urllib.request.urlopen(req, timeout=6).read().decode("utf-8", "ignore"))
    d = data["data"][code]
    qt = d.get("qt", {}).get(code, [])
    name = qt[1] if len(qt) > 1 else code
    try:
        prev = float(qt[4])
    except (ValueError, IndexError, TypeError):
        prev = None
    rows = []
    for line in d.get("data", {}).get("data", []):
        parts = line.split()
        if len(parts) >= 3:
            try:
                rows.append((parts[0], float(parts[1]), float(parts[2])))
            except ValueError:
                continue
    return name, prev, rows


def fetch_kline(code, period):
    """腾讯K线。period: day/week/month/m5/m15/m30/m60
    返回 [(date, open, close, high, low), ...]"""
    if period in ("m5", "m15", "m30", "m60"):
        url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={code},{period},,,320"
    else:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},{period},,,120,qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = json.loads(urllib.request.urlopen(req, timeout=6).read().decode("utf-8", "ignore"))
    d = data["data"][code]
    rows = d.get("qfq" + period) or d.get(period) or []
    out = []
    for r in rows:
        try:
            out.append((str(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
        except (ValueError, IndexError, TypeError):
            continue
    return out


def blend(fg_hex, bg_hex, alpha):
    """模拟字体透明度：把字体颜色向背景色混合"""
    try:
        f = [int(fg_hex[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(bg_hex[i:i + 2], 16) for i in (1, 3, 5)]
        return "#%02x%02x%02x" % tuple(
            max(0, min(255, round(f[i] * alpha + b[i] * (1 - alpha)))) for i in range(3))
    except Exception:
        return fg_hex


def fmt_vol(v):
    """成交量(手)格式化"""
    if v is None:
        return "--"
    if v >= 100000000:
        return "%.2f亿手" % (v / 100000000)
    if v >= 10000:
        return "%.2f万手" % (v / 10000)
    return "%d手" % int(v)


def make_default_tray_icon():
    """生成默认托盘图标：K线风格"""
    img = Image.new("RGBA", (64, 64), (30, 34, 42, 255))
    dr = ImageDraw.Draw(img)
    dr.rectangle([12, 22, 26, 48], fill=(244, 64, 64, 255))
    dr.rectangle([18, 12, 20, 54], fill=(244, 64, 64, 255))
    dr.rectangle([38, 16, 52, 40], fill=(0, 204, 102, 255))
    dr.rectangle([44, 10, 46, 48], fill=(0, 204, 102, 255))
    img.save(DEFAULT_TRAY)


def set_colorkey_alpha(hwnd, key_hex, alpha):
    """Win32：同时启用颜色键透明(LWA_COLORKEY)与整体透明(LWA_ALPHA)。
    tkinter 原生 -transparentcolor 与 -alpha 互斥（alpha<1 时颜色键失效），此函数绕过该限制。"""
    try:
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if not (ex & WS_EX_LAYERED):
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        r, g, b = (int(key_hex[i:i + 2], 16) for i in (1, 3, 5))
        cr_key = (b << 16) | (g << 8) | r  # COLORREF = 0x00BBGGRR
        user32.SetLayeredWindowAttributes(hwnd, cr_key, max(0, min(255, int(alpha * 255))), 1 | 2)
    except Exception:
        pass


class MinuteChart:
    """分时图/K线（双窗口：标题栏窗口跟随窗口透明度，画布窗口跟随字体透明度）
    全局只允许一个分时图窗口，双击其他股票时复用并切换股票。"""

    KLINE_PERIODS = [("day", "日K"), ("week", "周K"), ("month", "月K"),
                     ("m5", "5分"), ("m15", "15分"), ("m30", "30分"), ("m60", "60分")]
    BAR_H = 26

    def __init__(self, master, code, name_hint, cfg, on_cfg_change=None,
                 font_family="微软雅黑"):
        self.code = code
        self.cfg = cfg
        self.on_cfg_change = on_cfg_change
        self.font_family = font_family
        self.mode = "minute"
        self.period_idx = 0
        self.period, self.period_label = self.KLINE_PERIODS[0]
        self.krows = []
        self.name = name_hint or code
        self.rows = []
        self.prev = None
        px = max(0, master.winfo_screenwidth() - 960)
        py = 120
        # ---- 标题栏窗口（chrome，跟随窗口透明度） ----
        self.win_bar = tk.Toplevel(master)
        self.win_bar.overrideredirect(True)
        self.win_bar.geometry("%dx%d+%d+%d" % (440, self.BAR_H, px, py))
        self.bar = tk.Frame(self.win_bar, bg="#2a2f38")
        self.bar.pack(fill=tk.BOTH, expand=True)
        self.bar_lbl = tk.Label(self.bar, text=self.name, bg="#2a2f38", fg="#dddddd",
                                font=(self.font_family, 9))
        self.bar_lbl.pack(side=tk.LEFT, padx=8)
        self.btn_minute = tk.Label(self.bar, text="分时", bg="#4a90d9", fg="#ffffff",
                                   font=(self.font_family, 9), cursor="hand2", padx=6)
        self.btn_kline = tk.Label(self.bar, text="K线", bg="#3a4150", fg="#dddddd",
                                  font=(self.font_family, 9), cursor="hand2", padx=6)
        self.btn_minute.pack(side=tk.LEFT, padx=(8, 2))
        self.btn_kline.pack(side=tk.LEFT, padx=2)
        self.btn_minute.bind("<Button-1>", lambda e: self.set_mode("minute"))
        self.btn_kline.bind("<Button-1>", lambda e: self.set_mode("kline"))
        # 周期：◀ 标签 ▶（Combobox 在无边框窗口里下拉不可用，改用按钮循环切换）
        self.btn_prev = tk.Label(self.bar, text="◀", bg="#3a4150", fg="#dddddd",
                                 font=(self.font_family, 8), cursor="hand2", padx=3)
        self.btn_prev.pack(side=tk.LEFT, padx=(6, 0))
        self.btn_prev.bind("<Button-1>", lambda e: self.cycle_period(-1))
        self.period_lbl = tk.Label(self.bar, text=self.period_label, bg="#3a4150",
                                   fg="#ffffff", font=(self.font_family, 8),
                                   cursor="hand2", padx=4)
        self.period_lbl.pack(side=tk.LEFT)
        self.period_lbl.bind("<Button-1>", self._period_menu_popup)
        self.btn_next = tk.Label(self.bar, text="▶", bg="#3a4150", fg="#dddddd",
                                 font=(self.font_family, 8), cursor="hand2", padx=3)
        self.btn_next.pack(side=tk.LEFT)
        self.btn_next.bind("<Button-1>", lambda e: self.cycle_period(1))
        self.trans_btn = tk.Label(self.bar, text="背景透明:关", bg="#3a4150", fg="#dddddd",
                                  font=(self.font_family, 8), cursor="hand2", padx=5)
        self.trans_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.trans_btn.bind("<Button-1>", lambda e: self.toggle_transparent())
        close_lbl = tk.Label(self.bar, text="✕", bg="#2a2f38", fg="#bbbbbb",
                             font=(self.font_family, 10), cursor="hand2")
        close_lbl.pack(side=tk.RIGHT, padx=6)
        close_lbl.bind("<Button-1>", lambda e: self.destroy())
        # 拖动：标题栏窗口整体（框架/股票名/空白处均可拖）
        for wdg in (self.win_bar, self.bar, self.bar_lbl):
            wdg.bind("<Button-1>", self._drag_start)
            wdg.bind("<B1-Motion>", self._drag_move)
        # ---- 画布窗口（data，跟随字体透明度） ----
        self.win_canvas = tk.Toplevel(master)
        self.win_canvas.overrideredirect(True)
        self.win_canvas.geometry("900x474+%d+%d" % (px, py + self.BAR_H))
        self.canvas = tk.Canvas(self.win_canvas, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        # ---- 事件：滚轮/中键只隐藏自己；右键菜单；Esc 关闭 ----
        targets = (self.win_bar, self.bar, self.bar_lbl, self.trans_btn,
                   self.win_canvas, self.canvas)
        for w in targets:
            w.bind("<Button-2>", lambda e: self.withdraw())
            w.bind("<Double-Button-2>", lambda e: self.withdraw())
            w.bind("<MouseWheel>", lambda e: self.withdraw())
            w.bind("<Button-3>", self._popup_menu)
        self.win_canvas.bind("<Configure>", lambda e: self.draw())
        for w in (self.win_bar, self.win_canvas, self.canvas):
            w.bind("<Escape>", lambda e: self.destroy())
        self.cmenu = tk.Menu(self.win_canvas, tearoff=0, bg="#2a2f38", fg="#dddddd",
                             activebackground="#4a90d9", activeforeground="#ffffff")
        self.cmenu.add_command(label="切换 分时/K线", command=self.toggle_mode)
        self.pmenu = tk.Menu(self.cmenu, tearoff=0, bg="#2a2f38", fg="#dddddd",
                             activebackground="#4a90d9", activeforeground="#ffffff")
        for _, h in self.KLINE_PERIODS:
            self.pmenu.add_command(label=h, command=lambda h=h: self._set_period(h))
        self.cmenu.add_cascade(label="K线周期", menu=self.pmenu)
        self.cmenu.add_command(label="关闭", command=self.destroy)
        self.apply_bg()
        self.win_canvas.after(200, self.refresh)
        self.refresh()

    # ---- 窗口代理方法 ----
    def winfo_exists(self):
        try:
            return bool(self.win_bar.winfo_exists() and self.win_canvas.winfo_exists())
        except Exception:
            return False

    def withdraw(self):
        for w in (self.win_bar, self.win_canvas):
            try:
                w.withdraw()
            except Exception:
                pass

    def deiconify(self):
        for w in (self.win_bar, self.win_canvas):
            try:
                w.deiconify()
            except Exception:
                pass

    def lift(self):
        for w in (self.win_bar, self.win_canvas):
            try:
                w.lift()
            except Exception:
                pass

    def destroy(self):
        for w in (self.win_bar, self.win_canvas):
            try:
                w.destroy()
            except Exception:
                pass

    def _popup_menu(self, e):
        try:
            self.cmenu.tk_popup(e.x_root, e.y_root)
        except Exception:
            pass
        return "break"

    # ---- 布局/拖动 ----
    def _sync_position(self):
        x, y = self.win_bar.winfo_x(), self.win_bar.winfo_y()
        self.win_canvas.geometry("+%d+%d" % (x, y + self.BAR_H))

    def _drag_start(self, e):
        self._dx, self._dy = e.x, e.y

    def _drag_move(self, e):
        x = self.win_bar.winfo_x() + e.x - self._dx
        y = self.win_bar.winfo_y() + e.y - self._dy
        self.win_bar.geometry("+%d+%d" % (x, y))
        self._sync_position()

    # ---- 模式/周期/透明开关 ----
    def toggle_mode(self):
        self.set_mode("kline" if self.mode == "minute" else "minute")

    def set_mode(self, mode):
        self.mode = mode
        self.btn_minute.configure(bg="#4a90d9" if mode == "minute" else "#3a4150",
                                  fg="#ffffff" if mode == "minute" else "#dddddd")
        self.btn_kline.configure(bg="#4a90d9" if mode == "kline" else "#3a4150",
                                 fg="#ffffff" if mode == "kline" else "#dddddd")
        self.refresh()

    def cycle_period(self, d):
        self.period_idx = (self.period_idx + d) % len(self.KLINE_PERIODS)
        self._apply_period()

    def _period_menu_popup(self, e):
        try:
            self.pmenu.tk_popup(e.x_root, e.y_root)
        except Exception:
            pass
        return "break"

    def _set_period(self, label):
        for i, (_, h) in enumerate(self.KLINE_PERIODS):
            if h == label:
                self.period_idx = i
                break
        self._apply_period()

    def _apply_period(self):
        self.period, self.period_label = self.KLINE_PERIODS[self.period_idx]
        self.period_lbl.configure(text=self.period_label)
        if self.mode != "kline":
            self.set_mode("kline")
        else:
            self.refresh()

    def toggle_transparent(self):
        self.cfg["chart_transparent"] = not self.cfg.get("chart_transparent", False)
        save_config(self.cfg)
        self._update_trans_btn()
        self.apply_bg()
        if self.on_cfg_change:
            self.on_cfg_change()

    def _update_trans_btn(self):
        on = bool(self.cfg.get("chart_transparent", False))
        self.trans_btn.configure(text="背景透明:%s" % ("开" if on else "关"),
                                 bg="#4a90d9" if on else "#3a4150",
                                 fg="#ffffff" if on else "#dddddd")

    def apply_bg(self):
        bg = MAGIC if self.cfg.get("chart_transparent") else \
            self.cfg.get("chart_bg_color", "#14171c")
        self.win_canvas.configure(bg=bg)
        self.canvas.configure(bg=bg)

    # ---- 切换股票（复用同一窗口） ----
    def set_stock(self, code, name_hint):
        self.code = code
        self.name = name_hint or code
        self.bar_lbl.configure(text=self.name)
        self.period_idx = 0
        self.period, self.period_label = self.KLINE_PERIODS[0]
        self.period_lbl.configure(text=self.period_label)
        self.set_mode("minute")

    # ---- 数据 ----
    def refresh(self):
        def worker():
            try:
                if self.mode == "minute":
                    payload = ("minute", fetch_minute(self.code))
                else:
                    payload = ("kline", fetch_kline(self.code, self.period))
            except Exception:
                payload = (self.mode, None)
            try:
                self.win_canvas.after(0, lambda: self._apply(payload))
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()
        try:
            self.win_canvas.after(10000 if self.mode == "minute" else 60000, self.refresh)
        except Exception:
            pass

    def _apply(self, payload):
        kind, data = payload
        if kind == "minute":
            self.name, self.prev, self.rows = data
            self.bar_lbl.configure(text=self.name)
        else:
            self.krows = data or []
        self.draw()

    # ---- 绘制 ----
    def draw(self):
        if self.mode == "minute":
            self._draw_minute()
        else:
            self._draw_kline()

    def _grid(self, lo, hi, prev, unit=3):
        c = self.canvas
        w = c.winfo_width()
        h = c.winfo_height()
        left, right, top, bottom = 46, 66, 30, 30
        pw, ph = w - left - right, h - top - bottom

        def X(x):
            return left + x * pw

        def Y(p):
            return top + (hi - p) * ph / (hi - lo) if hi > lo else top

        for i in range(5):
            y = top + ph * i / 4
            c.create_line(left, y, left + pw, y, fill="#4a505c")
            pv = hi - (hi - lo) * i / 4
            color = "#ff4040" if pv > (prev or 0) else ("#00cc66" if pv < (prev or 0) else "#c8c8c8")
            c.create_text(left + pw + 8, y, text="%.*f" % (unit, pv), fill=color,
                          anchor="w", font=(self.font_family, 9))
        return X, Y, left, right, top, bottom, pw, ph

    def _title(self, price, prev):
        c = self.canvas
        pct = "  %+.2f%%" % ((price / prev - 1) * 100) if prev else ""
        color = "#ff4040" if price >= (prev or 0) else "#00cc66"
        c.create_text(56, 14, anchor="w", text="%s  %.*f%s" % (
            self.name, 3, price, pct),
            fill=color, font=(self.font_family, 12, "bold"))

    def _draw_minute(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 50 or h < 50:
            return
        rows = self.rows
        prev = self.prev
        if not rows:
            c.create_text(w // 2, h // 2, text="暂无分时数据（可能停牌）",
                          fill="#c8c8c8", font=(self.font_family, 12))
            return
        prices = [r[1] for r in rows]
        avgs, cum_pv, cum_v, last_v = [], 0.0, 0.0, 0.0
        for _, p, v in rows:
            dv = max(0.0, v - last_v)
            cum_pv += p * dv
            cum_v += dv
            last_v = v
            avgs.append(cum_pv / cum_v if cum_v > 0 else p)
        lo = min(min(prices), min(avgs), prev if prev else min(prices))
        hi = max(max(prices), max(avgs), prev if prev else max(prices))
        if hi == lo:
            hi, lo = hi * 1.001, lo * 0.999
        pad = (hi - lo) * 0.06
        hi, lo = hi + pad, lo - pad
        X, Y, left, right, top, bottom, pw, ph = self._grid(lo, hi, prev)
        for idx, txt in [(0, "09:30"), (60, "10:30"), (120, "11:30/13:00"),
                         (180, "14:00"), (240, "15:00")]:
            x = X(idx / 240)
            c.create_line(x, top, x, top + ph, fill="#4a505c")
            c.create_text(x, top + ph + 12, text=txt, fill="#8a8f99",
                          font=(self.font_family, 9))
        if prev:
            y = Y(prev)
            c.create_line(left, y, left + pw, y, fill="#888888", dash=(4, 3))
        color = "#ff4040" if prices[-1] >= (prev or 0) else "#00cc66"
        pts = []
        for i, p in enumerate(prices):
            pts.extend([X(i / 240), Y(p)])
        if len(pts) >= 4:
            c.create_line(pts, fill=color, width=1.4)
        apts = []
        for i, a in enumerate(avgs):
            apts.extend([X(i / 240), Y(a)])
        if len(apts) >= 4:
            c.create_line(apts, fill="#ffcc00", width=1)
        self._title(prices[-1], prev)
        c.create_text(left + 260, 14, anchor="w", text="— 分时  — 均价",
                      fill="#ffcc00", font=(self.font_family, 9))

    def _draw_kline(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 50 or h < 50:
            return
        rows = self.krows
        if not rows:
            c.create_text(w // 2, h // 2, text="暂无K线数据",
                          fill="#c8c8c8", font=(self.font_family, 12))
            return
        n = len(rows)
        lo = min(r[4] for r in rows)
        hi = max(r[3] for r in rows)
        if hi == lo:
            hi, lo = hi * 1.001, lo * 0.999
        pad = (hi - lo) * 0.06
        hi, lo = hi + pad, lo - pad
        X, Y, left, right, top, bottom, pw, ph = self._grid(lo, hi, rows[0][1])
        cw = max(1.5, pw / n * 0.7)

        def Xc(i):
            return left + (i + 0.5) * pw / n
        step = max(1, n // 7)
        for i in range(0, n, step):
            x = Xc(i)
            c.create_line(x, top, x, top + ph, fill="#333a44")
            txt = str(rows[i][0])
            c.create_text(x, top + ph + 12, text=txt[-5:] if len(txt) > 5 else txt,
                          fill="#8a8f99", font=(self.font_family, 8))
        last = rows[-1]
        for i, (dt, o, cl, hh, ll) in enumerate(rows):
            color = "#ff4040" if cl >= o else "#00cc66"
            x = Xc(i)
            c.create_line(x, Y(hh), x, Y(ll), fill=color)
            y1, y2 = Y(max(o, cl)), Y(min(o, cl))
            if y2 - y1 < 1:
                y2 = y1 + 1
            c.create_rectangle(x - cw / 2, y1, x + cw / 2, y2,
                               fill=color, outline=color)
        self._title(last[2], rows[0][2])
        c.create_text(left + 300, 14, anchor="w",
                      text="[%s] 红涨绿跌" % self.period_label,
                      fill="#8a8f99", font=(self.font_family, 9))


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.q = queue.Queue()
        self.hotkey_handle = None
        self.tray = None
        self.quote_cache = {}
        self.chart = None  # 全局唯一分时图窗口，双击其他股票时复用

        if not os.path.exists(DEFAULT_TRAY):
            try:
                make_default_tray_icon()
            except Exception:
                pass

        self.root.title("股票监控")
        self.root.withdraw()  # root 仅作隐藏容器；真正显示的是 标题栏窗口 + 数据窗口
        self._hidden = False
        self._resolve_font()

        pos = self.cfg.get("window_pos")
        if pos:
            chrome_x, chrome_y = pos[0], pos[1]
        else:
            sw = self.root.winfo_screenwidth()
            chrome_x, chrome_y = sw - 580, 80
        self.win_chrome = tk.Toplevel(self.root)  # 标题栏窗口：跟随窗口透明度
        self.win_data = tk.Toplevel(self.root)    # 数据窗口：跟随字体透明度
        for w in (self.win_chrome, self.win_data):
            w.overrideredirect(True)
        self.win_chrome.geometry("+%d+%d" % (chrome_x, chrome_y))
        self.win_data.geometry("+%d+%d" % (chrome_x, chrome_y + 30))

        self._build_ui()
        self._apply_style()
        self._rebuild_columns()
        try:
            self._bind_hotkey(self.cfg["hotkey"])
        except Exception:
            self._bind_hotkey("ctrl+alt+h")
        self._setup_tray()

        # 滚珠中键/滚轮 → 只隐藏鼠标所在的窗口（主窗或分时图各自独立）
        # 右键任意位置弹菜单；鼠标离开自动隐藏（上班族模式）
        for w in (self.win_chrome, self.win_data):
            w.bind("<Button-2>", lambda e: self.hide_main())
            w.bind("<Double-Button-2>", lambda e: self.hide_main())
            w.bind("<MouseWheel>", lambda e: self.hide_main())
            w.bind("<Button-3>", self._popup_menu)
            w.bind("<Leave>", self._schedule_auto_hide)
            w.bind("<Enter>", self._cancel_auto_hide)

        self.root.after(300, self._poll_queue)
        self.fetch_loop()

    def _resolve_font(self):
        """解析数据字体：用户指定优先；否则自动选纤细清晰的字体"""
        f = (self.cfg.get("font_family") or "").strip()
        if f:
            self.font_family = f
            return
        try:
            fams = set(tkfont.families(self.root))
            for cand in ("等线 Light", "Microsoft YaHei UI Light", "等线", "微软雅黑"):
                if cand in fams:
                    self.font_family = cand
                    return
        except Exception:
            pass
        self.font_family = "微软雅黑"

    def _blend_base(self):
        """字体混色的背景基准色"""
        return TRANSPARENT_BLEND_BASE if self.cfg.get("transparent_bg") else self.cfg["bg_color"]

    def _win_hwnd(self, win):
        try:
            u = ctypes.windll.user32
            wid = win.winfo_id()
            # GA_ROOT：无边框(overrideredirect)窗口没有 wrapper，GetParent 会取错
            hwnd = u.GetAncestor(wid, 2)  # 2 = GA_ROOT
            return hwnd if hwnd else wid
        except Exception:
            return 0

    def _apply_alpha(self, win, alpha, key=None):
        """设置窗口透明度；key（#hex）给定时叠加颜色键透明（背景透明）"""
        try:
            win.attributes("-alpha", alpha)
        except Exception:
            return
        if key:
            hwnd = self._win_hwnd(win)
            if hwnd:
                set_colorkey_alpha(hwnd, key, alpha)

    def _refresh_transparency(self):
        """窗口透明度→标题栏窗口；字体透明度→数据窗口/分时图画布。
        背景透明(colorkey)时对应窗口的背景被抠掉，只留文字/线条。"""
        c = self.cfg
        wa = float(c.get("window_alpha", 1.0))
        fa = c.get("font_alpha", 100) / 100
        self._apply_alpha(self.win_chrome, wa)
        data_key = MAGIC if c.get("transparent_bg") else None
        self._apply_alpha(self.win_data, fa, data_key)
        chart_key = MAGIC if c.get("chart_transparent") else None
        chart = self.chart
        try:
            if chart is not None and chart.winfo_exists():
                self._apply_alpha(chart.win_bar, wa)
                self._apply_alpha(chart.win_canvas, fa, chart_key)
                chart.apply_bg()
        except Exception:
            pass

    def _effective_bg(self):
        """当前实际背景色（透明模式用魔法色）"""
        return MAGIC if self.cfg.get("transparent_bg") else self.cfg["bg_color"]

    # ---------- UI ----------
    def _build_ui(self):
        c = self.cfg
        base = self._blend_base()
        ff = self.font_family
        # ---- 标题栏窗口（跟随窗口透明度） ----
        self.bar = tk.Frame(self.win_chrome, bg=blend(c["text_color"], base, 0.15))
        self.bar.pack(fill=tk.BOTH, expand=True)
        bar_bg = self.bar["bg"]
        self.title_lbl = tk.Label(self.bar, text="📈 股票监控", bg=bar_bg,
                                  fg=blend(c["text_color"], base, 0.9),
                                  font=(ff, 10, "bold"))
        self.title_lbl.pack(side=tk.LEFT, padx=8, pady=3)
        self.status = tk.Label(self.bar, text="", anchor="w", bg=bar_bg,
                               fg=blend(c["text_color"], base, 0.55),
                               font=(ff, 8))
        self.status.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # 状态栏区域也要能拖动（按住标题栏任意位置拖动窗口）
        self.status.bind("<Button-1>", self._drag_start)
        self.status.bind("<B1-Motion>", self._drag_move)
        btn_ks = dict(bg=bar_bg, fg=blend(c["text_color"], base, 0.75),
                      font=(ff, 10), bd=0, activebackground=bar_bg,
                      activeforeground=c["text_color"], cursor="hand2")
        tk.Label(self.bar, text="⚙", **btn_ks).pack(side=tk.RIGHT, padx=6)
        b_hide = tk.Label(self.bar, text="—", **btn_ks)
        b_hide.pack(side=tk.RIGHT, padx=6)
        b_hide.bind("<Button-1>", lambda e: self.hide_to_tray())
        self.win_chrome.nametowidget(str(self.bar.winfo_children()[-2])).bind(
            "<Button-1>", lambda e: self.open_settings())
        for wdg in (self.bar, self.title_lbl):
            wdg.bind("<Button-1>", self._drag_start)
            wdg.bind("<B1-Motion>", self._drag_move)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # ---- 数据窗口（跟随字体透明度） ----
        cols = [k for k, _, _ in COLS]
        heads = [h for _, h, _ in COLS]
        self.tree = ttk.Treeview(self.win_data, columns=cols, show="headings",
                                 style="Stock.Treeview", selectmode="browse")
        for (key, head, wd), col in zip(COLS, cols):
            self.tree.heading(col, text=head)
            self.tree.column(col, width=wd, anchor="center", stretch=False)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<Double-Button-1>", self._on_double_click)
        # 滚珠中键直接绑在表格控件上（Treeview 类绑定会拦截根窗口的中键事件）
        for seq in ("<Button-2>", "<Double-Button-2>"):
            self.tree.bind(seq, lambda e: self.hide_to_tray())
        # 左键单击：强制选中该行（可选直接打开分时图）；右键：弹菜单
        self.tree.bind("<Button-1>", self._on_left_click)
        self.tree.bind("<Button-3>", self._popup_menu)

        self.menu = tk.Menu(self.win_data, tearoff=0, bg=c["bg_color"], fg=c["text_color"],
                            activebackground="#3a4150", activeforeground="#ffffff")
        self.menu.add_command(label="添加股票", command=self.add_stock_dialog)
        self.menu.add_command(label="删除选中", command=self.remove_selected)
        self.menu.add_command(label="查看分时图", command=self.show_chart_selected)
        self.menu.add_command(label="关闭分时图", command=self.close_all_charts)
        # 列显示子菜单
        self.col_vars = {}
        col_menu = tk.Menu(self.menu, tearoff=0, bg=c["bg_color"], fg=c["text_color"],
                           activebackground="#3a4150", activeforeground="#ffffff")
        for key, head, _ in COLS:
            v = tk.BooleanVar(value=self.cfg["columns"].get(key, True))
            self.col_vars[key] = v
            col_menu.add_checkbutton(label=head, variable=v,
                                     command=lambda k=key: self.toggle_column(k))
        self.menu.add_cascade(label="显示列", menu=col_menu)
        self.menu.add_separator()
        self.menu.add_command(label="设置...", command=self.open_settings)
        self.bar_var = tk.BooleanVar(value=bool(c.get("show_bar", True)))
        self.menu.add_checkbutton(label="显示标题栏", variable=self.bar_var,
                                  command=self.toggle_bar)
        self.minimal_var = tk.BooleanVar(value=bool(c.get("minimal_mode", False)))
        self.menu.add_checkbutton(label="极简模式（只显示数据）", variable=self.minimal_var,
                                  command=self.toggle_minimal)
        self.ah_var = tk.BooleanVar(value=bool(int(c.get("auto_hide", 0) or 0)))
        self.menu.add_checkbutton(label="鼠标离开自动隐藏(10秒)", variable=self.ah_var,
                                  command=self.toggle_auto_hide)
        self.co_var = tk.BooleanVar(value=bool(c.get("click_open", False)))
        self.menu.add_checkbutton(label="左键单击直接打开分时图", variable=self.co_var,
                                  command=self.toggle_click_open)
        self.menu.add_command(label="隐藏到托盘", command=self.hide_to_tray)
        self.menu.add_command(label="退出", command=self.quit_app)
        self._refresh_tree_rows(initial=True)

    def toggle_column(self, key):
        self.cfg["columns"][key] = bool(self.col_vars[key].get())
        save_config(self.cfg)
        self._rebuild_columns()

    def _rebuild_columns(self):
        vis = [k for k, _, _ in COLS if self.cfg["columns"].get(k, True)]
        self.tree["displaycolumns"] = vis
        self._apply_style()

    def _apply_tag_colors(self):
        """根据字体透明度刷新行文字颜色（供滑杆实时预览）"""
        c = self.cfg
        base = self._blend_base()
        alpha = c.get("font_alpha", 100) / 100
        for tag, color in (("up", c["up_color"]), ("down", c["down_color"]),
                           ("flat", c["flat_color"])):
            self.tree.tag_configure(tag, foreground=blend(color, base, alpha))

    def toggle_bar(self):
        self.cfg["show_bar"] = bool(self.bar_var.get())
        save_config(self.cfg)
        self._apply_style()

    def toggle_minimal(self):
        self.cfg["minimal_mode"] = bool(self.minimal_var.get())
        save_config(self.cfg)
        self._apply_style()

    def toggle_auto_hide(self):
        self.cfg["auto_hide"] = 10 if self.ah_var.get() else 0
        save_config(self.cfg)

    def toggle_click_open(self):
        self.cfg["click_open"] = bool(self.co_var.get())
        save_config(self.cfg)

    # ---------- 交互辅助 ----------
    def _popup_menu(self, e):
        """窗口内任意位置右键弹菜单，无需先选中股票"""
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        except Exception:
            pass
        return "break"

    def _on_left_click(self, e):
        """左键单击强制选中该行；可选直接打开分时图"""
        item = self.tree.identify_row(e.y)
        if item:
            self.tree.selection_set(item)
            self.tree.focus(item)
            if self.cfg.get("click_open"):
                self.show_chart(item)

    # ---------- 鼠标离开自动隐藏 ----------
    def _schedule_auto_hide(self, e=None):
        secs = int(self.cfg.get("auto_hide", 0) or 0)
        if secs <= 0:
            return
        self._cancel_auto_hide()
        self._ah_id = self.root.after(secs * 1000, self._auto_hide_check)

    def _cancel_auto_hide(self, e=None):
        try:
            if getattr(self, "_ah_id", None):
                self.root.after_cancel(self._ah_id)
        except Exception:
            pass
        self._ah_id = None

    def _auto_hide_check(self):
        try:
            under = self.root.winfo_containing(self.root.winfo_pointerx(),
                                               self.root.winfo_pointery())
            # 指针不在本程序任何窗口上（含分时图）才隐藏
            if under is None:
                self.hide_to_tray()
        except Exception:
            pass

    def close_all_charts(self):
        if self.chart is not None:
            try:
                self.chart.destroy()
            except Exception:
                pass
        self.chart = None

    def _apply_style(self):
        c = self.cfg
        base = self._blend_base()
        bg = self._effective_bg()
        fa = c.get("font_alpha", 100) / 100
        ff = self.font_family
        style = ttk.Style(self.root)
        # 去掉 Treeview 自绘边框（极简模式四周黑框的来源之一）
        try:
            style.layout("Stock.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        except Exception:
            pass
        # 数据窗口：字体透明度作用于全部文字（数据行、表头），淡入背景
        style.configure("Stock.Treeview", background=bg,
                        fieldbackground=bg,
                        foreground=blend(c["text_color"], base, fa),
                        rowheight=int(c["row_height"]), borderwidth=0,
                        font=(ff, int(c["font_size"])))
        style.configure("Stock.Treeview.Heading", background=blend(c["text_color"], base, 0.18),
                        foreground=blend(c["text_color"], base, 0.9 * fa),
                        font=(ff, int(c["font_size"]), "bold"), borderwidth=0)
        style.map("Stock.Treeview",
                  background=[("selected", bg)],
                  foreground=[("selected", blend(c["text_color"], base, fa))])
        style.map("Stock.Treeview.Heading", background=[("active", blend(c["text_color"], base, 0.25))])
        try:
            self.win_data.attributes("-transparentcolor", MAGIC)
        except Exception:
            pass
        self.win_data.configure(bg=bg)
        self._apply_tag_colors()
        # 标题栏窗口（跟随窗口透明度，不随字体透明度）
        chrome_bg = blend(c["text_color"], base, 0.15)
        self.bar.configure(bg=chrome_bg)
        self.title_lbl.configure(bg=chrome_bg, fg=blend(c["text_color"], base, 0.9),
                                 font=(ff, 10, "bold"))
        self.status.configure(bg=chrome_bg, fg=blend(c["text_color"], base, 0.55),
                              font=(ff, 8))
        # 极简模式只显示数据
        minimal = c.get("minimal_mode", False)
        self.tree.configure(show="" if minimal else "headings")
        # 透明度分配：chrome=窗口透明度，data=字体透明度
        self._refresh_transparency()
        self._layout_windows()

    def _layout_windows(self):
        """摆放 标题栏窗口(30px) 与 数据窗口 的尺寸和相对位置"""
        c = self.cfg
        minimal = c.get("minimal_mode", False)
        self.tree.configure(show="" if minimal else "headings")
        show_chrome = c.get("show_bar", True) and not minimal
        x, y = self.win_chrome.winfo_x(), self.win_chrome.winfo_y()
        self.root.update_idletasks()
        h = self.tree.winfo_reqheight()
        if minimal:
            w = 2 + sum(wd for k, _, wd in COLS if c["columns"].get(k, True))
        else:
            w = 16 + sum(wd for k, _, wd in COLS if c["columns"].get(k, True))
        self.win_data.geometry("%dx%d+%d+%d" % (w, h, x, y + (30 if show_chrome else 0)))
        if show_chrome and not self._hidden:
            self.win_chrome.geometry("%dx30+%d+%d" % (w, x, y))
            if not self.win_chrome.winfo_ismapped():
                self.win_chrome.deiconify()
        else:
            self.win_chrome.withdraw()

    def _refresh_tree_rows(self, initial=False):
        self.tree.delete(*self.tree.get_children())
        for code in self.cfg["stocks"]:
            self.tree.insert("", tk.END, iid=code,
                             values=(code[:2].upper() + " " + code[2:],
                                     "--", "--", "--", "--", "--", "--", "--"),
                             tags=("flat",))
        # 行数按自选股数量自适应（Treeview 默认显示10行，会导致窗口下部大片空白）
        self.tree.configure(height=max(1, len(self.cfg["stocks"])))
        if not initial:
            self._apply_style()

    # ---------- 拖动 ----------
    def _drag_start(self, e):
        self._dx, self._dy = e.x, e.y

    def _drag_move(self, e):
        x = self.win_chrome.winfo_x() + e.x - self._dx
        y = self.win_chrome.winfo_y() + e.y - self._dy
        # 标题栏窗口和数据窗口一起移动
        self.win_chrome.geometry("+%d+%d" % (x, y))
        show_chrome = self.cfg.get("show_bar", True) and not self.cfg.get("minimal_mode", False)
        self.win_data.geometry("+%d+%d" % (x, y + (30 if show_chrome else 0)))
        self.cfg["window_pos"] = [x, y]

    # ---------- 数据 ----------
    def fetch_loop(self):
        def worker():
            while True:
                try:
                    data = fetch_quotes(self.cfg["stocks"])
                    self.q.put(("quotes", data))
                except Exception:
                    self.q.put(("quotes", {}))
                time.sleep(max(1, int(self.cfg.get("refresh_interval", 3))))
        threading.Thread(target=worker, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "quotes":
                    self._update_rows(data)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

    def _update_rows(self, data):
        self.quote_cache.update(data)
        for code in self.cfg["stocks"]:
            q = data.get(code)
            if not q:
                continue
            pct = q["pct"]
            if pct is None:
                tag = "flat"
            elif pct > 0:
                tag = "up"
            elif pct < 0:
                tag = "down"
            else:
                tag = "flat"
            vals = {
                "code": code[:2].upper() + " " + code[2:],
                "name": q["name"] or "--",
                "price": "%.2f" % q["price"] if q["price"] is not None else "--",
                "pct": "%+.2f%%" % pct if pct is not None else "--",
                "lb": "%.2f" % q["liangbi"] if q["liangbi"] is not None else "--",
                "vol": fmt_vol(q["vol"]),
                "high": "%.2f" % q["high"] if q["high"] is not None else "--",
                "low": "%.2f" % q["low"] if q["low"] is not None else "--",
            }
            self.tree.item(code, values=[vals[k] for k, _, _ in COLS], tags=(tag,))
        self.status.config(text="更新于 %s   |   双击行看分时图 · 右键菜单 · 中键/快捷键隐藏" %
                           (time.strftime("%H:%M:%S"),))

    # ---------- 交互 ----------
    def _on_double_click(self, e):
        item = self.tree.identify_row(e.y)
        if item:
            self.show_chart(item)

    def show_chart_selected(self):
        sel = self.tree.selection()
        if sel:
            self.show_chart(sel[0])

    def show_chart(self, code):
        """全局只保留一个分时图窗口：已存在则切换股票，否则新建"""
        name = ""
        if code in self.quote_cache:
            name = self.quote_cache[code].get("name") or ""
        wa = float(self.cfg.get("window_alpha", 1.0))
        fa = self.cfg.get("font_alpha", 100) / 100
        key = MAGIC if self.cfg.get("chart_transparent") else None
        if self.chart is not None and self.chart.winfo_exists():
            self.chart.set_stock(code, name)
            self.chart.deiconify()
            self.chart.lift()
            self._apply_alpha(self.chart.win_bar, wa)
            self._apply_alpha(self.chart.win_canvas, fa, key)
            return
        self.chart = MinuteChart(self.root, code, name, self.cfg,
                                 on_cfg_change=self._refresh_transparency,
                                 font_family=self.font_family)
        # 透明度分配与主窗一致：标题栏=窗口透明度，画布=字体透明度
        self._apply_alpha(self.chart.win_bar, wa)
        self._apply_alpha(self.chart.win_canvas, fa, key)

    def add_stock_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("添加股票")
        dlg.configure(bg=self.cfg["bg_color"])
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        tk.Label(dlg, text="输入代码（如 600519 / 000001 / 510300 / sh000001指数）:",
                 bg=self.cfg["bg_color"], fg=self.cfg["text_color"],
                 font=("微软雅黑", 10)).pack(padx=14, pady=(12, 4))
        ent = tk.Entry(dlg, width=24, font=("Consolas", 11),
                       bg="#2a2f38", fg="#ffffff", insertbackground="#ffffff")
        ent.pack(padx=14, pady=4)
        ent.focus_force()

        def ok(event=None):
            code = normalize_code(ent.get())
            if not code:
                messagebox.showwarning("提示", "代码格式不正确", parent=dlg)
                return
            if code in self.cfg["stocks"]:
                messagebox.showinfo("提示", "该股票已存在", parent=dlg)
                return
            self.cfg["stocks"].append(code)
            save_config(self.cfg)
            self._refresh_tree_rows()
            dlg.destroy()
        ent.bind("<Return>", ok)
        tk.Button(dlg, text="添加", command=ok, bg="#4a90d9", fg="#ffffff",
                  font=("微软雅黑", 10), relief="flat", cursor="hand2").pack(pady=(4, 12))

    def remove_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        code = sel[0]
        if messagebox.askyesno("删除", "确定删除 %s ?" % code):
            self.cfg["stocks"].remove(code)
            save_config(self.cfg)
            self._refresh_tree_rows()

    # ---------- 快捷键 / 托盘 ----------
    def _bind_hotkey(self, hk):
        """先注册并验证新快捷键，成功后才卸载旧的；失败抛 ValueError 且旧键保留"""
        hk = hk.strip().lower()
        if not hk:
            raise ValueError("快捷键不能为空")
        try:
            new_handle = keyboard.add_hotkey(hk, lambda: self.root.after(0, self.toggle_show))
        except Exception:
            raise ValueError("快捷键无效或无法注册: %s" % hk)
        try:
            if self.hotkey_handle is not None:
                keyboard.remove_hotkey(self.hotkey_handle)
        except Exception:
            pass
        self.hotkey_handle = new_handle
        self.cfg["hotkey"] = hk

    def toggle_show(self):
        if self.win_data.state() == "withdrawn":
            self.show_all()
        else:
            self.hide_to_tray()

    def show_all(self):
        self._hidden = False
        self._apply_topmost()
        show_chrome = self.cfg.get("show_bar", True) and not self.cfg.get("minimal_mode", False)
        if show_chrome:
            self.win_chrome.deiconify()
        self.win_data.deiconify()
        self._layout_windows()
        if self.chart is not None:
            try:
                if self.chart.winfo_exists():
                    self.chart.deiconify()
            except Exception:
                pass

    def hide_to_tray(self):
        """全部隐藏（主窗+分时图）"""
        self._hidden = True
        self.win_chrome.withdraw()
        self.win_data.withdraw()
        if self.chart is not None:
            try:
                if self.chart.winfo_exists():
                    self.chart.withdraw()
            except Exception:
                pass

    def hide_main(self):
        """只隐藏主窗（滚珠/滚轮作用于主窗时），分时图不受影响"""
        self._hidden = True
        self.win_chrome.withdraw()
        self.win_data.withdraw()

    def _setup_tray(self):
        path = self.cfg.get("tray_icon") or DEFAULT_TRAY
        try:
            img = Image.open(path)
        except Exception:
            img = Image.new("RGBA", (64, 64), (60, 60, 60, 255))
        menu = pystray.Menu(
            pystray.MenuItem("显示 / 隐藏", lambda: self.root.after(0, self.toggle_show), default=True),
            pystray.MenuItem("设置...", lambda: self.root.after(0, self.open_settings)),
            pystray.MenuItem("退出", lambda: self.root.after(0, self.quit_app)),
        )
        self.tray = pystray.Icon("stock_monitor", img, "股票监控", menu)
        self.tray.run_detached()

    def _reload_tray_icon(self):
        if not self.tray:
            return
        path = self.cfg.get("tray_icon") or DEFAULT_TRAY
        try:
            self.tray.icon = Image.open(path)
        except Exception:
            pass

    def _apply_topmost(self):
        t = bool(self.cfg.get("always_on_top", True))
        for w in (getattr(self, "win_chrome", None), getattr(self, "win_data", None)):
            if w is not None:
                try:
                    w.attributes("-topmost", t)
                except Exception:
                    pass

    def _preview_font_alpha(self, v):
        self.cfg["font_alpha"] = int(v)
        self._apply_tag_colors()
        self._refresh_transparency()

    # ---------- 设置 ----------
    def open_settings(self):
        if getattr(self, "_set_win", None) is not None:
            try:
                if self._set_win.winfo_exists():
                    self._set_win.lift()
                    return
            except Exception:
                pass
        c = self.cfg
        win = tk.Toplevel(self.root)
        self._set_win = win
        win.title("设置")
        win.configure(bg=c["bg_color"])
        win.resizable(False, False)
        win.attributes("-topmost", True)
        LBL = dict(bg=c["bg_color"], fg=c["text_color"], font=("微软雅黑", 9), anchor="e", width=20)
        ENT = dict(width=12, font=("Consolas", 10), bg="#2a2f38", fg="#ffffff",
                   insertbackground="#ffffff")
        ents = {}
        r = 0
        orig_alpha = float(c.get("window_alpha", 0.85))
        orig_fa = int(c.get("font_alpha", 100))
        orig_hotkey = c["hotkey"]

        def make_swatch_row(parent, current, presets, with_trans=False, init_trans=False):
            """通用色块选择行，返回 (frame, state)"""
            state = {"color": current, "transparent": False}
            frame = tk.Frame(parent, bg=parent["bg"])
            swatches = []
            trans_var = [None]

            def paint():
                for s in swatches:
                    if not state["transparent"] and s["color"] == state["color"]:
                        s["w"].config(highlightbackground="#ffffff", highlightthickness=2)
                    else:
                        s["w"].config(highlightbackground=parent["bg"], highlightthickness=1)

            def pick(col):
                state["color"] = col
                if with_trans:
                    state["transparent"] = False
                    trans_var[0].set(False)
                paint()

            def custom():
                rgb = colorchooser.askcolor(color=state["color"], parent=win)
                if rgb and rgb[1]:
                    state["color"] = rgb[1]
                    if with_trans:
                        state["transparent"] = False
                        trans_var[0].set(False)
                    paint()
            for col in presets:
                b = tk.Label(frame, bg=col, width=3, height=1, cursor="hand2")
                b.pack(side="left", padx=2)
                b.bind("<Button-1>", lambda e, col=col: pick(col))
                swatches.append({"w": b, "color": col})
            tk.Button(frame, text="自定义", command=custom, bg="#3a4150", fg="#ffffff",
                      font=("微软雅黑", 8), relief="flat", cursor="hand2").pack(side="left", padx=(6, 2))
            if with_trans:
                state["transparent"] = bool(init_trans)
                trans_var[0] = tk.BooleanVar(value=state["transparent"])

                def toggle_trans():
                    state["transparent"] = trans_var[0].get()
                    paint()
                tk.Checkbutton(frame, text="背景透明", variable=trans_var[0], command=toggle_trans,
                               bg=c["bg_color"], fg=c["text_color"], selectcolor="#3a4150",
                               activebackground=c["bg_color"], activeforeground=c["text_color"],
                               font=("微软雅黑", 8)).pack(side="left", padx=(6, 0))
            paint()
            return frame, state

        def live_alpha(v):
            c["window_alpha"] = float(v)
            self._refresh_transparency()
        # --- 窗口透明度滑杆（实时预览，含分时图） ---
        tk.Label(win, text="窗口透明度", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        alpha_var = tk.DoubleVar(value=orig_alpha)
        tk.Scale(win, from_=0.10, to=1.0, resolution=0.01, orient="horizontal",
                 length=200, variable=alpha_var, showvalue=True, highlightthickness=0,
                 bg=c["bg_color"], fg=c["text_color"], troughcolor="#3a4150",
                 activebackground="#4a90d9", command=live_alpha
                 ).grid(row=r, column=1, columnspan=2, padx=(0, 8), pady=3, sticky="w")
        r += 1
        # --- 背景色色块 + 透明 ---
        tk.Label(win, text="背景色", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        bg_frame, bg_state = make_swatch_row(
            win, c.get("bg_color", "#1e222a"),
            ["#1e222a", "#0d0d0d", "#ffffff", "#f7f3e8", "#eef2f7",
             "#2d4a3e", "#4a2d3e", "#3a2d4a", "#4a3b2d", "#12324a"],
            with_trans=True, init_trans=bool(c.get("transparent_bg", False)))
        bg_frame.grid(row=r, column=1, columnspan=2, padx=(0, 8), pady=3, sticky="w")
        r += 1
        # --- 字体颜色色块 ---
        tk.Label(win, text="字体颜色", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        text_frame, text_state = make_swatch_row(
            win, c.get("text_color", "#ffffff"),
            ["#ffffff", "#000000", "#ffcc00", "#7fd4ff", "#ffb3b3",
             "#c8ffc8", "#ffd9b3", "#e6d5ff", "#d9d9d9", "#f5f5dc"])
        text_frame.grid(row=r, column=1, columnspan=2, padx=(0, 8), pady=3, sticky="w")
        r += 1
        # --- 涨/跌/平颜色 ---
        for label, key in [("涨的颜色", "up_color"), ("跌的颜色", "down_color"),
                           ("平的颜色", "flat_color")]:
            tk.Label(win, text=label, **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
            e = tk.Entry(win, **ENT)
            e.insert(0, c.get(key, "#ffffff"))
            e.grid(row=r, column=1, padx=(0, 4), pady=3, sticky="w")

            def pick(key=key, e=e):
                rgb = colorchooser.askcolor(color=e.get(), parent=win, title=key)
                if rgb and rgb[1]:
                    e.delete(0, tk.END)
                    e.insert(0, rgb[1])
            tk.Button(win, text="🎨", command=pick, bg="#3a4150", fg="#ffffff",
                      relief="flat", font=("微软雅黑", 9), cursor="hand2").grid(
                row=r, column=2, padx=(0, 8))
            ents[key] = e
            r += 1
        # --- 字体透明度滑杆 ---
        tk.Label(win, text="字体透明度", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        fa_var = tk.IntVar(value=orig_fa)
        tk.Scale(win, from_=0, to=100, resolution=1, orient="horizontal",
                 length=200, variable=fa_var, showvalue=True, highlightthickness=0,
                 bg=c["bg_color"], fg=c["text_color"], troughcolor="#3a4150",
                 activebackground="#4a90d9", command=self._preview_font_alpha
                 ).grid(row=r, column=1, columnspan=2, padx=(0, 8), pady=3, sticky="w")
        r += 1
        # --- 显示列 ---
        tk.Label(win, text="显示列", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="ne")
        colf = tk.Frame(win, bg=c["bg_color"])
        colf.grid(row=r, column=1, columnspan=2, padx=(0, 8), pady=3, sticky="w")
        col_vars = {}
        for i, (key, head, _) in enumerate(COLS):
            v = tk.BooleanVar(value=c["columns"].get(key, True))
            col_vars[key] = v
            tk.Checkbutton(colf, text=head, variable=v,
                           bg=c["bg_color"], fg=c["text_color"], selectcolor="#3a4150",
                           activebackground=c["bg_color"], activeforeground=c["text_color"],
                           font=("微软雅黑", 8)).grid(row=i // 4, column=i % 4, sticky="w")
        r += 1
        # --- 数值项 ---
        for label, key in [("字体大小 (pt)", "font_size"),
                           ("行高 (px)", "row_height"), ("刷新间隔 (秒)", "refresh_interval")]:
            tk.Label(win, text=label, **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
            e = tk.Entry(win, **ENT)
            e.insert(0, str(c.get(key)))
            e.grid(row=r, column=1, columnspan=2, padx=(0, 8), pady=3, sticky="w")
            ents[key] = e
            r += 1
        # --- 字体名称（空=自动选纤细字体） ---
        tk.Label(win, text="字体名称(空=自动纤细)", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        e = tk.Entry(win, **ENT)
        e.insert(0, c.get("font_family", ""))
        e.grid(row=r, column=1, columnspan=2, padx=(0, 8), pady=3, sticky="w")
        ents["font_family"] = e
        r += 1
        # --- 分时图背景色 + 透明 ---
        tk.Label(win, text="分时图背景", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        chart_frame, chart_state = make_swatch_row(
            win, c.get("chart_bg_color", "#14171c"),
            ["#14171c", "#000000", "#ffffff", "#1e222a", "#f5f5dc",
             "#0d1b2a", "#1b2a1b", "#2a1b1b", "#2a2337", "#0d0d0d"],
            with_trans=True, init_trans=bool(c.get("chart_transparent", False)))
        r += 1
        # --- 快捷键：可下拉 / 手输 / 录制 ---
        tk.Label(win, text="全局快捷键", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        hkf = tk.Frame(win, bg=c["bg_color"])
        hkf.grid(row=r, column=1, columnspan=2, padx=(0, 8), pady=3, sticky="w")
        hk_var = tk.StringVar(value=c["hotkey"])
        hk_combo = ttk.Combobox(hkf, textvariable=hk_var, width=12,
                                values=["ctrl+alt+h", "ctrl+alt+s", "ctrl+alt+x",
                                        "ctrl+shift+a", "f9", "f10", "alt+q"])
        hk_combo.pack(side="left")
        rec_btn = tk.Button(hkf, text="🎙录制", bg="#3a4150", fg="#ffffff",
                            relief="flat", font=("微软雅黑", 9), cursor="hand2")

        def toggle_record():
            if getattr(toggle_record, "recording", False):
                toggle_record.recording = False
                rec_btn.config(text="🎙录制")
                return
            toggle_record.recording = True
            rec_btn.config(text="按组合键…")

            def worker():
                combo = None
                try:
                    combo = keyboard.read_hotkey(suppress=False)
                except Exception:
                    pass
                def done():
                    toggle_record.recording = False
                    try:
                        rec_btn.config(text="🎙录制")
                    except Exception:
                        pass
                    if combo and win.winfo_exists():
                        hk_var.set(combo.strip().lower())
                self.root.after(0, done)
            threading.Thread(target=worker, daemon=True).start()
        rec_btn.config(command=toggle_record)
        rec_btn.pack(side="left", padx=(6, 0))
        r += 1
        # --- 托盘图标 ---
        tk.Label(win, text="托盘图标 (png/ico)", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        icon_ent = tk.Entry(win, **ENT)
        icon_ent.insert(0, c.get("tray_icon", ""))
        icon_ent.grid(row=r, column=1, padx=(0, 4), pady=3, sticky="w")

        def browse():
            p = filedialog.askopenfilename(filetypes=[("图片", "*.png *.ico *.jpg")])
            if p:
                icon_ent.delete(0, tk.END)
                icon_ent.insert(0, p)
        tk.Button(win, text="...", command=browse, bg="#3a4150", fg="#ffffff",
                  relief="flat", font=("微软雅黑", 9)).grid(row=r, column=2, padx=(0, 8))
        ents["tray_icon"] = icon_ent
        r += 1
        # --- 显示标题栏 / 置顶 ---
        tk.Label(win, text="显示标题栏", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        bar_opt = tk.StringVar(value="是" if c.get("show_bar", True) else "否")
        tk.OptionMenu(win, bar_opt, "是", "否").grid(row=r, column=1, padx=(0, 8), sticky="w")
        r += 1
        tk.Label(win, text="窗口置顶", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        top_var = tk.StringVar(value="是" if c.get("always_on_top", True) else "否")
        tk.OptionMenu(win, top_var, "是", "否").grid(row=r, column=1, padx=(0, 8), sticky="w")
        r += 1
        # --- 极简模式 / 分时图标题栏 ---
        tk.Label(win, text="极简模式", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        minimal_opt = tk.StringVar(value="是" if c.get("minimal_mode", False) else "否")
        tk.OptionMenu(win, minimal_opt, "是", "否").grid(row=r, column=1, padx=(0, 8), sticky="w")
        r += 1
        tk.Label(win, text="分时图标题栏", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        chart_bar_opt = tk.StringVar(value="是" if c.get("chart_show_bar", True) else "否")
        tk.OptionMenu(win, chart_bar_opt, "是", "否").grid(row=r, column=1, padx=(0, 8), sticky="w")
        r += 1
        # --- 自动隐藏 / 单击打开分时图 ---
        tk.Label(win, text="自动隐藏秒数(0=关)", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        e = tk.Entry(win, **ENT)
        e.insert(0, str(c.get("auto_hide", 0)))
        e.grid(row=r, column=1, columnspan=2, padx=(0, 8), pady=3, sticky="w")
        ents["auto_hide"] = e
        r += 1
        tk.Label(win, text="单击打开分时图", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        co_opt = tk.StringVar(value="是" if c.get("click_open", False) else "否")
        tk.OptionMenu(win, co_opt, "是", "否").grid(row=r, column=1, padx=(0, 8), sticky="w")
        r += 1

        def do_apply(persist):
            """应用设置到界面；persist=True 时同时写入配置文件"""
            c["window_alpha"] = max(0.1, min(1.0, float(alpha_var.get())))
            c["bg_color"] = bg_state["color"]
            c["transparent_bg"] = bg_state["transparent"]
            c["text_color"] = text_state["color"]
            self.root.winfo_rgb(c["text_color"])
            for k in ("up_color", "down_color", "flat_color"):
                c[k] = ents[k].get().strip()
                self.root.winfo_rgb(c[k])
            c["font_alpha"] = max(0, min(100, int(fa_var.get())))
            c["font_size"] = max(8, min(24, int(ents["font_size"].get())))
            c["font_family"] = ents["font_family"].get().strip()
            self._resolve_font()
            c["row_height"] = max(18, min(50, int(ents["row_height"].get())))
            c["refresh_interval"] = max(1, min(60, int(ents["refresh_interval"].get())))
            c["always_on_top"] = top_var.get() == "是"
            c["show_bar"] = bar_opt.get() == "是"
            c["minimal_mode"] = minimal_opt.get() == "是"
            c["chart_show_bar"] = chart_bar_opt.get() == "是"
            c["auto_hide"] = max(0, min(300, int(ents["auto_hide"].get())))
            c["click_open"] = co_opt.get() == "是"
            c["tray_icon"] = ents["tray_icon"].get().strip()
            c["columns"] = {k: bool(v.get()) for k, v in col_vars.items()}
            c["chart_bg_color"] = chart_state["color"]
            c["chart_transparent"] = chart_state["transparent"]
            self._apply_topmost()
            self._apply_style()
            self._rebuild_columns()
            self._reload_tray_icon()
            if persist:
                # 快捷键：先验证注册新键，成功才替换旧键
                new_hk = hk_var.get().strip().lower()
                if new_hk != orig_hotkey:
                    self._bind_hotkey(new_hk)
                save_config(c)

        def apply():
            try:
                do_apply(persist=False)
                # 应用后作为新的"原始值"，取消关闭时不再回退
                nonlocal orig_alpha, orig_fa
                orig_alpha = float(alpha_var.get())
                orig_fa = int(fa_var.get())
            except ValueError as ve:
                messagebox.showerror("错误", "应用失败：%s" % ve, parent=win)
            except tk.TclError:
                messagebox.showerror("错误", "颜色格式不正确，应为 #RRGGBB", parent=win)

        def save():
            try:
                do_apply(persist=True)
                win.destroy()
            except ValueError as ve:
                messagebox.showerror("错误", "保存失败：%s" % ve, parent=win)
            except tk.TclError:
                messagebox.showerror("错误", "颜色格式不正确，应为 #RRGGBB", parent=win)

        def on_close():
            c["window_alpha"] = orig_alpha
            c["font_alpha"] = orig_fa
            self._apply_tag_colors()
            self._refresh_transparency()
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", on_close)
        btnf = tk.Frame(win, bg=c["bg_color"])
        btnf.grid(row=r, column=0, columnspan=3, pady=12)
        tk.Button(btnf, text="应用(预览)", command=apply, bg="#3a6ea5", fg="#ffffff",
                  font=("微软雅黑", 10), relief="flat", cursor="hand2",
                  width=10).pack(side="left", padx=6)
        tk.Button(btnf, text="保存", command=save, bg="#4a90d9", fg="#ffffff",
                  font=("微软雅黑", 10), relief="flat", cursor="hand2",
                  width=10).pack(side="left", padx=6)

    # ---------- 退出 ----------
    def quit_app(self):
        save_config(self.cfg)
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        self.root.destroy()
        os._exit(0)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
