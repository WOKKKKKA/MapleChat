# -*- coding: utf-8 -*-
"""
MapleChat 跨平台适配层（Windows / macOS）

把原先散落在 main.py 里的 Win32 专有能力收敛到这里，main.py 只调用本模块：
    set_dpi_aware()            进程 DPI 感知（仅 Windows 需要）
    init_pixel_scale(root)     Retina 物理像素 / 逻辑点换算系数
    pixel_scale()              取换算系数
    get_foreground_title()     前台窗口标题（窗口过滤用）
    set_clipboard(text)        写系统剪贴板
    apply_floating(win)        悬浮窗 / 全屏遮罩的窗口样式
    FONT_UI / FONT_MONO        界面字体、等宽字体
    MOD_NAME                   热键修饰键显示名

设计原则：Windows 分支与改造前完全等价，保证 v0.5 行为零回归。
"""
import sys

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# ---------------- 字体 ----------------
# macOS 没有微软雅黑，用系统自带中文字体；Linux 兜底 Noto
FONT_UI = "Microsoft YaHei UI" if IS_WIN else ("PingFang SC" if IS_MAC else "Noto Sans CJK SC")
FONT_MONO = "Consolas" if IS_WIN else ("Menlo" if IS_MAC else "DejaVu Sans Mono")

# 热键修饰键（Windows 是 Alt，mac 键盘上叫 Option，同一个键）
MOD_NAME = "Ctrl+Alt" if IS_WIN else "Ctrl+Option"


# ---------------- DPI 感知 ----------------
def set_dpi_aware():
    """必须在 tkinter / mss 之前执行：让进程按物理像素工作，否则框选坐标与截屏错位。仅 Windows。"""
    if not IS_WIN:
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ---------------- 像素比（Retina） ----------------
_PIXEL_SCALE = 1.0


def init_pixel_scale(tk_root=None):
    """
    macOS Retina：mss 抓到的是物理像素（2x），而 Tk 的坐标是逻辑点。
    框选得到的坐标是逻辑点，送进 mss 前必须乘上该系数，否则只截到左上角一小块。
    """
    global _PIXEL_SCALE
    if not IS_MAC:
        _PIXEL_SCALE = 1.0
        return 1.0
    try:
        import mss
        m = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        # monitors[0] 是"所有显示器联合区域"，多屏时会翻倍；monitors[1] 才是主显示器
        try:
            pw = float(m.monitors[1]["width"])
        except Exception:
            pw = float(m.monitors[0]["width"])
        lw = float(tk_root.winfo_screenwidth()) if tk_root is not None else pw
        if lw <= 0:
            lw = pw
        ratio = pw / lw
        _PIXEL_SCALE = 2.0 if ratio > 1.5 else 1.0
        try:
            m.close()
        except Exception:
            pass
    except Exception:
        _PIXEL_SCALE = 1.0
    return _PIXEL_SCALE


def pixel_scale():
    return _PIXEL_SCALE


# ---------------- 前台窗口标题 ----------------
def get_foreground_title():
    if IS_WIN:
        import ctypes
        try:
            u = ctypes.windll.user32
            hwnd = u.GetForegroundWindow()
            n = u.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            return buf.value or ""
        except Exception:
            return ""
    if IS_MAC:
        try:
            import Quartz
            opts = (Quartz.kCGWindowListOptionOnScreenOnly
                    | Quartz.kCGWindowListExcludeDesktopElements)
            wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
            for w in wins:
                # kCGWindowLayer == 0 才是普通应用窗口
                if int(w.get("kCGWindowLayer", 1) or 0) != 0:
                    continue
                owner = str(w.get("kCGWindowOwnerName", "") or "")
                name = str(w.get("kCGWindowName", "") or "")
                return (owner + " " + name).strip()
            return ""
        except Exception:
            return ""
    return ""


# ---------------- 剪贴板 ----------------
def set_clipboard(text, tk_root=None):
    """写系统剪贴板。Windows 直写 CF_UNICODETEXT（程序非所有者也能粘），mac 走 pbcopy。"""
    text = text or ""
    if IS_WIN:
        ok = _set_clipboard_win(text)
        if ok:
            return True
    elif IS_MAC:
        if _set_clipboard_mac(text):
            return True
    # 兜底：Tk 剪贴板
    try:
        import tkinter as tk
        r = tk_root or getattr(tk, "_default_root", None)
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update_idletasks()
        return True
    except Exception:
        return False


def _set_clipboard_win(text):
    import ctypes
    try:
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        # 64 位下句柄必须声明为 64 位，否则 GlobalAlloc 返回值被截断成负数，GlobalLock 直接失败
        u.OpenClipboard.argtypes = [ctypes.c_void_p]
        u.OpenClipboard.restype = ctypes.c_bool
        u.EmptyClipboard.restype = ctypes.c_bool
        u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        u.SetClipboardData.restype = ctypes.c_void_p
        u.CloseClipboard.restype = ctypes.c_bool
        k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        k.GlobalAlloc.restype = ctypes.c_void_p
        k.GlobalLock.argtypes = [ctypes.c_void_p]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalUnlock.argtypes = [ctypes.c_void_p]
        k.GlobalUnlock.restype = ctypes.c_bool
        GMEM_MOVEABLE, CF_UNICODETEXT = 0x0002, 13
        if not u.OpenClipboard(None):
            raise OSError("OpenClipboard failed")
        try:
            u.EmptyClipboard()
            buf = ctypes.create_unicode_buffer(text)
            size = ctypes.sizeof(buf)
            h = k.GlobalAlloc(GMEM_MOVEABLE, size)
            if not h:
                raise OSError("GlobalAlloc failed")
            p = k.GlobalLock(h)
            if not p:
                raise OSError("GlobalLock failed")
            ctypes.memmove(p, buf, size)
            k.GlobalUnlock(h)
            if not u.SetClipboardData(CF_UNICODETEXT, h):
                raise OSError("SetClipboardData failed")
        finally:
            u.CloseClipboard()
        return True
    except Exception:
        return False


def _set_clipboard_mac(text):
    import subprocess
    try:
        p = subprocess.run(["/usr/bin/pbcopy"], input=text.encode("utf-8"),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return p.returncode == 0
    except Exception:
        return False


# ---------------- 窗口样式 ----------------
def apply_floating(win):
    """无边框浮窗。mac 上 overrideredirect 有焦点与绘制问题，改用原生 help 类型窗口。"""
    if IS_MAC:
        try:
            win.overrideredirect(False)
        except Exception:
            pass
        try:
            win.attributes("-type", "help")
            return "help"
        except Exception:
            pass
    win.overrideredirect(True)
    return "override"


def apply_picker(win):
    """框选遮罩层专用窗口样式：必须压在游戏窗口之上，且不随主窗口失活而隐藏。

    mac 上 help 类型属于面板类窗口，主窗失去焦点（用户去点游戏）时会被系统收起，
    表现为"点完框选再切回游戏，框选层/选择框就没了"。
    这里优先用无边框普通窗口（borderless，不会因失活隐藏），失败再退回 help。
    Windows 分支与 apply_floating 完全等价（行为零回归）。
    """
    if IS_MAC:
        try:
            win.overrideredirect(True)
            return "borderless"
        except Exception:
            pass
        try:
            win.attributes("-type", "help")
            return "help"
        except Exception:
            pass
    win.overrideredirect(True)
    return "override"
