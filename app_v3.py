# -*- coding: utf-8 -*-
"""
桌面股票监控小工具 v3
新增：标题栏可隐藏/显示（设置+右键菜单）；字体透明度滑杆实时调节；
     字体颜色色块选择器；修复全局快捷键设置（先验证新键再卸旧键，可下拉选择常用组合）。
v2功能：鼠标中键隐藏；透明度滑杆；背景色色块选择（含背景透明）；取色器。
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
from tkinter import ttk, filedialog, messagebox, colorchooser
import urllib.request

import keyboard
import pystray
from PIL import Image, ImageDraw, ImageTk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEFAULT_TRAY = os.path.join(BASE_DIR, "default_tray.png")
MAGIC = "#ABCDEF"  # 背景透明时占用的魔法色（配合 -transparentcolor）
TRANSPARENT_BLEND_BASE = "#1e222a"  # 背景透明时字体混色基准

DEFAULT_CONFIG = {
    "stocks": ["sh600519", "sz000001", "sh510300"],
    "refresh_interval": 3,
    "window_alpha": 0.85,
    "bg_color": "#1e222a",
    "transparent_bg": False,
    "show_bar": True,
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
    dr.rectangle([12, 22, 26, 48], fill=(244, 64, 64, 255))
    dr.rectangle([18, 12, 20, 54], fill=(244, 64, 64, 255))
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
        try:
            # 分时图透明度跟随主窗口
            self.attributes("-alpha", master.attributes("-alpha"))
        except Exception:
            pass
        self.canvas = tk.Canvas(self, bg="#14171c", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.bind("<Configure>", lambda e: self.draw())
        self.after(200, self.refresh)
        self.refresh()

    def refresh(self):
        def worker():
            try:
                data = fetch_minute(self.code)
            except Exception:
                data = ("", None, [])
            self.after(0, lambda: self._apply(data))
        threading.Thread(target=worker, daemon=True).start()
        self.after(10000, self.refresh)

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

        for i in range(5):
            y = top + ph * i / 4
            c.create_line(left, y, left + pw, y, fill="#2a2f38")
            pv = hi - (hi - lo) * i / 4
            color = "#ff4040" if pv > (prev or 0) else ("#00cc66" if pv < (prev or 0) else "#c8c8c8")
            c.create_text(left + pw + 8, y, text="%.3f" % pv, fill=color,
                          anchor="w", font=("微软雅黑", 9))
        if prev:
            y = Y(prev)
            c.create_line(left, y, left + pw, y, fill="#888888", dash=(4, 3))
        labels = [(0, "09:30"), (60, "10:30"), (120, "11:30/13:00"),
                  (180, "14:00"), (240, "15:00")]
        for idx, txt in labels:
            x = X(idx)
            c.create_line(x, top, x, top + ph, fill="#2a2f38")
            c.create_text(x, top + ph + 12, text=txt, fill="#8a8f99",
                          font=("微软雅黑", 9))
        color = "#ff4040" if prices[-1] >= (prev or 0) else "#00cc66"
        pts = []
        for i, p in enumerate(prices):
            pts.extend([X(i), Y(p)])
        if len(pts) >= 4:
            c.create_line(pts, fill=color, width=1.4)
        apts = []
        for i, a in enumerate(avgs):
            apts.extend([X(i), Y(a)])
        if len(apts) >= 4:
            c.create_line(apts, fill="#ffcc00", width=1)
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
        try:
            self._bind_hotkey(self.cfg["hotkey"])
        except Exception:
            self._bind_hotkey("ctrl+alt+h")
        self._setup_tray()

        # 鼠标中键（滚珠）点击界面任意位置 → 隐藏到托盘
        self.root.bind("<Button-2>", lambda e: self.hide_to_tray())

        self.root.after(300, self._poll_queue)
        self.fetch_loop()

    def _blend_base(self):
        """字体混色的背景基准色"""
        return TRANSPARENT_BLEND_BASE if self.cfg.get("transparent_bg") else self.cfg["bg_color"]

    # ---------- UI ----------
    def _build_ui(self):
        c = self.cfg
        base = self._blend_base()
        self.bar = tk.Frame(self.root, bg=blend(c["text_color"], base, 0.15))
        self.bar.pack(fill=tk.X)
        bar_bg = self.bar["bg"]
        self.title_lbl = tk.Label(self.bar, text="📈 股票监控", bg=bar_bg,
                                  fg=blend(c["text_color"], base, 0.9),
                                  font=("微软雅黑", 10, "bold"))
        self.title_lbl.pack(side=tk.LEFT, padx=8, pady=3)
        btn_ks = dict(bg=bar_bg, fg=blend(c["text_color"], base, 0.75),
                      font=("微软雅黑", 10), bd=0, activebackground=bar_bg,
                      activeforeground=c["text_color"], cursor="hand2")
        tk.Label(self.bar, text="⚙", **btn_ks).pack(side=tk.RIGHT, padx=6)
        b_hide = tk.Label(self.bar, text="—", **btn_ks)
        b_hide.pack(side=tk.RIGHT, padx=6)
        b_hide.bind("<Button-1>", lambda e: self.hide_to_tray())
        self.root.nametowidget(str(self.bar.winfo_children()[-2])).bind(
            "<Button-1>", lambda e: self.open_settings())
        for wdg in (self.bar, self.title_lbl):
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
        self.tree.bind("<Double-Button-1>", self._on_double_click)

        self.menu = tk.Menu(self.root, tearoff=0, bg=c["bg_color"], fg=c["text_color"],
                            activebackground="#3a4150", activeforeground="#ffffff")
        self.menu.add_command(label="添加股票", command=self.add_stock_dialog)
        self.menu.add_command(label="删除选中", command=self.remove_selected)
        self.menu.add_command(label="查看分时图", command=self.show_chart_selected)
        self.menu.add_separator()
        self.menu.add_command(label="设置...", command=self.open_settings)
        self.bar_var = tk.BooleanVar(value=bool(c.get("show_bar", True)))
        self.menu.add_checkbutton(label="显示标题栏", variable=self.bar_var,
                                  command=self.toggle_bar)
        self.menu.add_command(label="隐藏到托盘", command=self.hide_to_tray)
        self.menu.add_command(label="退出", command=self.quit_app)
        self.tree.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))

        self.status = tk.Label(self.root, text="", anchor="w",
                               bg=self._effective_bg(), fg=blend(c["text_color"], base, 0.55),
                               font=("微软雅黑", 8))
        self.status.pack(fill=tk.X, padx=6, pady=(0, 3))
        self._refresh_tree_rows(initial=True)

    def _effective_bg(self):
        """当前实际背景色（透明模式用魔法色）"""
        return MAGIC if self.cfg.get("transparent_bg") else self.cfg["bg_color"]

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

    def _apply_style(self):
        c = self.cfg
        base = self._blend_base()
        bg = self._effective_bg()
        style = ttk.Style(self.root)
        alpha = c["font_alpha"] / 100
        style.configure("Stock.Treeview", background=bg,
                        fieldbackground=bg, foreground=c["text_color"],
                        rowheight=int(c["row_height"]), borderwidth=0,
                        font=("微软雅黑", int(c["font_size"])))
        style.configure("Stock.Treeview.Heading", background=blend(c["text_color"], base, 0.18),
                        foreground=blend(c["text_color"], base, 0.9),
                        font=("微软雅黑", int(c["font_size"]), "bold"), borderwidth=0)
        style.map("Stock.Treeview",
                  background=[("selected", blend("#4a90d9", base, 0.9))],
                  foreground=[("selected", "#ffffff")])
        style.map("Stock.Treeview.Heading", background=[("active", blend(c["text_color"], base, 0.25))])
        self.root.attributes("-alpha", float(c["window_alpha"]))
        try:
            # 背景透明：魔法色区域完全透明（Windows 分层窗口特性）
            self.root.attributes("-transparentcolor", MAGIC)
        except Exception:
            pass
        self.root.configure(bg=bg)
        self._apply_tag_colors()
        self.bar.configure(bg=blend(c["text_color"], base, 0.15))
        self.title_lbl.configure(bg=self.bar["bg"], fg=blend(c["text_color"], base, 0.9))
        self.status.configure(bg=bg, fg=blend(c["text_color"], base, 0.55))
        # 标题栏显示/隐藏
        if c.get("show_bar", True):
            self.bar.pack(fill=tk.X, before=self.tree)
            bar_h = 30
        else:
            self.bar.pack_forget()
            bar_h = 0
        # 已打开的分时图窗口透明度跟随主窗口
        for win in list(self.charts.values()):
            try:
                if win.winfo_exists():
                    win.attributes("-alpha", float(c["window_alpha"]))
            except Exception:
                pass
        self.root.update_idletasks()
        h = self.tree.winfo_reqheight()
        self.root.geometry("%dx%d" % (self.root.winfo_width(), bar_h + h + 24))

    def _refresh_tree_rows(self, initial=False):
        """根据配置重建行（仅代码列表变化时）"""
        self.tree.delete(*self.tree.get_children())
        for code in self.cfg["stocks"]:
            self.tree.insert("", tk.END, iid=code,
                             values=(code[:2].upper() + " " + code[2:],
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
    def _preview_font_alpha(self, v):
        """字体透明度滑杆实时预览"""
        self.cfg["font_alpha"] = int(v)
        self._apply_tag_colors()

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

        def make_swatch_row(parent, current, presets, with_trans=False):
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
                state["transparent"] = bool(c.get("transparent_bg", False))
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
            a = float(v)
            self.root.attributes("-alpha", a)
            for w_ in list(self.charts.values()):
                try:
                    if w_.winfo_exists():
                        w_.attributes("-alpha", a)
                except Exception:
                    pass
        # --- 窗口透明度：拖动滑杆，实时预览（含分时图） ---
        tk.Label(win, text="窗口透明度", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        alpha_var = tk.DoubleVar(value=orig_alpha)
        tk.Scale(win, from_=0.10, to=1.0, resolution=0.01, orient="horizontal",
                 length=200, variable=alpha_var, showvalue=True, highlightthickness=0,
                 bg=c["bg_color"], fg=c["text_color"], troughcolor="#3a4150",
                 activebackground="#4a90d9", command=live_alpha
                 ).grid(row=r, column=1, columnspan=2, padx=(0, 8), pady=3, sticky="w")
        r += 1
        # --- 背景色：色块选择 + 自定义取色 + 透明选项 ---
        tk.Label(win, text="背景色", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        bg_frame, bg_state = make_swatch_row(
            win, c.get("bg_color", "#1e222a"),
            ["#1e222a", "#0d0d0d", "#ffffff", "#f7f3e8", "#eef2f7",
             "#2d4a3e", "#4a2d3e", "#3a2d4a", "#4a3b2d", "#12324a"], with_trans=True)
        bg_frame.grid(row=r, column=1, columnspan=2, padx=(0, 8), pady=3, sticky="w")
        r += 1
        # --- 字体颜色：色块选择 ---
        tk.Label(win, text="字体颜色", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        text_frame, text_state = make_swatch_row(
            win, c.get("text_color", "#ffffff"),
            ["#ffffff", "#000000", "#ffcc00", "#7fd4ff", "#ffb3b3",
             "#c8ffc8", "#ffd9b3", "#e6d5ff", "#d9d9d9", "#f5f5dc"])
        text_frame.grid(row=r, column=1, columnspan=2, padx=(0, 8), pady=3, sticky="w")
        r += 1
        # --- 涨/跌/平颜色：输入框 + 取色器按钮 ---
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
        # --- 字体透明度：拖动滑杆，实时预览 ---
        tk.Label(win, text="字体透明度", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        fa_var = tk.IntVar(value=orig_fa)
        tk.Scale(win, from_=0, to=100, resolution=1, orient="horizontal",
                 length=200, variable=fa_var, showvalue=True, highlightthickness=0,
                 bg=c["bg_color"], fg=c["text_color"], troughcolor="#3a4150",
                 activebackground="#4a90d9", command=self._preview_font_alpha
                 ).grid(row=r, column=1, columnspan=2, padx=(0, 8), pady=3, sticky="w")
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
        # --- 快捷键：可下拉选常用组合，也可手输 ---
        tk.Label(win, text="全局快捷键", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        hk_var = tk.StringVar(value=c["hotkey"])
        hk_combo = ttk.Combobox(win, textvariable=hk_var, width=14,
                                values=["ctrl+alt+h", "ctrl+alt+s", "ctrl+alt+x",
                                        "ctrl+shift+a", "f9", "f10", "alt+q"])
        hk_combo.grid(row=r, column=1, columnspan=2, padx=(0, 8), pady=3, sticky="w")
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
        # --- 显示标题栏 / 窗口置顶 ---
        tk.Label(win, text="显示标题栏", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        bar_opt = tk.StringVar(value="是" if c.get("show_bar", True) else "否")
        tk.OptionMenu(win, bar_opt, "是", "否").grid(row=r, column=1, padx=(0, 8), sticky="w")
        r += 1
        tk.Label(win, text="窗口置顶", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        top_var = tk.StringVar(value="是" if c.get("always_on_top", True) else "否")
        tk.OptionMenu(win, top_var, "是", "否").grid(row=r, column=1, padx=(0, 8), sticky="w")
        r += 1

        def save():
            try:
                c["window_alpha"] = max(0.1, min(1.0, float(alpha_var.get())))
                c["bg_color"] = bg_state["color"]
                c["transparent_bg"] = bg_state["transparent"]
                c["text_color"] = text_state["color"]
                self.root.winfo_rgb(c["text_color"])
                for k in ("up_color", "down_color", "flat_color"):
                    c[k] = ents[k].get().strip()
                    self.root.winfo_rgb(c[k])  # 校验颜色
                c["font_alpha"] = max(0, min(100, int(fa_var.get())))
                c["font_size"] = max(8, min(24, int(ents["font_size"].get())))
                c["row_height"] = max(18, min(50, int(ents["row_height"].get())))
                c["refresh_interval"] = max(1, min(60, int(ents["refresh_interval"].get())))
                c["always_on_top"] = top_var.get() == "是"
                c["show_bar"] = bar_opt.get() == "是"
                c["tray_icon"] = ents["tray_icon"].get().strip()
                # 快捷键最后处理：先验证并注册新键，成功才替换旧键
                new_hk = hk_var.get().strip().lower()
                if new_hk != c["hotkey"]:
                    self._bind_hotkey(new_hk)  # 失败抛 ValueError，旧键保留
                save_config(c)
                self._apply_topmost()
                self._apply_style()
                self._reload_tray_icon()
                win.destroy()
            except ValueError as ve:
                messagebox.showerror("错误", "保存失败：%s" % ve, parent=win)
            except tk.TclError:
                messagebox.showerror("错误", "颜色格式不正确，应为 #RRGGBB", parent=win)

        def on_close():
            self.root.attributes("-alpha", orig_alpha)
            c["font_alpha"] = orig_fa
            self._apply_tag_colors()
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", on_close)
        tk.Button(win, text="保存", command=save, bg="#4a90d9", fg="#ffffff",
                  font=("微软雅黑", 10), relief="flat", cursor="hand2",
                  width=10).grid(row=r, column=0, columnspan=3, pady=12)

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
