# -*- coding: utf-8 -*-
"""
ui_flat.py — 跨平台扁平按钮（解决 macOS 白块）

背景：
    macOS 的 Tk 使用原生 aqua 外观，tk.Button 的 bg / fg / activebackground
    等配色参数会被系统控件绘制直接忽略，按钮恒定渲染成系统白/灰色方块。
    Windows 上一切正常，所以问题只在 .app / dmg 包体里出现。

方案：
    用 tk.Frame + tk.Label 自绘按钮（Frame/Label 的配色在三大平台都生效），
    对外保持 tk.Button 的常用接口（text / bg / fg / command / state / padx /
    pady / font / cursor / configure / cget / invoke），可直接平替。
"""

import tkinter as tk

__all__ = ["FlatButton"]


def _shift(color, delta):
    """把 #rrggbb 按亮度偏移 delta，用于自动推导悬停色。失败则原样返回。"""
    try:
        c = color.lstrip("#")
        if len(c) != 6:
            return color
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        r = max(0, min(255, r + delta))
        g = max(0, min(255, g + delta))
        b = max(0, min(255, b + delta))
        return "#%02x%02x%02x" % (r, g, b)
    except Exception:
        return color


class FlatButton(tk.Frame):
    """tk.Button 的跨平台替代品：自带配色，禁用态/悬停态均自绘。"""

    # 允许出现在 configure() 里但只做记录、不影响外观的参数
    _IGNORED = {
        "bd", "borderwidth", "relief", "highlightthickness", "highlightbackground",
        "highlightcolor", "overrelax", "selectcolor", "offvalue", "onvalue",
        "troughcolor", "indicatoron", "default", "underline", "bitmap", "image",
        "compound", "activebackground", "activeforeground",
    }

    def __init__(self, master=None, cnf=None, **kw):
        opts = {}
        if isinstance(cnf, dict):
            opts.update(cnf)
        opts.update(kw)

        self._bg = opts.pop("bg", opts.pop("background", "#2a3040"))
        self._fg = opts.pop("fg", opts.pop("foreground", "#eef1f6"))
        self._active_bg = opts.pop("activebackground", None) or _shift(self._bg, 18)
        self._active_fg = opts.pop("activeforeground", None) or self._fg
        self._disabled_fg = opts.pop("disabledforeground", None) or "#6b7280"
        self._state = opts.pop("state", "normal")
        self._command = opts.pop("command", None)

        text = opts.pop("text", "")
        font = opts.pop("font", None) or ("Microsoft YaHei", 9)
        padx = int(opts.pop("padx", 8) or 0)
        pady = int(opts.pop("pady", 2) or 0)
        self._cursor = opts.pop("cursor", "hand2")
        width = opts.pop("width", None)      # 字符宽，转成 Label 的 width
        anchor = opts.pop("anchor", "center")

        for k in list(opts.keys()):
            opts.pop(k, None)                # bd / relief / highlightthickness 等一律丢弃

        super().__init__(master, bg=self._bg, bd=0, highlightthickness=0,
                         cursor=self._cursor if self._state != "disabled" else "")

        self._label = tk.Label(
            self, text=text, bg=self._bg, fg=self._fg, font=font, anchor=anchor,
            padx=padx, pady=pady,
        )
        if width:
            try:
                self._label.configure(width=int(width))
            except Exception:
                pass
        self._label.pack(fill="both", expand=True)

        for w in (self, self._label):
            w.bind("<Button-1>", self._on_press)
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
        self._label.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<ButtonRelease-1>", self._on_release)

        self._apply_state()

    # ---------- 事件 ----------
    def _paint(self, bg, fg):
        """直接上色，不改基准色（_bg/_fg）。"""
        try:
            super().configure(bg=bg)
            self._label.configure(bg=bg, fg=fg)
        except Exception:
            pass

    def _on_press(self, _e=None):
        if self._state == "disabled":
            return
        self._paint(self._active_bg, self._active_fg)

    def _on_release(self, _e=None):
        self._refresh()

    def _on_enter(self, _e=None):
        if self._state == "disabled":
            return
        self._paint(self._active_bg, self._active_fg)

    def _on_leave(self, _e=None):
        self._refresh()

    # ---------- 内部 ----------
    def _apply_state(self):
        try:
            if self._state == "disabled":
                self._label.configure(cursor="")
                self.configure(cursor="")
            else:
                self._label.configure(cursor=self._cursor)
                self.configure(cursor=self._cursor)
        except Exception:
            pass

    def _refresh(self):
        fg = self._disabled_fg if self._state == "disabled" else self._fg
        self._paint(self._bg, fg)
        self._apply_state()

    # ---------- 兼容 tk.Button 的公开接口 ----------
    def invoke(self):
        if self._state == "disabled":
            return None
        if callable(self._command):
            return self._command()
        return None

    def click(self):
        return self.invoke()

    def configure(self, cnf=None, **kw):
        opts = {}
        if isinstance(cnf, dict):
            opts.update(cnf)
        opts.update(kw)
        redraw = False
        for key, val in opts.items():
            if key in ("bg", "background"):
                self._bg = val
                if not opts.get("activebackground"):
                    self._active_bg = _shift(val, 18)
                redraw = True
            elif key in ("fg", "foreground"):
                self._fg = val
                redraw = True
            elif key == "text":
                self._label.configure(text=val)
            elif key == "font":
                self._label.configure(font=val)
            elif key == "state":
                self._state = val
                redraw = True
            elif key == "command":
                self._command = val
            elif key == "cursor":
                self._cursor = val
                redraw = True
            elif key in ("padx", "pady"):
                self._label.configure(**{key: int(val or 0)})
            elif key == "width":
                try:
                    self._label.configure(width=int(val))
                except Exception:
                    pass
            elif key == "anchor":
                self._label.configure(anchor=val)
            elif key == "activebackground":
                self._active_bg = val
            elif key == "activeforeground":
                self._active_fg = val
            elif key == "disabledforeground":
                self._disabled_fg = val
                redraw = True
            elif key in self._IGNORED:
                continue
            else:
                try:
                    self._label.configure(**{key: val})
                except Exception:
                    pass
        if redraw:
            self._refresh()
        return None

    config = configure

    def cget(self, key):
        if key in ("bg", "background"):
            return self._bg
        if key in ("fg", "foreground"):
            return self._fg
        if key == "text":
            return self._label.cget("text")
        if key == "state":
            return self._state
        if key == "command":
            return self._command
        return self._label.cget(key)

    def __getitem__(self, key):
        return self.cget(key)

    def __setitem__(self, key, val):
        self.configure(**{key: val})

