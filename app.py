# -*- coding: utf-8 -*-
"""
桌面股票监控小工具
功能：实时行情（涨跌幅/量比/成交量/最高/最低）、全局快捷键一键隐藏、
     双击行查看分时图、界面透明/背景色/字体颜色与透明度可调、
     最小化到托盘、自定义托盘图标。
数据源：腾讯行情接口（免费，无需Key）
"""
import json
import os
import re
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import urllib.request

import keyboard
import pystray
from PIL import Image, ImageDraw, ImageTk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEFAULT_TRAY = os.path.join(BASE_DIR, "default_tray.png")

DEFAULT_CONFIG = {
    "stocks": ["sh600519", "sz000001", "sh510300"],
    "refresh_interval": 3,
    "window_alpha": 0.85,
    "bg_color": "#1e222a",
    "text_color": "#ffffff",
    "up_color": "#ff4040",
    "down_color": "#00cc66",
    "flat_color": "#c8c8c8",
    "font_alpha": 100,
    "font_size": 11,
    "row_height": 26,
    "hotkey": "ctrl+alt+h",
    "always_on_top": True,
    "tray_icon": "",
    "window_pos": None,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
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
    # 阳线
    dr.rectangle([12, 22, 26, 48], fill=(244, 64, 64, 255))
    dr.rectangle([18, 12, 20, 54], fill=(244, 64, 64, 255))
    # 阴线
    dr.rectangle([38, 16, 52, 40], fill=(0, 204, 102, 255))
    dr.rectangle([44, 10, 46, 48], fill=(0, 204, 102, 255))
    img.save(DEFAULT_TRAY)


class MinuteChart(tk.Toplevel):
    """分时图窗口"""

    def __init__(self, master, code, name_hint=""):
        super().__init__(master)
        self.code = code
        self.title("分时图 - %s" % (name_hint or code))
        self.configure(bg="#14171c")
        self.resizable(True, True)
        self.geometry("880x480")
        self.attributes("-topmost", master.attributes("-topmost"))
        self.canvas = tk.Canvas(self, bg="#14171c", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.bind("<Configure>", lambda e: self.draw())
        self.after(200, self.refresh)
        self.refresh()

    def refresh(self):
        try:
            data = fetch_minute(self.code)
            threading.Thread(target=self._set_data, args=(data,), daemon=True).start()
        except Exception:
            self._set_data(("", None, []))
        self.after(10000, self.refresh)

    def _set_data(self, data):
        self.after(0, lambda: self._apply(data))

    def _apply(self, data):
        self.name, self.prev, self.rows = data
        self.draw()

    def draw(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 50 or h < 50:
            return
        rows = getattr(self, "rows", [])
        prev = getattr(self, "prev", None)
        if not rows:
            c.create_text(w // 2, h // 2, text="暂无分时数据（可能停牌）",
                          fill="#c8c8c8", font=("微软雅黑", 12))
            return
        left, right, top, bottom = 46, 66, 34, 30
        pw, ph = w - left - right, h - top - bottom

        # 计算均价线
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

        def X(i):
            return left + i * pw / max(240, len(rows) - 1)

        def Y(p):
            return top + (hi - p) * ph / (hi - lo)

        # 网格 + 价格刻度
        for i in range(5):
            y = top + ph * i / 4
            c.create_line(left, y, left + pw, y, fill="#2a2f38")
            pv = hi - (hi - lo) * i / 4
            color = "#ff4040" if pv > (prev or 0) else ("#00cc66" if pv < (prev or 0) else "#c8c8c8")
            c.create_text(left + pw + 8, y, text="%.3f" % pv, fill=color,
                          anchor="w", font=("微软雅黑", 9))
        # 昨收虚线
        if prev:
            y = Y(prev)
            c.create_line(left, y, left + pw, y, fill="#888888", dash=(4, 3))
        # 时间刻度
        labels = [(0, "09:30"), (60, "10:30"), (120, "11:30/13:00"),
                  (180, "14:00"), (240, "15:00")]
        for idx, txt in labels:
            x = X(idx)
            c.create_line(x, top, x, top + ph, fill="#2a2f38")
            c.create_text(x, top + ph + 12, text=txt, fill="#8a8f99",
                          font=("微软雅黑", 9))
        # 分时线
        color = "#ff4040" if prices[-1] >= (prev or 0) else "#00cc66"
        pts = []
        for i, p in enumerate(prices):
            pts.extend([X(i), Y(p)])
        if len(pts) >= 4:
            c.create_line(pts, fill=color, width=1.4)
        # 均价线（黄）
        apts = []
        for i, a in enumerate(avgs):
            apts.extend([X(i), Y(a)])
        if len(apts) >= 4:
            c.create_line(apts, fill="#ffcc00", width=1)
        # 标题
        pct = ""
        if prev:
            pct = "  %+.2f%%" % ((prices[-1] / prev - 1) * 100)
        c.create_text(left, 14, anchor="w",
                      text="%s  %.3f%s" % (getattr(self, "name", ""), prices[-1], pct),
                      fill=color, font=("微软雅黑", 12, "bold"))
        c.create_text(left + 220, 14, anchor="w", text="— 分时  — 均价",
                      fill="#ffcc00", font=("微软雅黑", 9))


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.q = queue.Queue()
        self.hotkey_handle = None
        self.tray = None
        self.quote_cache = {}
        self.charts = {}

        if not os.path.exists(DEFAULT_TRAY):
            try:
                make_default_tray_icon()
            except Exception:
                pass

        self.root.title("股票监控")
        self.root.overrideredirect(True)
        self.root.attributes("-alpha", float(self.cfg.get("window_alpha", 0.85)))
        self.root.configure(bg=self.cfg["bg_color"])
        self._apply_topmost()
        pos = self.cfg.get("window_pos")
        if pos:
            self.root.geometry("+%d+%d" % (pos[0], pos[1]))
        else:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            self.root.geometry("+%d+%d" % (sw - 580, 80))

        self._build_ui()
        self._apply_style()
        self._bind_hotkey(self.cfg["hotkey"])
        self._setup_tray()

        self.root.after(300, self._poll_queue)
        self.fetch_loop()

    # ---------- UI ----------
    def _build_ui(self):
        c = self.cfg
        bar = tk.Frame(self.root, bg=blend(c["text_color"], c["bg_color"], 0.15))
        bar.pack(fill=tk.X)
        self.title_lbl = tk.Label(bar, text="📈 股票监控", bg=bar["bg"],
                                  fg=blend(c["text_color"], c["bg_color"], 0.9),
                                  font=("微软雅黑", 10, "bold"))
        self.title_lbl.pack(side=tk.LEFT, padx=8, pady=3)
        btn_ks = dict(bg=bar["bg"], fg=blend(c["text_color"], c["bg_color"], 0.75),
                      font=("微软雅黑", 10), bd=0, activebackground=bar["bg"],
                      activeforeground=c["text_color"], cursor="hand2")
        tk.Label(bar, text="⚙", **btn_ks).pack(side=tk.RIGHT, padx=6)
        tk.Label(bar, text="—", **btn_ks).pack(side=tk.RIGHT, padx=6)
        self.root.nametowidget(str(bar.winfo_children()[-1])).bind(
            "<Button-1>", lambda e: self.hide_to_tray())
        self.root.nametowidget(str(bar.winfo_children()[-2])).bind(
            "<Button-1>", lambda e: self.open_settings())
        for wdg in (bar, self.title_lbl):
            wdg.bind("<Button-1>", self._drag_start)
            wdg.bind("<B1-Motion>", self._drag_move)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        cols = ("code", "name", "price", "pct", "lb", "vol", "high", "low")
        heads = ("代码", "名称", "现价", "涨跌幅", "量比", "成交量", "最高", "最低")
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings",
                                 style="Stock.Treeview", selectmode="browse")
        for col, head in zip(cols, heads):
            self.tree.heading(col, text=head)
        widths = (64, 84, 62, 66, 52, 72, 58, 58)
        for col, wd in zip(cols, widths):
            self.tree.column(col, width=wd, anchor="center", stretch=False)
        self.tree["displaycolumns"] = cols
        self.tree.pack(fill=tk.X, padx=4, pady=(4, 0))
        self.tree.tag_configure("up", foreground=blend(c["up_color"], c["bg_color"], c["font_alpha"] / 100))
        self.tree.tag_configure("down", foreground=blend(c["down_color"], c["bg_color"], c["font_alpha"] / 100))
        self.tree.tag_configure("flat", foreground=blend(c["flat_color"], c["bg_color"], c["font_alpha"] / 100))
        self.tree.bind("<Double-Button-1>", self._on_double_click)

        self.menu = tk.Menu(self.root, tearoff=0, bg=c["bg_color"], fg=c["text_color"],
                            activebackground="#3a4150", activeforeground="#ffffff")
        self.menu.add_command(label="添加股票", command=self.add_stock_dialog)
        self.menu.add_command(label="删除选中", command=self.remove_selected)
        self.menu.add_command(label="查看分时图", command=self.show_chart_selected)
        self.menu.add_separator()
        self.menu.add_command(label="设置...", command=self.open_settings)
        self.menu.add_command(label="隐藏到托盘", command=self.hide_to_tray)
        self.menu.add_command(label="退出", command=self.quit_app)
        self.tree.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))

        self.status = tk.Label(self.root, text="", anchor="w",
                               bg=c["bg_color"], fg=blend(c["text_color"], c["bg_color"], 0.55),
                               font=("微软雅黑", 8))
        self.status.pack(fill=tk.X, padx=6, pady=(0, 3))
        self._refresh_tree_rows(initial=True)

    def _apply_style(self):
        c = self.cfg
        style = ttk.Style(self.root)
        alpha = c["font_alpha"] / 100
        style.configure("Stock.Treeview", background=c["bg_color"],
                        fieldbackground=c["bg_color"], foreground=c["text_color"],
                        rowheight=int(c["row_height"]), borderwidth=0,
                        font=("微软雅黑", int(c["font_size"])))
        style.configure("Stock.Treeview.Heading", background=blend(c["text_color"], c["bg_color"], 0.18),
                        foreground=blend(c["text_color"], c["bg_color"], 0.9),
                        font=("微软雅黑", int(c["font_size"]), "bold"), borderwidth=0)
        style.map("Stock.Treeview",
                  background=[("selected", blend("#4a90d9", c["bg_color"], 0.9))],
                  foreground=[("selected", "#ffffff")])
        style.map("Stock.Treeview.Heading", background=[("active", blend(c["text_color"], c["bg_color"], 0.25))])
        self.root.attributes("-alpha", float(c["window_alpha"]))
        self.root.configure(bg=c["bg_color"])
        for tag, color in (("up", c["up_color"]), ("down", c["down_color"]),
                           ("flat", c["flat_color"])):
            self.tree.tag_configure(tag, foreground=blend(color, c["bg_color"], alpha))
        self.status.configure(bg=c["bg_color"], fg=blend(c["text_color"], c["bg_color"], 0.55))
        self.root.update_idletasks()
        n = len(self.cfg["stocks"])
        h = self.tree.winfo_reqheight()
        self.root.geometry("%dx%d" % (self.root.winfo_width(), 30 + h + 24))

    def _refresh_tree_rows(self, initial=False):
        """根据配置重建行（仅代码列表变化时）"""
        self.tree.delete(*self.tree.get_children())
        for code in self.cfg["stocks"]:
            self.tree.insert("", tk.END, iid=code,
                             values=(code.replace("sh", "SH ").replace("sz", "SZ ").replace("bj", "BJ "),
                                     "--", "--", "--", "--", "--", "--", "--"),
                             tags=("flat",))
        if not initial:
            self._apply_style()

    # ---------- 拖动 ----------
    def _drag_start(self, e):
        self._dx, self._dy = e.x, e.y

    def _drag_move(self, e):
        x = self.root.winfo_x() + e.x - self._dx
        y = self.root.winfo_y() + e.y - self._dy
        self.root.geometry("+%d+%d" % (x, y))
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
            price = "%.2f" % q["price"] if q["price"] is not None else "--"
            pct_s = "%+.2f%%" % pct if pct is not None else "--"
            lb = "%.2f" % q["liangbi"] if q["liangbi"] is not None else "--"
            hi = "%.2f" % q["high"] if q["high"] is not None else "--"
            lo = "%.2f" % q["low"] if q["low"] is not None else "--"
            self.tree.item(code, values=(
                code[:2].upper() + " " + code[2:], q["name"] or "--",
                price, pct_s, lb, fmt_vol(q["vol"]), hi, lo), tags=(tag,))
        self.status.config(text="更新于 %s   |   双击行看分时图 · 右键菜单 · %s 隐藏/显示" %
                           (time.strftime("%H:%M:%S"), self.cfg["hotkey"].upper()))

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
        win = self.charts.get(code)
        try:
            if win is not None and win.winfo_exists():
                win.lift()
                win.focus_force()
                return
        except Exception:
            pass
        name = ""
        if code in self.quote_cache:
            name = self.quote_cache[code].get("name") or ""
        self.charts[code] = MinuteChart(self.root, code, name)

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
        try:
            if self.hotkey_handle is not None:
                keyboard.remove_hotkey(self.hotkey_handle)
        except Exception:
            pass
        self.hotkey_handle = keyboard.add_hotkey(hk, lambda: self.root.after(0, self.toggle_show))
        self.cfg["hotkey"] = hk

    def toggle_show(self):
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.attributes("-topmost", self.cfg.get("always_on_top", True))
            self.root.lift()
        else:
            self.hide_to_tray()

    def hide_to_tray(self):
        self.root.withdraw()

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
        self.root.attributes("-topmost", bool(self.cfg.get("always_on_top", True)))

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
        fields = [
            ("窗口透明度 (0.1~1)", "window_alpha"),
            ("背景色 (#hex)", "bg_color"),
            ("字体颜色 (#hex)", "text_color"),
            ("涨的颜色 (#hex)", "up_color"),
            ("跌的颜色 (#hex)", "down_color"),
            ("平的颜色 (#hex)", "flat_color"),
            ("字体不透明度 (0~100)", "font_alpha"),
            ("字体大小 (pt)", "font_size"),
            ("行高 (px)", "row_height"),
            ("刷新间隔 (秒)", "refresh_interval"),
            ("全局快捷键 (如 ctrl+alt+h)", "hotkey"),
        ]
        ents = {}
        for i, (label, key) in enumerate(fields):
            tk.Label(win, text=label, bg=c["bg_color"], fg=c["text_color"],
                     font=("微软雅黑", 9), anchor="e", width=22).grid(row=i, column=0, padx=(12, 4), pady=3, sticky="e")
            v = c.get(key)
            if isinstance(v, float):
                v = str(v)
            elif not isinstance(v, str):
                v = str(v)
            e = tk.Entry(win, width=16, font=("Consolas", 10),
                         bg="#2a2f38", fg="#ffffff", insertbackground="#ffffff")
            e.insert(0, v)
            e.grid(row=i, column=1, padx=(0, 8), pady=3, sticky="w")
            ents[key] = e
        # 托盘图标行
        r = len(fields)
        tk.Label(win, text="托盘图标 (png/ico)", bg=c["bg_color"], fg=c["text_color"],
                 font=("微软雅黑", 9), anchor="e", width=22).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        icon_ent = tk.Entry(win, width=16, font=("Consolas", 10),
                            bg="#2a2f38", fg="#ffffff", insertbackground="#ffffff")
        icon_ent.insert(0, c.get("tray_icon", ""))
        icon_ent.grid(row=r, column=1, padx=(0, 4), pady=3, sticky="w")

        def browse():
            p = filedialog.askopenfilename(filetypes=[("图片", "*.png *.ico *.jpg")])
            if p:
                icon_ent.delete(0, tk.END)
                icon_ent.insert(0, p)
        tk.Button(win, text="...", command=browse, bg="#3a4150", fg="#ffffff",
                  relief="flat", font=("微软雅黑", 9)).grid(row=r, column=2, padx=(0, 8))
        # 置顶
        r += 1
        tk.Label(win, text="窗口置顶 (是/否)", bg=c["bg_color"], fg=c["text_color"],
                 font=("微软雅黑", 9), anchor="e", width=22).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        top_var = tk.StringVar(value="是" if c.get("always_on_top", True) else "否")
        tk.OptionMenu(win, top_var, "是", "否").grid(row=r, column=1, padx=(0, 8), sticky="w")

        def save():
            try:
                c["window_alpha"] = max(0.1, min(1.0, float(ents["window_alpha"].get())))
                for k in ("bg_color", "text_color", "up_color", "down_color", "flat_color"):
                    c[k] = ents[k].get().strip()
                    self.root.winfo_rgb(c[k])  # 校验颜色
                c["font_alpha"] = max(0, min(100, int(ents["font_alpha"].get())))
                c["font_size"] = max(8, min(24, int(ents["font_size"].get())))
                c["row_height"] = max(18, min(50, int(ents["row_height"].get())))
                c["refresh_interval"] = max(1, min(60, int(ents["refresh_interval"].get())))
                c["always_on_top"] = top_var.get() == "是"
                old_hk = c["hotkey"]
                c["hotkey"] = ents["hotkey"].get().strip()
                c["tray_icon"] = icon_ent.get().strip()
                save_config(c)
                self._apply_topmost()
                self._apply_style()
                if c["hotkey"] != old_hk:
                    self._bind_hotkey(c["hotkey"])
                self._reload_tray_icon()
                win.destroy()
            except ValueError as ve:
                messagebox.showerror("错误", "参数格式不正确：%s" % ve, parent=win)
            except tk.TclError:
                messagebox.showerror("错误", "颜色格式不正确，应为 #RRGGBB", parent=win)
        tk.Button(win, text="保存", command=save, bg="#4a90d9", fg="#ffffff",
                  font=("微软雅黑", 10), relief="flat", cursor="hand2",
                  width=10).grid(row=r + 1, column=0, columnspan=3, pady=12)

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
