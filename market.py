# -*- coding: utf-8 -*-
"""market.py — MapleChat 外部资料入口（只开窗口，不抓取任何数据）。

设计
    * 主界面「外部资料」只保留两个胶囊按钮：怪物图鉴 / 市场行情；
    * 点击后以 Edge 应用窗口（--app=）打开对应站点，无地址栏、无标签页；
    * 本模块不含任何行情拉取、缓存、曲线绘制逻辑。

合规红线
    maplestory.watch 与 meso.watch 都明文禁止抓取与再发布，本模块对这两站
    只做浏览器跳转，绝不发起任何数据请求、不缓存、不解析其内容。
"""

import os
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

import tkinter as tk


# ---- 跳转入口（只开窗口，不抓取对方任何数据）----
GUIDE_URL = "https://mscw-guidebook.com/"
MARKET_URL = "https://maplestory.watch/market"
APP_WIN_SIZE = (1200, 820)            # Edge 应用窗口尺寸（宽, 高）

LINKS = [("怪物图鉴", GUIDE_URL), ("市场行情", MARKET_URL)]


def ui_scale(root=None):
    """设备像素 / 逻辑像素 的比例（96 DPI 下为 1.0），用于让尺寸在缩放屏上保持一致。"""
    try:
        r = root if root is not None else tk._default_root
        if r is None:
            return 1.0
        return max(1.0, float(r.tk.call("tk", "scaling")) / (96.0 / 72.0))
    except Exception:
        return 1.0


def px(n, root=None):
    """逻辑像素 -> 当前 DPI 下的设备像素。"""
    return int(round(float(n) * ui_scale(root)))


# ============================================================
# Edge 应用窗口（只打开，不抓取）
# ============================================================
def find_edge():
    """探测 msedge.exe 绝对路径；找不到返回 None。

    顺序：注册表 App Paths（HKCU/HKLM）→ 常见安装目录（Program Files (x86) /
    Program Files / LOCALAPPDATA）。
    """
    cands = []
    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, key) as k:
                    p = str(winreg.QueryValueEx(k, "")[0] or "").strip().strip('"')
                if p:
                    cands.append(Path(p))
            except Exception:
                continue
    except Exception:
        pass
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    for p in cands:
        try:
            if p.is_file():
                return p
        except Exception:
            continue
    return None


def build_edge_args(exe=None, url=None, size=None):
    """拼 Edge 应用窗口启动参数：--app=<url> 即无地址栏、无标签页的独立窗口。"""
    url = url or GUIDE_URL
    size = size or APP_WIN_SIZE
    w, h = size
    return [str(exe) if exe else "msedge.exe",
            "--app=%s" % url,
            "--window-size=%d,%d" % (w, h),
            "--new-window"]


def _win_snapshot():
    """枚举当前可见顶层窗口句柄集合，用于识别"新打开的窗口"。失败返回空集。"""
    if os.name != "nt":
        return set()
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        handles = set()
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            try:
                if user32.IsWindowVisible(hwnd):
                    handles.add(int(hwnd))
            except Exception:
                pass
            return True

        user32.EnumWindows(proto(_cb), 0)
        return handles
    except Exception:
        return set()


def fit_edge_window(before, size=None, timeout=8.0):
    """把新出现的 Edge 应用窗口校正到目标尺寸（按设备无关像素，系统自动换算）。

    Edge 复用已在运行的浏览器进程时会忽略 --window-size，这里在窗口出现后
    调一次 SetWindowPos；只调整窗口大小与位置，不读取、不解析页面内容。
    返回是否校正成功。
    """
    size = size or APP_WIN_SIZE
    if os.name != "nt" or not before:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    except Exception:
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.4)
        found = []

        def _cb(hwnd, _lparam):
            try:
                hw = int(hwnd)
                if hw in before or not user32.IsWindowVisible(hwnd):
                    return True
                if user32.GetWindowTextLengthW(hwnd) <= 0:
                    return True
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                if (r.right - r.left) > 500 and (r.bottom - r.top) > 400:
                    found.append(hw)
            except Exception:
                pass
            return True

        user32.EnumWindows(proto(_cb), 0)
        for hw in found:
            try:
                user32.SetWindowPos(hw, 0, 100, 60, int(size[0]), int(size[1]),
                                    0x0004 | 0x0010 | 0x0040)  # NOZORDER|NOACTIVATE|SHOWWINDOW
                return True
            except Exception:
                continue
    return False


def open_app_window(url, size=None):
    """用 Edge 应用窗口打开指定地址；Edge 缺失时退回默认浏览器新标签。

    返回 (kind, detail)，kind 为 "edge" / "browser"。不抓取目标站任何数据。
    """
    size = size or APP_WIN_SIZE
    exe = find_edge()
    if exe is not None:
        before = _win_snapshot()
        subprocess.Popen(build_edge_args(exe, url, size),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
        # Edge 复用既有浏览器进程时会忽略 --window-size，新窗口出现后就地校正一次
        if before:
            threading.Thread(target=fit_edge_window, args=(before, size),
                             daemon=True).start()
        return "edge", str(exe)
    if webbrowser is not None:
        webbrowser.open_new_tab(url)
        return "browser", ""
    raise RuntimeError("未找到 Edge，默认浏览器也不可用")


def selftest(verbose=True):
    """自检：只校验常量与参数构造，不启动浏览器、不发任何网络请求。"""
    ok = True
    msgs = []

    def chk(name, cond, extra=""):
        nonlocal ok
        if not cond:
            ok = False
        msgs.append(("[ok]  " if cond else "[FAIL] ") + name +
                    (("  " + str(extra)) if extra else ""))

    chk("怪物图鉴地址为 mscw-guidebook.com",
        GUIDE_URL.startswith("https://mscw-guidebook.com"), GUIDE_URL)
    chk("市场行情地址为 maplestory.watch/market",
        MARKET_URL == "https://maplestory.watch/market", MARKET_URL)
    chk("两个外链入口齐全",
        [n for n, _u in LINKS] == ["怪物图鉴", "市场行情"], LINKS)
    args = build_edge_args("msedge.exe", MARKET_URL, (1000, 700))
    chk("Edge 应用窗口参数正确",
        args[0] == "msedge.exe" and "--app=%s" % MARKET_URL in args
        and "--window-size=1000,700" in args, args)
    chk("行情拉取逻辑已移除",
        not any(hasattr(__import__(__name__), a) for a in
                ("fetch_trend", "parse_trend", "MarketCard", "TrendChart")), "")
    if verbose:
        for m in msgs:
            print("  " + m)
    return ok, msgs
