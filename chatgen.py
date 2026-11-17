# -*- coding: utf-8 -*-
"""
chatgen.py — MapleChat 快捷组句器（场景化聊天生成）

把 mscw-guidebook.com 翻译角里的「聊天生成器」能力内建进 MapleChat：
8 个聊天场景 × 逐场景动作按钮 × 少量字段 → 本地模板直接拼出地道英文单行句，
零 API 调用、零延迟、不花钱，复制即可回游戏 Ctrl+V。

设计原则
- 引擎（build / auto_en）纯逻辑无 UI，可单独自测；
- 目标字段支持中文自动转英文（复用 terms.json 的反向索引）；
- 生成即复制，回车即发。
"""

import re
import tkinter as tk
from collections import OrderedDict, deque

# ---------------- 场景与动作 ----------------
# (key, 中文名)
SCENES = [
    ("trade", "交易"),
    ("party", "组队"),
    ("leech", "带练"),
    ("pq", "组队任务"),
    ("location", "位置频道"),
    ("skill", "技能沟通"),
    ("server", "服务器"),
    ("custom", "自定义"),
]

# mode -> [(action_key, 按钮文案)]
ACTIONS = {
    "trade": [
        ("buy", "B> 买"),
        ("sell", "S> 卖"),
        ("trade", "T> 换"),
        ("lf", "LF> 收/找"),
        ("pc", "PC> 问价"),
        ("offer", "Offer 报价"),
    ],
    "party": [
        ("lfp", "LFP 求组"),
        ("recruit", "R> 招人"),
        ("join", "J> 加入"),
        ("need", "Need 需要"),
        ("full", "Full 队满"),
        ("ready", "Ready 好了"),
    ],
    "leech": [
        ("buyingLeech", "买带练"),
        ("sellingLeech", "卖带练"),
        ("leechSeller", "找带练卖家"),
        ("leechBuyer", "找买家"),
    ],
    "pq": [
        ("lfp", "LFP 求组"),
        ("recruit", "R> 招人"),
        ("need", "Need 需要"),
        ("hosting", "Hosting 开队"),
        ("join", "Join 加入"),
        ("ready", "Ready 准备"),
    ],
    "location": [
        ("askChannel", "问频道"),
        ("tellChannel", "告诉频道"),
        ("askLocation", "问位置"),
        ("tellLocation", "告诉位置"),
        ("comeHere", "叫人过来"),
        ("arrived", "我来了"),
    ],
    "skill": [
        ("need", "Need 需要"),
        ("canUse", "你能放吗"),
        ("have", "我有"),
        ("noSkill", "没技能"),
        ("cooldown", "冷却中"),
    ],
    "server": [
        ("dc", "掉线"),
        ("lag", "延迟"),
        ("maintenance", "维护"),
        ("backUp", "恢复了"),
        ("down", "关了"),
        ("noEta", "不知道时间"),
        ("reset", "重启"),
        ("stuck", "卡住"),
    ],
    "custom": [
        ("B>", "B> 买"),
        ("S>", "S> 卖"),
        ("T>", "T> 换"),
        ("LF>", "LF> 收"),
        ("LFP", "LFP 求组"),
        ("R>", "R> 招人"),
        ("Need", "Need 需要"),
        ("PC>", "PC> 问价"),
        ("Offer", "Offer 报价"),
        ("free", "无前缀"),
    ],
}

ACTION_CN = {
    "buy": "买", "sell": "卖", "trade": "换", "lf": "找", "pc": "问价", "offer": "报价",
    "lfp": "求组", "recruit": "招人", "join": "加入", "need": "需要", "full": "队满",
    "ready": "准备好了", "buyingLeech": "买带练", "sellingLeech": "卖带练",
    "leechSeller": "找带练卖家", "leechBuyer": "找带练买家", "hosting": "开队",
    "askChannel": "问频道", "tellChannel": "告诉频道", "askLocation": "问位置",
    "tellLocation": "告诉位置", "comeHere": "叫别人过来", "arrived": "我来了",
    "canUse": "询问能否使用", "have": "我有", "noSkill": "没有技能", "cooldown": "冷却中",
    "dc": "掉线", "lag": "延迟", "maintenance": "维护", "backUp": "服务器恢复",
    "down": "服务器关闭", "noEta": "不知道时间", "reset": "重启", "stuck": "卡住了",
}

# 每个场景要显示的输入框：key -> (标签, 提示)
FIELDS = {
    "trade": [
        ("target", "目标", "Chinese or English: 工地手套 / work glove"),
        ("extra", "价格/数量/补充", "500k / 10m / cc1 / 2 slots / nego"),
    ],
    "party": [
        ("target", "地图/怪物/职业", "kerning pq / lv50+ / cleric"),
        ("extra", "补充", "cc1 / 2 slots / fast run"),
    ],
    "leech": [
        ("target", "地图/怪物", "sg2 / hb3 / wolf spider"),
        ("slots", "人数/位置", "例如 2 slots / cc3"),
        ("extra", "补充", "10m / 1h / afk"),
    ],
    "pq": [
        ("target", "任务/Boss", "ludi pq / zakum"),
        ("role", "需要的职业/技能", "HB / HS / cleric"),
        ("extra", "补充", "cc1 / lv50+ / 2 slots"),
    ],
    "location": [
        ("target", "地图", "henesys / kerning / orbi"),
        ("channel", "频道", "例如 cc1"),
        ("extra", "补充", "at the shop / wait up"),
    ],
    "skill": [
        ("target", "技能", "HB / HS / haste / bless"),
        ("extra", "补充", "plz / ty"),
    ],
    "server": [
        ("extra", "补充", "again / so laggy / no ETA"),
    ],
    "custom": [
        ("target", "目标", "work glove / zakum"),
        ("extra", "补充", "500k / cc1 / pm me"),
    ],
}

# 常用词一键追加（游戏里高频，省打字）
CHIPS = ["500k", "10m", "cc1", "2 slots", "fast", "nego", "lv50+", "clean", "pm me", "ty hf"]

# 这些动作不需要填目标（服务器 / 队满 / 好了 / 纯自定义 / 问频道等固定句）
NO_TARGET = {
    ("server", None),
    ("party", "full"), ("party", "ready"),
    ("custom", None),
    ("trade", "offer"),
    ("location", "askChannel"), ("location", "tellChannel"),
    ("location", "askLocation"), ("location", "arrived"),
}

_CN_RE = re.compile(r"[\u4e00-\u9fff]")


def _k(s):
    """压空格、去首尾。"""
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _q(parts):
    """拼句子：丢空段、压空格、去掉标点前多余空格。"""
    s = " ".join([_k(p) for p in parts if _k(p)])
    return re.sub(r"\s+([?!,.])", r"\1", s).strip()


def build(mode, action, target="", role="", channel="", slots="", extra=""):
    """本地模板组句。返回 {english, warning, valid}。"""
    t = _k(target)
    r = _k(role)
    ch = _k(channel)
    sl = _k(slots)
    x = _k(extra)
    en = ""
    warn = None

    if mode == "trade":
        pre = {"buy": "B>", "sell": "S>", "trade": "T>", "lf": "LF>", "pc": "PC>"}.get(action)
        if action == "offer":
            en = _q(["offer", x or t])
        elif action == "pc":
            en = _q([pre, t])
        else:
            en = _q([pre, t, x])

    elif mode == "party":
        if action == "lfp":
            en = _q(["LFP", t, x])
        elif action == "recruit":
            en = _q(["R>", t, "party", x])
        elif action == "join":
            en = _q(["J>", t, x])
        elif action == "need":
            en = _q(["Need", t, x])
        elif action == "full":
            en = "Full"
        elif action == "ready":
            en = "Ready"

    elif mode == "leech":
        m = {
            "buyingLeech": "Buying leech",
            "sellingLeech": "Selling leech",
            "leechSeller": "LF> leech",
            "leechBuyer": "R> leech buyer",
        }.get(action)
        if m:
            en = _q([m, t, sl, x])

    elif mode == "pq":
        if action == "lfp":
            en = _q(["LFP", t, x])
        elif action == "recruit":
            en = _q(["R>", t, r, x])
        elif action == "need":
            en = _q(["Need", r or t, ("for " + t) if (t and r) else "", x])
        elif action == "hosting":
            en = _q(["Hosting", t, x])
        elif action == "join":
            en = ("Can I join %s?" % t) if t else "Can I join?"
        elif action == "ready":
            en = ("Ready for %s" % t) if t else "Ready"

    elif mode == "location":
        if action == "askChannel":
            en = "What cc are you in?"
        elif action == "tellChannel":
            en = _q(["I am in", ch])
        elif action == "askLocation":
            en = "Where are you?"
        elif action == "tellLocation":
            en = _q(["I am at", t, ch, x])
        elif action == "comeHere":
            en = _q(["Come to", t, ch, x])
        elif action == "arrived":
            en = _q(["I am here", t, ch, x])

    elif mode == "skill":
        if action == "need":
            en = _q(["Need", t, x])
        elif action == "canUse":
            en = _q(["Can you use", t, x]) + "?"
            en = re.sub(r"\s+\?", "?", en)
        elif action == "have":
            en = _q(["I have", t])
        elif action == "noSkill":
            en = _q(["No", t])
        elif action == "cooldown":
            en = _q([t, "cooldown"])

    elif mode == "server":
        base = {
            "dc": "DC", "lag": "Lagging", "maintenance": "Server check",
            "backUp": "Server is back up", "down": "Server is down",
            "noEta": "No ETA", "reset": "Server reset", "stuck": "Stuck",
        }.get(action, "")
        en = _q([base, x])

    elif mode == "custom":
        pre = "" if action == "free" else _k(action)
        en = _q([pre, t, x])

    en = _k(en)

    # 校验：该动作需要目标，但一个都没填
    need_target = (mode, action) not in NO_TARGET and (mode, None) not in NO_TARGET
    filled = bool(t or r or x or ch or sl)
    if not filled and not en:
        warn = "先点动作、填个目标或补充内容。"
    elif need_target and not (t or r) and mode not in ("server", "custom"):
        warn = "这个动作用来问/说具体对象，请填目标（中文会自动转英文）。"
    elif need_target and not (t or r) and mode in ("server", "custom"):
        warn = None

    valid = bool(en) and (warn is None)
    return {"english": en, "warning": warn, "valid": valid}


def zh_preview(mode, action, target="", role="", channel="", slots="", extra=""):
    """中文回显，方便确认自己組的是什么意思。"""
    parts = [ACTION_CN.get(action, action)]
    for label, v in (("目标", target), ("职业/技能", role), ("人数/位置", slots),
                     ("频道", channel), ("补充", extra)):
        if _k(v):
            parts.append("%s：%s" % (label, _k(v)))
    return "，".join(parts)


# ---------------- 中文 → 英文（复用术语库反向索引） ----------------
# 高频物品/术语补充（中文 -> 英文常用写法），优先于 terms.json 的反向候选
EXTRA_REV = {
    "枫叶短刀": "maple claw",
    "枫叶杖": "maple staff",
    "枫叶剑": "maple sword",
    "工地手套": "work glove",
    "扎昆头盔": "zakum helmet",
    "龙之项链": "horntail necklace",
    "粉色披风": "pink cape",
    "骷髅头盔": "skull helmet",
    "混沌卷轴": "chaos scroll",
    "白板": "clean",
    "攻击力": "wa",
    "魔攻": "ma",
    "血量": "hp",
    "蓝量": "mp",
    "等级": "lvl",
    "金币": "mesos",
    "点券": "nx",
    "名声": "fame",
    "公会": "guild",
    "好友": "buddy",
    "私聊": "pm me",
    "挂机": "afk",
    "马上回来": "brb",
    "多少钱": "how much",
    "求带": "lf leech",
}


def build_rev(flat):
    """flat: 英文小写 -> 中文。返回 {中文: [英文候选...]}（按缩写优先排序）。"""
    rev = {}
    for en, zh in (flat or {}).items():
        for piece in re.split(r"[、,，/;；]", str(zh)):
            p = piece.strip()
            if not p or not _CN_RE.search(p):
                continue
            rev.setdefault(p, []).append(en)
    for cn, en in EXTRA_REV.items():
        lst = rev.setdefault(cn, [])
        if en not in lst:
            lst.insert(0, en)
    for p, lst in rev.items():
        lst.sort(key=lambda s: (0 if re.fullmatch(r"[a-z]{2,5}", s) else 1, len(s)))
    return rev


def auto_en(text, rev, max_replace=3):
    """
    把中文目标转成英文：
    1) 整串命中术语 → 直接取英文；
    2) 否则按「最长中文词条」逐个替换（最多 max_replace 个）。
    返回 (新文本, 未完全转换的残留中文片段列表)
    """
    s = _k(text)
    if not s or not _CN_RE.search(s):
        return s, []
    if rev.get(s):
        return rev[s][0], []

    out = s
    used = []
    keys = sorted(rev.keys(), key=len, reverse=True)
    for k in keys:
        if len(used) >= max_replace:
            break
        if k and k in out:
            out = out.replace(k, rev[k][0], 1)
            used.append(k)
    left = _CN_RE.findall(out)
    return _k(out), left


# ---------------- 快捷窗 UI ----------------
BG = "#0d0d12"
BG2 = "#171a21"
FG = "#eef1f6"
FG_DIM = "#7f8a9a"
ACC = "#00e0a0"
ACC_DARK = "#04120c"


class QuickGen:
    """场景化组句窗口：点场景 → 点动作 → 填一两个词 → 回车即复制。"""

    def __init__(self, master, flat, setclip, on_log=None):
        self.rev = build_rev(flat)
        self.setclip = setclip
        self.on_log = on_log
        self.mode = "trade"
        self.action = "buy"
        self.fields = {}
        self.ph = {}
        self.ph_on = {}
        self.chips = []
        self.history = deque(maxlen=12)
        self.custom_pre = ""

        self.win = tk.Toplevel(master)
        self.win.title("MapleChat 快捷组句")
        self.win.configure(bg=BG)
        self.win.attributes("-topmost", True)
        self.win.geometry("520x600")

        tk.Label(self.win, text="快捷组句  点场景 / 点动作 / 填一两个词，回车即复制",
                 bg=BG, fg=FG_DIM, font=("Microsoft YaHei", 8), anchor="w").pack(fill="x", padx=10, pady=(8, 2))

        self.scene_row = tk.Frame(self.win, bg=BG)
        self.scene_row.pack(fill="x", padx=8)
        self.scene_btns = {}
        for i, (k, name) in enumerate(SCENES):
            b = tk.Button(self.scene_row, text=name, bd=0, relief="flat", padx=8, pady=3,
                          font=("Microsoft YaHei", 9), cursor="hand2",
                          command=lambda kk=k: self.set_mode(kk))
            b.grid(row=i // 4, column=i % 4, sticky="ew", padx=2, pady=2)
            self.scene_btns[k] = b
        for c in range(4):
            self.scene_row.grid_columnconfigure(c, weight=1)

        self.action_row = tk.Frame(self.win, bg=BG)
        self.action_row.pack(fill="x", padx=8, pady=(6, 0))

        self.fld_box = tk.Frame(self.win, bg=BG)
        self.fld_box.pack(fill="x", padx=10, pady=(6, 0))

        tk.Label(self.win, text="常用词（点一下追加到补充）",
                 bg=BG, fg=FG_DIM, font=("Microsoft YaHei", 8), anchor="w").pack(fill="x", padx=10, pady=(6, 0))
        self.chip_row = tk.Frame(self.win, bg=BG)
        self.chip_row.pack(fill="x", padx=8)

        tk.Frame(self.win, bg="#242833", height=1).pack(fill="x", padx=10, pady=8)

        self.out_var = tk.StringVar(value="")
        self.out = tk.Label(self.win, textvariable=self.out_var, bg=BG2, fg=ACC,
                            font=("Consolas", 13), anchor="w", justify="left",
                            wraplength=480, padx=10, pady=8)
        self.out.pack(fill="x", padx=10)
        self.warn_var = tk.StringVar(value="")
        self.warn = tk.Label(self.win, textvariable=self.warn_var, bg=BG, fg="#ff8f6b",
                             font=("Microsoft YaHei", 8), anchor="w", justify="left", wraplength=490)
        self.warn.pack(fill="x", padx=10, pady=(4, 0))

        brow = tk.Frame(self.win, bg=BG)
        brow.pack(fill="x", padx=10, pady=8)
        tk.Button(brow, text="生成并复制 (Enter)", command=self.gen,
                  bg=ACC, fg=ACC_DARK, bd=0, relief="flat", padx=12, pady=5,
                  font=("Microsoft YaHei", 9, "bold"), cursor="hand2").pack(side="left")
        tk.Button(brow, text="只生成", command=lambda: self.gen(copy=False),
                  bg=BG2, fg=FG, bd=0, relief="flat", padx=10, pady=5,
                  font=("Microsoft YaHei", 9), cursor="hand2").pack(side="left", padx=6)
        tk.Button(brow, text="最近", command=self.show_history,
                  bg=BG2, fg=FG, bd=0, relief="flat", padx=10, pady=5,
                  font=("Microsoft YaHei", 9), cursor="hand2").pack(side="left")
        tk.Button(brow, text="清空", command=self.reset,
                  bg=BG2, fg=FG, bd=0, relief="flat", padx=10, pady=5,
                  font=("Microsoft YaHei", 9), cursor="hand2").pack(side="right")

        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self.win.bind("<Return>", lambda e: self.gen())

        self.set_mode("trade")

    # ---- 场景/动作 ----
    def _paint(self, btn, on):
        if on:
            btn.configure(bg=ACC, fg=ACC_DARK)
        else:
            btn.configure(bg=BG2, fg=FG)

    def set_mode(self, mode):
        self.mode = mode
        for k, b in self.scene_btns.items():
            self._paint(b, k == mode)
        for w in self.action_row.winfo_children():
            w.destroy()
        acts = ACTIONS.get(mode, [])
        if mode == "custom":
            acts = ACTIONS["custom"]
        self.action_btns = {}
        for i, (key, label) in enumerate(acts):
            b = tk.Button(self.action_row, text=label, bd=0, relief="flat", padx=8, pady=2,
                          font=("Microsoft YaHei", 9), cursor="hand2",
                          command=lambda kk=key: self.set_action(kk))
            b.grid(row=i // 4, column=i % 4, sticky="ew", padx=2, pady=2)
            self.action_btns[key] = b
        for c in range(4):
            self.action_row.grid_columnconfigure(c, weight=1)
        self.action = acts[0][0] if acts else ""
        self._paint_action()
        self._build_fields()
        self._build_chips()

    def _paint_action(self):
        for k, b in getattr(self, "action_btns", {}).items():
            self._paint(b, k == self.action)

    def set_action(self, key):
        self.action = key
        self._paint_action()
        self._build_fields()

    # ---- 字段 ----
    def _build_fields(self):
        for w in self.fld_box.winfo_children():
            w.destroy()
        self.fields = {}
        self.ph = {}
        self.ph_on = {}
        specs = FIELDS.get(self.mode, [])
        for idx, (key, label, ph) in enumerate(specs):
            row = tk.Frame(self.fld_box, bg=BG)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, bg=BG, fg=FG_DIM, width=12, anchor="w",
                     font=("Microsoft YaHei", 9)).pack(side="left")
            var = tk.StringVar()
            e = tk.Entry(row, textvariable=var, bg=BG2, fg=FG, insertbackground=ACC,
                         relief="flat", bd=0, font=("Microsoft YaHei", 10))
            e.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 2))
            e.bind("<Return>", lambda ev: (self.gen(), "break")[1])
            self.fields[key] = var
            if ph:
                self._placeholder(e, var, ph, key)
        first = None
        for key in ("target", "extra", "role", "channel"):
            if key in self.fields:
                first = self.fields[key]
                break
        self._focus_first = first

    def _placeholder(self, entry, var, text, key):
        self.ph[key] = text
        self.ph_on[key] = True
        var.set(text)
        entry.configure(fg="#4a5160")

        def on_in(_):
            if self.ph_on.get(key):
                self.ph_on[key] = False
                var.set("")
            entry.configure(fg=FG)

        def on_out(_):
            if not (var.get() or "").strip():
                self.ph_on[key] = True
                var.set(text)
                entry.configure(fg="#4a5160")

        entry.bind("<FocusIn>", on_in)
        entry.bind("<FocusOut>", on_out)

    def _val(self, key):
        v = self.fields.get(key)
        if v is None:
            return ""
        s = (v.get() or "").strip()
        if not s:
            return ""
        if self.ph_on.get(key) and s == self.ph.get(key):
            return ""
        return s

    # ---- 常用词 chips ----
    def _build_chips(self):
        for w in self.chip_row.winfo_children():
            w.destroy()
        for i, c in enumerate(CHIPS):
            b = tk.Button(self.chip_row, text=c, bd=0, relief="flat", padx=6, pady=1,
                          bg=BG2, fg=FG_DIM, font=("Microsoft YaHei", 8), cursor="hand2",
                          command=lambda cc=c: self.add_chip(cc))
            b.grid(row=i // 5, column=i % 5, sticky="ew", padx=2, pady=2)
        for c in range(5):
            self.chip_row.grid_columnconfigure(c, weight=1)

    def add_chip(self, text):
        key = "extra" if "extra" in self.fields else ("target" if "target" in self.fields else None)
        if key is None:
            return
        cur = self._val(key)
        if text in cur.split():
            return
        self.fields[key].set(_k(cur + " " + text))

    # ---- 生成 ----
    def _collect(self):
        vals = {k: self._val(k) for k in ("target", "role", "channel", "slots", "extra")}
        # 中文自动转英文
        lefts = []
        for k in ("target", "role"):
            if vals[k]:
                new, left = auto_en(vals[k], self.rev)
                if new != vals[k]:
                    vals[k] = new
                    if k in self.fields:
                        self.fields[k].set(new)
                lefts += left
        return vals, lefts

    def gen(self, copy=True):
        vals, lefts = self._collect()
        if self.mode == "custom" and self.action and self.action != "free":
            act = self.action
        else:
            act = self.action
        res = build(self.mode, act, **vals)
        en = res["english"]
        self.out_var.set(en or "—")
        msg = res["warning"] or ""
        if lefts:
            msg = (msg + "  " if msg else "") + "这些中文没有对应术语，建议手填英文：%s" % "、".join(dict.fromkeys(lefts))
        self.warn_var.set(msg)
        if en and copy:
            try:
                self.setclip(en)
                self.warn_var.set((msg + "  " if msg else "") + "已复制，回游戏 Ctrl+V")
            except Exception as ex:
                self.warn_var.set("复制失败：%s" % ex)
        if en:
            self.history.appendleft(en)
            if callable(self.on_log):
                self.on_log("快捷组句 -> " + en)
        return en

    def reset(self):
        for v in self.fields.values():
            v.set("")
        self.out_var.set("")
        self.warn_var.set("")

    def show_history(self):
        if not self.history:
            self.warn_var.set("还没有生成记录。")
            return
        m = tk.Menu(self.win, tearoff=0)
        for h in self.history:
            m.add_command(label=h[:60], command=lambda v=h: self._use(v))
        m.tk_popup(self.win.winfo_pointerx(), self.win.winfo_pointery())

    def _use(self, text):
        self.out_var.set(text)
        try:
            self.setclip(text)
            self.warn_var.set("已复制，回游戏 Ctrl+V")
        except Exception:
            pass


def selftest():
    """命令行自检：不依赖 tkinter。"""
    cases = [
        ("trade", "buy", dict(target="work glove", extra="500k")),
        ("trade", "pc", dict(target="maple claw")),
        ("party", "recruit", dict(target="kerning pq", extra="cc1")),
        ("party", "full", {}),
        ("pq", "need", dict(target="ludi pq", role="cleric")),
        ("leech", "buyingLeech", dict(target="sg2", slots="2 slots", extra="10m")),
        ("location", "tellChannel", dict(channel="cc3")),
        ("location", "arrived", {}),
        ("skill", "canUse", dict(target="HB")),
        ("server", "lag", dict(extra="so laggy")),
        ("custom", "LFP", dict(target="zakum", extra="2 slots")),
    ]
    bad = 0
    for mode, act, kw in cases:
        r = build(mode, act, **kw)
        flag = "OK " if r["english"] else "ERR"
        if not r["english"]:
            bad += 1
        print("%s %-9s %-12s %s %s" % (flag, mode, act, r["english"], ("| " + r["warning"]) if r["warning"] else ""))

    # 中文自动转英文（术语库在场时）
    try:
        flat = {}
        from pathlib import Path
        import json
        import os
        import sys
        cand = Path(os.path.abspath(os.path.dirname(sys.argv[0]))) / "terms.json"
        if cand.exists():
            raw = json.loads(cand.read_text(encoding="utf-8"))
            for _, table in (raw.get("groups") or {}).items():
                for en, zh in table.items():
                    flat[en.strip().lower()] = zh.strip()
        rev = build_rev(flat)
        print("[rev] %d 个中文词条" % len(rev))
        for t in ("工地手套", "枫叶短刀", "你好"):
            en, left = auto_en(t, rev)
            print("  %s -> %s  left=%s" % (t, en, left))
    except Exception as e:
        print("[rev] skip:", e)

    print("ALL PASS" if bad == 0 else "FAILED %d" % bad)
    return bad == 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        selftest()
