# -*- coding: utf-8 -*-
"""
桌面股票监控小工具 v19 —— "真·无背景"（每像素透明度）
原理：不再用颜色键抠图，改用 UpdateLayeredWindow 每像素 alpha：
  背景像素 alpha=0（完全看不见、且鼠标可穿透），文字像素 alpha=255*字体透明度，
  文字边缘由 FreeType 灰度抗锯齿给出平滑半透明 → 无重影、无断线、无毛边。
代价：数据区不再由 tkinter 绘制，改为 Pillow 渲染成位图后推给系统；
      Treeview 保留为"数据模型"（存值/选中），点击由幽灵层换算行号。
数据源：腾讯行情接口（免费，无需Key）
"""
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser
from tkinter import font as tkfont
import urllib.request

import keyboard
import pystray
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageTk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEFAULT_TRAY = os.path.join(BASE_DIR, "default_tray.png")
MAGIC = "#0D0F14"  # 背景透明的魔法色。必须用深色：文字抗锯齿/ClearType 的边缘像素
                   # 是"字色与背景色的混合"，用浅蓝(#ABCDEF)时边缘会变成白色阴影；
                   # 改深色后边缘自然变暗，字体在任意桌面背景下都清晰
TRANSPARENT_BLEND_BASE = "#1e222a"  # 背景透明时字体混色基准

# ---- 每像素 alpha 渲染（UpdateLayeredWindow）----
FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
FONT_FILES = {
    "等线 Light": ["Dengl.ttf", "dengl.ttf"],
    "等线": ["Deng.ttf", "deng.ttf"],
    "Microsoft YaHei UI Light": ["msyhl.ttc"],
    "微软雅黑": ["msyh.ttc"],
}
FONT_FILES_BOLD = ["msyhbd.ttc", "Dengb.ttf", "simhei.ttf"]
PX_SS = 3  # 超采样倍数：3x 渲染后 LANCZOS 缩回，笔画最干净（不会断线/毛边）


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BLENDFUNC(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_ulong), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_ushort),
                ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_ulong),
                ("biSizeImage", ctypes.c_ulong), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_ulong),
                ("biClrImportant", ctypes.c_ulong)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_ulong * 3)]


def _gdi_setup():
    """设置 Win32 函数签名（64 位下句柄必须用 c_void_p，否则会被截断）"""
    u = ctypes.windll.user32
    g = ctypes.windll.gdi32
    u.GetDC.restype = ctypes.c_void_p
    u.GetDC.argtypes = [ctypes.c_void_p]
    u.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u.UpdateLayeredWindow.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_POINT), ctypes.POINTER(_SIZE),
        ctypes.c_void_p, ctypes.POINTER(_POINT), ctypes.c_uint32,
        ctypes.POINTER(_BLENDFUNC), ctypes.c_uint32]
    g.CreateCompatibleDC.restype = ctypes.c_void_p
    g.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    g.CreateDIBSection.restype = ctypes.c_void_p
    g.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BITMAPINFO),
                                   ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.c_void_p, ctypes.c_uint32]
    g.SelectObject.restype = ctypes.c_void_p
    g.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    g.DeleteObject.argtypes = [ctypes.c_void_p]
    g.DeleteDC.argtypes = [ctypes.c_void_p]
    return u, g


_ULW_READY = None


def ulw_blit(hwnd, x, y, w, h, bgra):
    """把 32 位 BGRA（预乘 alpha）位图贴到分层窗口上，并设定位置/尺寸"""
    global _ULW_READY
    if _ULW_READY is None:
        _ULW_READY = _gdi_setup()
    u, g = _ULW_READY
    if not hwnd or w <= 0 or h <= 0:
        return
    try:
        hdc_screen = u.GetDC(None)
        hdc_mem = g.CreateCompatibleDC(hdc_screen)
        bi = _BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h        # 负数=自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0    # BI_RGB
        bits = ctypes.c_void_p()
        hbmp = g.CreateDIBSection(hdc_mem, ctypes.byref(bi), 0,
                                  ctypes.byref(bits), None, 0)
        ctypes.memmove(bits, bgra, min(len(bgra), w * h * 4))
        old = g.SelectObject(hdc_mem, hbmp)
        pt_dst = _POINT(int(x), int(y))
        size = _SIZE(int(w), int(h))
        pt_src = _POINT(0, 0)
        blend = _BLENDFUNC(0, 0, 255, 1)   # AC_SRC_ALPHA
        ok = u.UpdateLayeredWindow(hwnd, hdc_screen, ctypes.byref(pt_dst),
                                   ctypes.byref(size), hdc_mem, ctypes.byref(pt_src),
                                   0, ctypes.byref(blend), 2)   # ULW_ALPHA
        g.SelectObject(hdc_mem, old)
        g.DeleteObject(hbmp)
        g.DeleteDC(hdc_mem)
        u.ReleaseDC(None, hdc_screen)
        return bool(ok)
    except Exception:
        return False

LOCK_PATH = os.path.join(BASE_DIR, "stock_monitor.lock")  # 单实例守卫锁


def _my_parent_pid():
    """取父进程 PID（venv 的 pythonw 是 stub，真解释器是它的子进程，清理时要排除）"""
    try:
        k32 = ctypes.windll.kernel32
        TH32CS_SNAPPROCESS = 0x2

        class PROC(ctypes.Structure):
            _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                        ("th32ProcessID", ctypes.c_ulong),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                        ("th32ParentProcessID", ctypes.c_ulong),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong),
                        ("szExeFile", ctypes.c_char * 260)]
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        pe = PROC()
        pe.dwSize = ctypes.sizeof(PROC)
        me = os.getpid()
        parent = 0
        ok = k32.Process32First(snap, ctypes.byref(pe))
        while ok:
            if pe.th32ProcessID == me:
                parent = pe.th32ParentProcessID
                break
            ok = k32.Process32Next(snap, ctypes.byref(pe))
        k32.CloseHandle(snap)
        return parent
    except Exception:
        return 0


def _term_pid(k32, pid):
    try:
        h = k32.OpenProcess(0x0001, False, pid)
        if h:
            k32.TerminateProcess(h, 0)
            k32.CloseHandle(h)
    except Exception:
        pass


def kill_old_instance():
    """单实例守卫：结束上一个监控实例，避免两个透明窗口叠加出现字体重影。
    ① 锁文件里的旧 PID（校验是 python 解释器才动手）；
    ② 兜底：按命令行清掉还挂着本程序旧版本(v16 及更早无锁文件)的实例。
    排除自己与自己的父进程（venv stub）。"""
    old = 0
    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as f:
            old = int(f.read().strip() or 0)
    except Exception:
        old = 0
    k32 = ctypes.windll.kernel32
    if old and old != os.getpid():
        PROCESS_TERMINATE = 0x0001
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, old)
        if h:
            try:
                buf = ctypes.create_unicode_buffer(512)
                size = ctypes.c_ulong(512)
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    exe = (buf.value or "").replace("/", "\\").lower()
                    if exe.endswith("pythonw.exe") or exe.endswith("python.exe"):
                        k32.TerminateProcess(h, 0)
            except Exception:
                pass
            k32.CloseHandle(h)
    # 兜底：清理命令行里带"股票监控"的其它 python 实例（旧版本启动方式）
    try:
        keep = {os.getpid(), _my_parent_pid()}
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' "
             "-and $_.CommandLine -like '*股票监控*' -and $_.CommandLine -like '*app*.py*' } "
             "| Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=20).stdout or ""
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit() and int(line) not in keep:
                _term_pid(k32, int(line))
    except Exception:
        pass
    try:
        with open(LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

# 列定义: key, 表头, 宽度
COLS = [("code", "代码", 66), ("name", "名称", 100), ("price", "现价", 76),
        ("pct", "涨跌幅", 74), ("lb", "量比", 56), ("vol", "成交量", 80),
        ("high", "最高", 68), ("low", "最低", 68)]

DEFAULT_CONFIG = {
    "stocks": ["sh600519", "sz000001", "sh510300"],
    "refresh_interval": 3,
    "window_alpha": 0.85,
    "bg_color": "#1e222a",
    "transparent_bg": False,
    "bg_mode": "px",            # 背景模式: px=真·无背景(每像素透明) / translucent=半透明底 / solid=不透明
    "px_outline": 0,            # 文字描边档：0=无(最纤细·默认) 1=极细(1/3px) 2=细(1px) 3=粗(2px)
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
    "chart_pos": None,
    "chart_size": [900, 440],
    "auto_hide": 0,
    "click_open": False,
    "drag_catcher": True,
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
    # 背景模式归一化：旧配置没有 bg_mode 时按 transparent_bg 推导；key 统一升级为 px
    if cfg.get("bg_mode") not in ("px", "translucent", "solid"):
        cfg["bg_mode"] = "solid" if (cfg.get("bg_mode") == "solid") else (
            "px" if (cfg.get("bg_mode") == "key" or cfg.get("transparent_bg")) else "solid")
    cfg["transparent_bg"] = (cfg["bg_mode"] == "key")
    # v20：描边档位（老配置没有这一项时默认"无"，即最纤细）
    if not isinstance(cfg.get("px_outline"), int) or cfg["px_outline"] not in (0, 1, 2, 3):
        cfg["px_outline"] = 0
    # v20 首次升级：老配置字号偏小（11pt/22px 行高）在真透明下偏细，抬一档便于阅读。
    # 只做一次（用 v20_migrated 打标），否则用户之后把字号改小会被反复改回去。
    if not cfg.get("v20_migrated"):
        cfg["v20_migrated"] = True
        try:
            if int(cfg.get("font_size", 11)) <= 11:
                cfg["font_size"] = 12
            if int(cfg.get("row_height", 22)) <= 22:
                cfg["row_height"] = 24
        except Exception:
            cfg["font_size"], cfg["row_height"] = 12, 24
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def short_name(name):
    """长基金名压缩：'沪深300ETF华夏' → '沪深300ETF'、'科创50ETF联接' → '科创50ETF'。
    只在 ETF/LOF 之后还跟着 2 个字以上的公司名时才截断，个股名不受影响。"""
    s = (name or "").strip()
    if len(s) <= 7:
        return s
    up = s.upper()
    cut = -1
    for mark in ("ETF", "LOF"):
        i = up.rfind(mark)
        if i >= 0:
            cut = max(cut, i + len(mark))
    if cut > 0 and len(s) - cut >= 2 and cut >= 4:
        return s[:cut]
    return s


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


UA_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://gu.qq.com/",
    "Accept": "*/*",
}


def http_get(url, tries=3, timeout=6, encoding="utf-8"):
    """带重试的 GET。腾讯接口偶发超时/限流，多试几次成功率明显提高。"""
    last = None
    for i in range(max(1, tries)):
        try:
            req = urllib.request.Request(url, headers=UA_HEADERS)
            return urllib.request.urlopen(req, timeout=timeout).read().decode(encoding, "ignore")
        except Exception as ex:      # noqa: BLE001
            last = ex
            time.sleep(0.5 * (i + 1))
    raise last


def http_json(url, tries=3, timeout=6):
    return json.loads(http_get(url, tries=tries, timeout=timeout))


def fetch_quotes(codes):
    """腾讯实时行情。返回 {code: dict}"""
    if not codes:
        return {}
    url = "https://qt.gtimg.cn/q=" + ",".join(codes)
    raw = http_get(url, tries=3, encoding="gbk")
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
    """腾讯分时数据。返回 (name, prev_close, [(time, price, cumvol), ...])
    注意：必须用 ifzq.gtimg.cn，带 web. 前缀的域名会被腾讯 WAF 拦截返回 501。"""
    url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?code={code}"
    data = http_json(url, tries=3)
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
        url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},{period},,,120,qfq"
    data = http_json(url, tries=3)
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
    """生成默认托盘图标：深色圆角底 + 白色上升曲线（4 倍抗锯齿，低调不扎眼）"""
    ss = 4
    img = Image.new("RGBA", (64 * ss, 64 * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2 * ss, 2 * ss, 62 * ss, 62 * ss], radius=14 * ss,
                        fill=(24, 28, 36, 255))
    pts = [(10, 44), (20, 38), (28, 42), (38, 28), (46, 22)]
    pts = [(x * ss, y * ss) for x, y in pts]
    d.line(pts, fill=(235, 240, 245, 255), width=3 * ss, joint="curve")
    x, y = pts[-1]
    d.polygon([(x - 2 * ss, y - 6 * ss), (x + 6 * ss, y - 2 * ss), (x - 1 * ss, y + 1 * ss)],
              fill=(235, 240, 245, 255))
    img.resize((64, 64), Image.LANCZOS).save(DEFAULT_TRAY)


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
    MIN_W, MIN_H = 470, 240     # 缩放下限，避免标题栏按钮挤成一团

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
        self._err = ""
        self._draw_id = None
        # 位置/尺寸：记忆上次的，默认贴屏幕右侧
        pos = cfg.get("chart_pos") or [max(0, master.winfo_screenwidth() - 960), 120]
        size = cfg.get("chart_size") or [900, 440]
        self._x, self._y = int(pos[0]), int(pos[1])
        self.cw = max(self.MIN_W, int(size[0]))
        self.chh = max(self.MIN_H, int(size[1]))
        # ---- 标题栏窗口（chrome，跟随窗口透明度） ----
        self.win_bar = tk.Toplevel(master)
        self.win_bar.overrideredirect(True)
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
        # ---- 画布窗口（data，跟随字体透明度） ----
        self.win_canvas = tk.Toplevel(master)
        self.win_canvas.overrideredirect(True)
        self.canvas = tk.Canvas(self.win_canvas, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        # 右下角缩放手柄（无边框窗口没有系统边框，用抓手代替）
        self.grip = tk.Label(self.win_canvas, text="◢", fg="#8a8f99", bd=0,
                             font=(self.font_family, 9), cursor="bottom_right_corner")
        self.grip.place(relx=1.0, rely=1.0, anchor="se", width=15, height=15)
        self.grip.bind("<Button-1>", self._resize_start)
        self.grip.bind("<B1-Motion>", self._resize_move)
        self.grip.bind("<ButtonRelease-1>", self._save_geom)
        # ---- 拖动：标题栏与图表区域按住左键都能整体拖动 ----
        for wdg in (self.win_bar, self.bar, self.bar_lbl,
                    self.win_canvas, self.canvas):
            wdg.bind("<Button-1>", self._drag_start)
            wdg.bind("<B1-Motion>", self._drag_move)
            wdg.bind("<ButtonRelease-1>", self._save_geom)
        # ---- 事件：滚轮/中键只隐藏自己；右键菜单；Esc 关闭 ----
        targets = (self.win_bar, self.bar, self.bar_lbl, self.trans_btn,
                   self.win_canvas, self.canvas)
        for w in targets:
            w.bind("<Button-2>", lambda e: self.withdraw())
            w.bind("<Double-Button-2>", lambda e: self.withdraw())
            w.bind("<MouseWheel>", lambda e: self.withdraw())
            w.bind("<Button-3>", self._popup_menu)
        # 尺寸变化时重绘（缩放拖动中做 60ms 节流，避免每像素重画一次）
        self.win_canvas.bind("<Configure>", lambda e: self._schedule_draw())
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
        self.cmenu.add_separator()
        self.cmenu.add_command(label="重置大小(900x440)", command=self.reset_size)
        self.cmenu.add_command(label="关闭", command=self.destroy)
        self.apply_bg()
        self._apply_geometry()
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

    # ---- 布局 / 拖动 / 缩放 ----
    def _apply_geometry(self):
        """标题栏窗口与画布窗口保持同宽、上下紧贴"""
        self.win_bar.geometry("%dx%d+%d+%d" % (self.cw, self.BAR_H, self._x, self._y))
        self.win_canvas.geometry("%dx%d+%d+%d" %
                                 (self.cw, self.chh, self._x, self._y + self.BAR_H))

    def _schedule_draw(self):
        """缩放拖动时按 60ms 节流重绘"""
        try:
            if self._draw_id:
                self.win_canvas.after_cancel(self._draw_id)
        except Exception:
            pass
        try:
            self._draw_id = self.win_canvas.after(60, self.draw)
        except Exception:
            self._draw_id = None

    def _drag_start(self, e):
        # 用屏幕绝对坐标算偏移，窗口跟着指针走不会抖
        self._dx = e.x_root - self._x
        self._dy = e.y_root - self._y

    def _drag_move(self, e):
        self._x = e.x_root - self._dx
        self._y = e.y_root - self._dy
        self._apply_geometry()

    def _resize_start(self, e):
        self._rw, self._rh = self.cw, self.chh
        self._rx, self._ry = e.x_root, e.y_root
        return "break"

    def _resize_move(self, e):
        self.cw = max(self.MIN_W, self._rw + (e.x_root - self._rx))
        self.chh = max(self.MIN_H, self._rh + (e.y_root - self._ry))
        self._apply_geometry()
        return "break"

    def _save_geom(self, e=None):
        self.cfg["chart_pos"] = [self._x, self._y]
        self.cfg["chart_size"] = [self.cw, self.chh]
        try:
            save_config(self.cfg)
        except Exception:
            pass

    def reset_size(self):
        self.cw, self.chh = 900, 440
        self._apply_geometry()
        self._save_geom()
        self._schedule_draw()

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
        try:
            self.grip.configure(bg=bg)
            self.grip.lift()
        except Exception:
            pass

    # ---- 切换股票（复用同一窗口） ----
    def set_stock(self, code, name_hint):
        self.code = code
        self.name = name_hint or code
        self.bar_lbl.configure(text=self.name)
        self.period_idx = 0
        self.period, self.period_label = self.KLINE_PERIODS[0]
        self.period_lbl.configure(text=self.period_label)
        self.rows, self.krows, self.prev, self._err = [], [], None, ""
        self.set_mode("minute")

    # ---- 数据 ----
    def refresh(self):
        def worker():
            err = ""
            try:
                if self.mode == "minute":
                    payload = ("minute", fetch_minute(self.code))
                else:
                    payload = ("kline", fetch_kline(self.code, self.period))
            except Exception as ex:
                err = str(ex)
                payload = (self.mode, None)
            try:
                self.win_canvas.after(0, lambda: self._apply(payload, err))
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()
        try:
            self.win_canvas.after(10000 if self.mode == "minute" else 60000, self.refresh)
        except Exception:
            pass

    def _apply(self, payload, err=""):
        self._err = err
        kind, data = payload
        if data is not None:
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
        left, right, top, bottom = 62, 66, 30, 30
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
            # 右侧：价格
            c.create_text(left + pw + 8, y, text="%.*f" % (unit, pv), fill=color,
                          anchor="w", font=(self.font_family, 9))
            # 左侧：相对昨收的涨跌幅
            if prev:
                pct = (pv / prev - 1) * 100
                c.create_text(left - 8, y, text="%+.2f%%" % pct, fill=color,
                              anchor="e", font=(self.font_family, 9))
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
            msg = "分时数据获取失败，10 秒后自动重试" if self._err else "暂无分时数据（可能停牌/未开盘）"
            c.create_text(w // 2, h // 2, text=msg, fill="#c8c8c8",
                          font=(self.font_family, 12))
            if self._err:
                c.create_text(w // 2, h // 2 + 24, text=self._err[:80], fill="#6a707a",
                              font=(self.font_family, 8))
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
            msg = "K线数据获取失败，60 秒后自动重试" if self._err else "暂无K线数据"
            c.create_text(w // 2, h // 2, text=msg,
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

        # 位置：self._pos 是唯一真相（隐藏/显示/重启都以此为准）
        pos = self.cfg.get("window_pos")
        if pos:
            self._pos = [int(pos[0]), int(pos[1])]
        else:
            self._pos = [self.root.winfo_screenwidth() - 580, 80]
        self._dw, self._dh = 300, 100       # 数据窗口尺寸，_layout_windows 里更新
        self.win_chrome = tk.Toplevel(self.root)  # 标题栏窗口：跟随窗口透明度
        self.win_data = tk.Toplevel(self.root)    # 数据窗口：跟随字体透明度
        for w in (self.win_chrome, self.win_data):
            w.overrideredirect(True)
        self.win_chrome.geometry("+%d+%d" % (self._pos[0], self._pos[1]))
        self.win_data.geometry("+%d+%d" % (self._pos[0], self._pos[1] + 30))

        self._build_ui()
        self._build_catch()
        self._build_px()
        self._bind_drag_widgets()
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
        self.root.after(800, self._px_render)   # 启动稳定后补一次贴图
        self.fetch_loop()

    # ---------- 幽灵点击层 ----------
    def _build_catch(self):
        """近乎全透明(alpha≈0.4%)但能接收鼠标的覆盖层。
        背景透明(颜色键)的像素在 Windows 里鼠标会穿透，导致空白处点不到；
        叠这一层后整个窗口矩形都能按住拖动、单击选中、双击看分时、右键出菜单。"""
        self.win_catch = tk.Toplevel(self.root)
        self.win_catch.overrideredirect(True)
        self.win_catch.configure(bg="#000000")
        try:
            self.win_catch.attributes("-alpha", 0.004)
        except Exception:
            pass
        self.on_catch = bool(self.cfg.get("drag_catcher", True))
        w = self.win_catch
        w.bind("<Button-1>", self._drag_start)
        w.bind("<B1-Motion>", self._drag_move)
        w.bind("<ButtonRelease-1>", self._drag_end)
        w.bind("<Double-Button-1>", self._catch_double)
        w.bind("<Button-3>", self._popup_menu)
        w.bind("<Button-2>", lambda e: self.hide_main())
        w.bind("<Double-Button-2>", lambda e: self.hide_main())
        w.bind("<MouseWheel>", lambda e: self.hide_main())
        w.bind("<Leave>", self._schedule_auto_hide)
        w.bind("<Enter>", self._cancel_auto_hide)

    def _catch_double(self, e):
        item = self._hit_row(e)
        if item:
            self.show_chart(item)
        return "break"

    # ---------- 每像素 alpha 数据层（真·无背景） ----------
    def _build_px(self):
        """创建一个空白分层窗口，内容全部由 Pillow 渲染后用 UpdateLayeredWindow 贴上去。
        tkinter 不支持每像素 alpha，但 Win32 支持 —— 这样背景能真正做到 alpha=0（全透明），
        文字又是 FreeType 灰度抗锯齿，不会出现颜色键的重影/断线。"""
        self._px_fonts = {}
        self._px_cache = None
        self._px_hwnd = 0
        self.win_px = tk.Toplevel(self.root)
        self.win_px.overrideredirect(True)
        self.win_px.configure(bg="#000000")
        self.win_px.withdraw()
        try:
            self.win_px.update_idletasks()
            hwnd = self._win_hwnd(self.win_px)
            ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, ex | 0x00080000)  # WS_EX_LAYERED
            self._px_hwnd = hwnd
        except Exception:
            self._px_hwnd = 0
        self.win_px.bind("<Expose>", lambda e: self._px_place())

    def _px_font(self, size_px, bold=False):
        key = (self.font_family, size_px, bold)
        f = self._px_fonts.get(key)
        if f is not None:
            return f
        cands = (FONT_FILES_BOLD if bold else []) + FONT_FILES.get(self.font_family, []) \
            + ["msyh.ttc", "simhei.ttf"]
        f = None
        for fn in cands:
            p = os.path.join(FONT_DIR, fn)
            if os.path.exists(p):
                try:
                    f = ImageFont.truetype(p, size_px)
                    break
                except Exception:
                    continue
        if f is None:
            try:
                f = ImageFont.load_default()
            except Exception:
                f = None
        self._px_fonts[key] = f
        return f

    @staticmethod
    def _rgba(hex_color, alpha):
        try:
            r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
        except Exception:
            r = g = b = 255
        return (r, g, b, max(0, min(255, int(alpha))))

    @staticmethod
    def _premultiply(img):
        """UpdateLayeredWindow 要求预乘 alpha：RGB 需乘上自己的 alpha"""
        r, g, b, a = img.split()
        return Image.merge("RGBA", (ImageChops.multiply(r, a),
                                    ImageChops.multiply(g, a),
                                    ImageChops.multiply(b, a), a))

    @staticmethod
    def _clamp_premul(img):
        """缩放后把 RGB 压回 alpha 以内。
        LANCZOS 缩小有过冲（振铃），会留下 rgb > alpha 的像素，例如 (255,255,255,1)。
        Windows 按 out = rgb + bg*(1-a) 合成，这些像素就成了"加法叠加"：
        绿字在蓝底上冒出青边、白点、笔画断线——v20 初版在蓝色/彩色背景上"看不清"的真凶。"""
        r, g, b, a = img.split()
        return Image.merge("RGBA", (ImageChops.darker(r, a), ImageChops.darker(g, a),
                                    ImageChops.darker(b, a), a))

    def _px_metrics(self):
        """数据层尺寸：宽=可见列宽之和，高=表头(可选)+行数*行高"""
        c = self.cfg
        minimal = bool(c.get("minimal_mode", False))
        row_h = max(12, int(c.get("row_height", 22)))
        cols = [(k, h, wd) for k, h, wd in COLS if c["columns"].get(k, True)]
        head_h = 0 if minimal else row_h + 2
        pad = 2 if minimal else 16
        w = pad + sum(wd for _, _, wd in cols)
        h = head_h + row_h * max(1, len(c["stocks"]))
        return cols, row_h, head_h, pad, int(w), int(h)

    def _px_render(self):
        """重画数据层并贴到屏幕上（数据变化/样式变化/字号变化时调用）。
        v20 关键改进：
          1) 3 倍超采样渲染 → LANCZOS 缩回，笔画边缘干净，不会断线；
          2) 可选深色描边，白字落在白底/浅色文档上也看得清（原来的"看不见"主因）；
          3) 预乘 alpha 之后再缩放（先缩后乘会出黑边）。"""
        if self.cfg.get("bg_mode") != "px":
            return
        try:
            c = self.cfg
            cols, row_h, head_h, pad, w, h = self._px_metrics()
            S = PX_SS
            big = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
            d = ImageDraw.Draw(big)
            size_px = max(9, int(round(float(c.get("font_size", 12)) * 96 / 72)))
            font = self._px_font(size_px * S)
            if font is None:
                return
            ai = max(0, min(100, int(c.get("font_alpha", 100)))) / 100
            if ai <= 0:
                # 完全透明时别浪费：给个空图，避免残留上一帧
                self._px_cache = (w, h, b"\x00" * (w * h * 4))
                self._dw, self._dh = w, h
                self._px_place()
                return
            ow = int(c.get("px_outline", 0) or 0)     # 0无 1极细 2细 3粗
            ow = max(0, min(3, ow))
            # 小字号时 1px 描边会把笔画糊死，自动降为"极细"（≈0.33px 暗边）
            if size_px <= 14 and ow >= 2:
                ow = 1
            # 超采样空间里的 stroke：极细=1(缩回后≈0.33px，只压一点点暗边，不增粗笔画)
            stroke = 0 if ow == 0 else (1 if ow == 1 else (S if ow == 2 else 2 * S))
            o_alpha = 0.50 if ow == 1 else (0.62 if ow == 2 else 0.70)

            def put(x, y, text, fnt, col, alpha):
                kw = {}
                if stroke and alpha > 0:
                    kw["stroke_width"] = stroke
                    kw["stroke_fill"] = (0, 0, 0, int(min(230, alpha * o_alpha)))
                d.text((x * S, y * S), text, font=fnt,
                       fill=self._rgba(col, alpha), anchor="mm", **kw)

            # 表头
            if head_h:
                hfont = self._px_font(size_px * S, bold=True) or font
                x = pad
                for _, head, wd in cols:
                    put(x + wd / 2, head_h / 2, head, hfont, c["text_color"], 235 * ai)
                    x += wd
            # 数据行（值取自 Treeview，它现在只当数据模型）

            def fit(text, font_, maxw):
                """列宽不够时截断加省略号，避免文字压到相邻列"""
                if not text:
                    return text
                try:
                    lim = maxw * S
                    if d.textlength(text, font=font_) <= lim:
                        return text
                    while text and d.textlength(text + "…", font=font_) > lim:
                        text = text[:-1]
                    return text + "…"
                except Exception:
                    return text

            for i, code in enumerate(c["stocks"]):
                vals = self.tree.set(code)          # {列key: 值}，按列名取不会错位
                tags = self.tree.item(code, "tags") or ("flat",)
                col = {"up": c["up_color"], "down": c["down_color"]}.get(tags[0],
                                                                        c["flat_color"])
                y = head_h + i * row_h + row_h / 2
                x = pad
                for (k, _hd, wd) in cols:
                    put(x + wd / 2, y, fit(str(vals.get(k, "--")), font, wd - 6),
                        font, col, 255 * ai)
                    x += wd
            # 预乘 → 缩放（顺序不能反，否则透明像素的黑色会渗进笔画边缘）
            pm = self._premultiply(big).resize((w, h), Image.LANCZOS)
            pm = self._clamp_premul(pm)      # 必须：否则 LANCZOS 过冲 → 彩边/白点/断线
            data = pm.tobytes("raw", "BGRA")
            self._px_cache = (w, h, data)
            self._dw, self._dh = w, h
            self._px_err = ""
            self._px_place()
        except Exception as ex:
            self._px_err = "%s: %s" % (type(ex).__name__, ex)

    def _px_place(self):
        """只挪位置/重贴缓存，不重画（拖动、显示时用）"""
        if self.cfg.get("bg_mode") != "px" or not self._px_cache or not self._px_hwnd:
            return
        try:
            w, h, data = self._px_cache
            dy = 30 if (self.cfg.get("show_bar", True)
                        and not self.cfg.get("minimal_mode", False)) else 0
            ulw_blit(self._px_hwnd, self._pos[0], self._pos[1] + dy, w, h, data)
        except Exception:
            pass

    def _px_set_visible(self, visible):
        try:
            if visible and self.cfg.get("bg_mode") == "px":
                self.win_px.deiconify()
                self.win_px.lift()
                self._px_place()
            else:
                self.win_px.withdraw()
        except Exception:
            pass

    def _hit_row(self, e):
        """幽灵层/画布上的点击 → 对应的股票代码"""
        if self.cfg.get("bg_mode") == "px":
            cols, row_h, head_h, pad, w, h = self._px_metrics()
            i = int((e.y - head_h) // row_h)
            codes = self.cfg["stocks"]
            if 0 <= i < len(codes):
                return codes[i]
            return None
        return self.tree.identify_row(e.y) or None

    def set_catch(self, on):
        self.on_catch = bool(on)
        self.cfg["drag_catcher"] = self.on_catch
        save_config(self.cfg)
        if self.on_catch:
            self._sync_catch()
        else:
            try:
                self.win_catch.withdraw()
            except Exception:
                pass

    def _sync_catch(self):
        """让幽灵层始终精确盖在数据窗口上（隐藏状态下不显示）"""
        try:
            if not (self.on_catch and not self._hidden):
                self.win_catch.withdraw()
                return
            dy = 30 if (self.cfg.get("show_bar", True)
                        and not self.cfg.get("minimal_mode", False)) else 0
            self.win_catch.geometry("%dx%d+%d+%d" %
                                    (self._dw, self._dh, self._pos[0], self._pos[1] + dy))
            self.win_catch.deiconify()
            self.win_catch.lift()
        except Exception:
            pass

    def _dlg_guard(self, dlg):
        """弹窗期间暂时撤掉幽灵层，否则它会挡在对话框上方抢点击"""
        try:
            self.win_catch.withdraw()
        except Exception:
            pass

        def _restore(e=None):
            if getattr(e, "widget", None) is not dlg:
                return
            try:
                self.root.after(60, self._sync_catch)
            except Exception:
                pass
        try:
            dlg.bind("<Destroy>", _restore, add="+")
        except Exception:
            pass

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
        return TRANSPARENT_BLEND_BASE if self.cfg.get("bg_mode") == "key" else self.cfg["bg_color"]

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
        bg_mode:
          key         全透明：颜色键抠掉背景，只留文字/线条（字体边缘有毛边，ClearType 所致）
          translucent 半透明底：深色底+整窗透明，文字按正常 ClearType 渲染，最清晰（推荐）
          solid       不透明：实底最清晰"""
        c = self.cfg
        wa = float(c.get("window_alpha", 1.0))
        fa = c.get("font_alpha", 100) / 100
        self._apply_alpha(self.win_chrome, wa)
        mode = c.get("bg_mode", "key")
        if mode == "key":
            data_key, data_alpha = MAGIC, fa
        elif mode == "translucent":
            data_key, data_alpha = None, fa
        else:
            data_key, data_alpha = None, 1.0
        self._apply_alpha(self.win_data, data_alpha, data_key)
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
        """当前实际背景色（全透明键色模式用魔法色）"""
        return MAGIC if self.cfg.get("bg_mode") == "key" else self.cfg["bg_color"]

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
        btn_ks = dict(bg=bar_bg, fg=blend(c["text_color"], base, 0.75),
                      font=(ff, 10), bd=0, activebackground=bar_bg,
                      activeforeground=c["text_color"], cursor="hand2")
        tk.Label(self.bar, text="⚙", **btn_ks).pack(side=tk.RIGHT, padx=6)
        b_hide = tk.Label(self.bar, text="—", **btn_ks)
        b_hide.pack(side=tk.RIGHT, padx=6)
        b_hide.bind("<Button-1>", lambda e: self.hide_to_tray())
        self.win_chrome.nametowidget(str(self.bar.winfo_children()[-2])).bind(
            "<Button-1>", lambda e: self.open_settings())

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
        # 背景模式：全透明键色 / 半透明底（推荐，字体最清晰）/ 不透明
        self.bg_mode_var = tk.StringVar(value=c.get("bg_mode", "translucent"))
        bg_menu = tk.Menu(self.menu, tearoff=0, bg=c["bg_color"], fg=c["text_color"],
                          activebackground="#3a4150", activeforeground="#ffffff")
        for val, txt in (("px", "真·无背景（推荐·背景全透明+字体最清晰）"),
                         ("translucent", "半透明底"),
                         ("solid", "不透明")):
            bg_menu.add_radiobutton(label=txt, value=val, variable=self.bg_mode_var,
                                    command=lambda v=val: self.set_bg_mode(v))
        self.menu.add_cascade(label="背景模式", menu=bg_menu)
        # 文字描边：蓝底/彩色底上文字对比度不够时，加一档描边最有效
        self.ol_var = tk.IntVar(value=int(c.get("px_outline", 0) or 0))
        ol_menu = tk.Menu(self.menu, tearoff=0, bg=c["bg_color"], fg=c["text_color"],
                          activebackground="#3a4150", activeforeground="#ffffff")
        for val, txt in ((0, "无（最纤细）"), (1, "极细（几乎不增粗）"),
                         (2, "细（蓝底/白底推荐）"), (3, "粗")):
            ol_menu.add_radiobutton(label=txt, value=val, variable=self.ol_var,
                                    command=lambda v=val: self.set_outline(v))
        self.menu.add_cascade(label="文字描边", menu=ol_menu)
        self.ah_var = tk.BooleanVar(value=bool(int(c.get("auto_hide", 0) or 0)))
        self.menu.add_checkbutton(label="鼠标离开自动隐藏(10秒)", variable=self.ah_var,
                                  command=self.toggle_auto_hide)
        self.co_var = tk.BooleanVar(value=bool(c.get("click_open", False)))
        self.menu.add_checkbutton(label="左键单击直接打开分时图", variable=self.co_var,
                                  command=self.toggle_click_open)
        self.catch_var = tk.BooleanVar(value=bool(c.get("drag_catcher", True)))
        self.menu.add_checkbutton(label="整窗左键拖动（含透明区）", variable=self.catch_var,
                                  command=self.toggle_catch)
        self.menu.add_command(label="隐藏到托盘", command=self.hide_to_tray)
        self.menu.add_command(label="退出", command=self.quit_app)
        self._refresh_tree_rows(initial=True)

    def toggle_catch(self):
        self.set_catch(self.catch_var.get())

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

    def set_bg_mode(self, mode):
        """切换背景模式：px=真·无背景 / translucent=半透明底 / solid=不透明"""
        self.cfg["bg_mode"] = mode if mode in ("px", "translucent", "solid") else "px"
        self.cfg["transparent_bg"] = (self.cfg["bg_mode"] == "key")
        save_config(self.cfg)
        self._apply_style()

    def set_outline(self, level):
        """切换文字描边档：0无 1极细 2细 3粗（蓝底/白底上提高对比度最有效）"""
        self.cfg["px_outline"] = max(0, min(3, int(level)))
        save_config(self.cfg)
        if self.cfg.get("bg_mode") == "px":
            self._px_render()
        else:
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
        """左键单击选中该行（拖动窗口不会误触发打开分时图）"""
        item = self.tree.identify_row(e.y)
        self._click_item = item or None
        if item:
            self.tree.selection_set(item)
            self.tree.focus(item)

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
        """按 self._pos 摆放 标题栏窗口(30px)、数据层、幽灵点击层。
        位置一律取自 self._pos，绝不读隐藏窗口的 winfo_x —— 否则隐藏后重新显示会跑回默认位置。"""
        c = self.cfg
        minimal = c.get("minimal_mode", False)
        self.tree.configure(show="" if minimal else "headings")
        show_chrome = c.get("show_bar", True) and not minimal
        if c.get("bg_mode") == "px":
            # 真·无背景：数据区由 Pillow 渲染，Tk 的 Treeview 只当数据模型（隐藏）
            self.win_data.withdraw()
            self._px_render()
            self._px_set_visible(not self._hidden)
        else:
            self._px_set_visible(False)
            self.root.update_idletasks()
            h = self.tree.winfo_reqheight()
            if minimal:
                w = 2 + sum(wd for k, _, wd in COLS if c["columns"].get(k, True))
            else:
                w = 16 + sum(wd for k, _, wd in COLS if c["columns"].get(k, True))
            self._dw, self._dh = int(w), int(h)
            x, y = self._pos
            dy = 30 if show_chrome else 0
            self.win_data.geometry("%dx%d+%d+%d" % (self._dw, self._dh, x, y + dy))
            if not self._hidden:
                self.win_data.deiconify()
        x, y = self._pos
        if show_chrome and not self._hidden:
            self.win_chrome.geometry("%dx30+%d+%d" % (self._dw, x, y))
            if not self.win_chrome.winfo_ismapped():
                self.win_chrome.deiconify()
        else:
            self.win_chrome.withdraw()
        self._sync_catch()

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

    # ---------- 拖动（窗口内任意位置按住左键都能拖） ----------
    def _bind_drag_widgets(self):
        """数据表、标题栏、状态栏也支持"按住左键拖动整窗"（幽灵层关闭时的备用方案）。
        用 4px 阈值区分单击与拖动：单击照常选中股票，拖动时不会误开分时图。"""
        for w in (self.tree, self.bar, self.title_lbl, self.status):
            w.bind("<Button-1>", self._drag_start, add="+")
            w.bind("<B1-Motion>", self._drag_move, add="+")
            w.bind("<ButtonRelease-1>", self._drag_end, add="+")

    def _drag_start(self, e):
        self._dx = e.x_root - self._pos[0]
        self._dy = e.y_root - self._pos[1]
        self._press_xy = (e.x_root, e.y_root)
        self._drag_moved = False
        item = self._hit_row(e)
        self._click_item = item or None
        if item and e.widget is self.win_catch:
            self.tree.selection_set(item)
            self.tree.focus(item)

    def _drag_move(self, e):
        if not getattr(self, "_drag_moved", False):
            px, py = getattr(self, "_press_xy", (e.x_root, e.y_root))
            if abs(e.x_root - px) + abs(e.y_root - py) < 4:
                return
            self._drag_moved = True
        x = e.x_root - self._dx
        y = e.y_root - self._dy
        self._pos = [x, y]
        # 标题栏窗口 / 数据窗口 / 幽灵层一起移动
        show_chrome = self.cfg.get("show_bar", True) and not self.cfg.get("minimal_mode", False)
        dy = 30 if show_chrome else 0
        self.win_chrome.geometry("+%d+%d" % (x, y))
        self.win_data.geometry("+%d+%d" % (x, y + dy))
        if self.cfg.get("bg_mode") == "px":
            self._px_place()
        try:
            self.win_catch.geometry("%dx%d+%d+%d" % (self._dw, self._dh, x, y + dy))
            self.win_catch.lift()
        except Exception:
            pass
        self.cfg["window_pos"] = [x, y]

    def _drag_end(self, e=None):
        moved = getattr(self, "_drag_moved", False)
        self._drag_moved = False
        if moved:
            try:
                save_config(self.cfg)
            except Exception:
                pass
            self._click_item = None
            return "break"
        item = getattr(self, "_click_item", None)
        self._click_item = None
        if item and self.cfg.get("click_open"):
            self.show_chart(item)

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
                "name": short_name(q["name"]) or "--",
                "price": "%.2f" % q["price"] if q["price"] is not None else "--",
                "pct": "%+.2f%%" % pct if pct is not None else "--",
                "lb": "%.2f" % q["liangbi"] if q["liangbi"] is not None else "--",
                "vol": fmt_vol(q["vol"]),
                "high": "%.2f" % q["high"] if q["high"] is not None else "--",
                "low": "%.2f" % q["low"] if q["low"] is not None else "--",
            }
            self.tree.item(code, values=[vals[k] for k, _, _ in COLS], tags=(tag,))
        if self.cfg.get("bg_mode") == "px":
            self._px_render()
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
        self._dlg_guard(dlg)
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
        if self._hidden:
            self.show_all()
        else:
            self.hide_to_tray()

    def show_all(self):
        self._hidden = False
        self._apply_topmost()
        show_chrome = self.cfg.get("show_bar", True) and not self.cfg.get("minimal_mode", False)
        if show_chrome:
            self.win_chrome.deiconify()
        if self.cfg.get("bg_mode") != "px":
            self.win_data.deiconify()
        self._layout_windows()      # 按 self._pos 复原位置
        self._sync_catch()
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
        self._px_set_visible(False)
        try:
            self.win_catch.withdraw()
        except Exception:
            pass
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
        self._px_set_visible(False)
        try:
            self.win_catch.withdraw()
        except Exception:
            pass

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
        for w in (getattr(self, "win_chrome", None), getattr(self, "win_data", None),
                  getattr(self, "win_catch", None), getattr(self, "win_px", None)):
            if w is not None:
                try:
                    w.attributes("-topmost", t)
                except Exception:
                    pass

    def _preview_font_alpha(self, v):
        self.cfg["font_alpha"] = int(v)
        self._apply_tag_colors()
        if self.cfg.get("bg_mode") == "px":
            self._px_render()
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
        self._dlg_guard(win)
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
        # --- 背景模式（三档，独立于背景色选择，避免被"透明勾选"覆盖掉真无背景） ---
        MODE2TXT = {"px": "真·无背景", "translucent": "半透明底", "solid": "不透明"}
        TXT2MODE = {v: k for k, v in MODE2TXT.items()}
        tk.Label(win, text="背景模式", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        mode_var = tk.StringVar(value=MODE2TXT.get(c.get("bg_mode", "px"), "真·无背景"))
        tk.OptionMenu(win, mode_var, "真·无背景", "半透明底", "不透明").grid(
            row=r, column=1, columnspan=2, padx=(0, 8), pady=3, sticky="w")
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
        # --- 文字描边（真无背景时保证浅色/白色底上也看得清） ---
        tk.Label(win, text="文字描边", **LBL).grid(row=r, column=0, padx=(12, 4), pady=3, sticky="e")
        OL2TXT = {0: "无(最纤细)", 1: "极细", 2: "细", 3: "粗"}
        TXT2OL = {v: k for k, v in OL2TXT.items()}
        ol_var = tk.StringVar(value=OL2TXT.get(int(c.get("px_outline", 0)), "无(最纤细)"))
        tk.OptionMenu(win, ol_var, "无(最纤细)", "极细", "细", "粗").grid(
            row=r, column=1, columnspan=2, padx=(0, 8), pady=3, sticky="w")
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
            # 背景模式只由"背景模式"下拉决定；原来的写法会被背景色色块的"透明"勾选覆盖成 key，
            # 于是每次保存设置都会把"真·无背景"打回颜色键模式 → 重影/断线又回来了（v20 修复）
            c["bg_mode"] = TXT2MODE.get(mode_var.get(), "px")
            c["transparent_bg"] = (c["bg_mode"] == "key")
            c["px_outline"] = TXT2OL.get(ol_var.get(), 0)
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
            os.remove(LOCK_PATH)
        except Exception:
            pass
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
    kill_old_instance()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
