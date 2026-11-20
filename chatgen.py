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
from ui_flat import FlatButton
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
# ---- 视觉常量：与主界面（main.py UI_*）完全一致，只用唯一强调色 #0A84FF ----
BG = "#1C1C1E"        # 窗口底（macOS 深色）
CARD = "#2C2C2E"      # 卡片
BG2 = "#3A3A3C"       # 输入框 / 胶囊
LINE = "#38383A"      # 分割线
FG = "#FFFFFF"        # 主文字
FG_DIM = "#8E8E93"    # 次文字
FG_PH = "#636366"     # 占位符
ACC = "#0A84FF"       # 唯一强调色
HOVER = "#48484A"     # 悬停
ERR = "#FF6B6B"       # 错误提示
FONT = "Microsoft YaHei UI"
SP = 8                  # 8pt 间距网格
PILL_H = 30             # 分类胶囊统一高度（等高对齐）
PILL_GAP = 14           # 第三轮：分类胶囊与候选列表的垂直间距（≥12px，避免压迫感）
ZH_COL = 168            # 第三轮：中文列固定列宽（首字起始 x 恒定，不随英文长短浮动）
ROW_HOVER = "#3A3A3C"   # 候选行悬停底色（三级背景，克制）

# 分类胶囊：全部 + 6 类（模板 5 类 + 术语）
CATS = [("全部", None), ("交易", "交易"), ("组队", "组队"), ("带练", "带练"),
        ("位置", "位置"), ("杂项", "杂项"), ("术语", "术语")]

_CAT_OF = {"trade": "交易", "party": "组队", "pq": "组队", "leech": "带练",
           "location": "位置", "skill": "杂项", "server": "杂项", "custom": "杂项"}

# 无参数调用即可形成可直接发送的完整句，才纳入候选
_FIXED = {
    ("party", "full"), ("party", "ready"),
    ("pq", "join"), ("pq", "ready"),
    ("location", "askChannel"), ("location", "askLocation"),
    ("skill", "canUse"),
    ("server", "dc"), ("server", "lag"), ("server", "maintenance"),
    ("server", "backUp"), ("server", "down"), ("server", "noEta"),
    ("server", "reset"), ("server", "stuck"),
}

# 无需参数即可直接发送的常用短句（补齐模板之外的交易 / 带练场景）
_EXTRA = [
    ("How much?", "多少钱 · 交易", "交易"),
    ("Offer?", "报个价？ · 交易", "交易"),
    ("Still selling?", "还在卖吗 · 交易", "交易"),
    ("Nego?", "可议价？ · 交易", "交易"),
    ("Deal", "成交 · 交易", "交易"),
    ("Sold", "已售出 · 交易", "交易"),
    ("Buying leech", "买带练 · 带练", "带练"),
    ("Selling leech", "卖带练 · 带练", "带练"),
    ("LF leech", "求带练 · 带练", "带练"),
    ("Leech pls", "求带，谢谢 · 带练", "带练"),
    ("How long?", "要多久？ · 带练", "带练"),
    ("TY", "谢谢 · 杂项", "杂项"),
    ("Nty", "不用了，谢谢 · 杂项", "杂项"),
    ("GL", "祝好运 · 杂项", "杂项"),
    ("AFK", "暂离一会儿 · 杂项", "杂项"),
]

MAX_ROWS = 200      # 单次渲染上限，保证输入时不卡


def build_flat(flat=None):
    """把 SCENES/ACTIONS 与术语库展开为扁平词条：[(英文短语, 中文, 分类)]。"""
    items, seen = [], set()
    for en, zh, cat in _EXTRA:
        k = en.lower()
        if k not in seen:
            seen.add(k)
            items.append((en, zh, cat))
    for mode, mode_cn in SCENES:
        for action, label in ACTIONS.get(mode, ()):
            if (mode, action) not in _FIXED:
                continue
            try:
                en = _k(build(mode, action).get("english"))
            except Exception:
                continue
            key = en.lower()
            if not en or key in seen:
                continue
            seen.add(key)
            zh = "%s · %s" % (ACTION_CN.get(action, label), mode_cn)
            items.append((en, zh, _CAT_OF.get(mode, "杂项")))
    for en, zh in sorted((flat or {}).items()):
        if not isinstance(en, str) or not isinstance(zh, str):
            continue
        k = en.strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        items.append((en.strip(), zh.strip(), "术语"))
    return items


def _roundify(box, fill, page, radius=12):
    """在直角容器四角贴背景色小画布，视觉上形成圆角（与主界面一致）。"""
    for relx, rely, anchor, oval in (
            (0.0, 0.0, "nw", (0, 0, 2 * radius, 2 * radius)),
            (1.0, 0.0, "ne", (-radius, 0, radius, 2 * radius)),
            (0.0, 1.0, "sw", (0, -radius, 2 * radius, radius)),
            (1.0, 1.0, "se", (-radius, -radius, radius, radius))):
        try:
            cv = tk.Canvas(box, width=radius, height=radius, bg=page,
                           highlightthickness=0, bd=0)
            cv.create_oval(*oval, fill=fill, outline="")
            cv.place(relx=relx, rely=rely, anchor=anchor)
        except Exception:
            pass


def _card(parent, px=11, py=9):
    """圆角卡片：返回 (外框, 内层容器)。"""
    box = tk.Frame(parent, bg=CARD)
    _roundify(box, CARD, BG, 12)
    inner = tk.Frame(box, bg=CARD)
    inner.pack(fill="x", padx=px, pady=py)
    return box, inner


class QuickGen:
    """极简组句窗：一个搜索框 + 一行分类 + 可滚动候选，点一下就复制。"""

    def __init__(self, master, flat, setclip, on_log=None):
        self.items = build_flat(flat)
        self.setclip = setclip
        self.on_log = on_log
        self.cat = None
        self._rows = []
        self._pills = {}

        self.win = tk.Toplevel(master)
        self.win.title("快捷组句")
        self.win.configure(bg=BG)
        self.win.geometry("440x580")
        self.win.minsize(380, 420)
        try:
            self.win.transient(master)
            self.win.lift()
            self.win.focus_force()
        except Exception:
            pass

        wrap = tk.Frame(self.win, bg=BG)
        wrap.pack(fill="both", expand=True, padx=14, pady=14)

        # —— 搜索框（圆角填充、无边框）——
        box = tk.Frame(wrap, bg=BG2, height=38)
        box.pack(fill="x")
        box.pack_propagate(False)
        _roundify(box, BG2, BG, 10)
        self.q = tk.StringVar()
        self.ent = tk.Entry(box, textvariable=self.q, bg=BG2, fg=FG, bd=0,
                            insertbackground=ACC, font=(FONT, 13),
                            highlightthickness=0, relief="flat")
        self.ent.pack(side="left", fill="both", expand=True, padx=(12, 10), pady=9)
        self.ph_lbl = tk.Label(box, text="输入英文或中文关键词", bg=BG2, fg=FG_PH,
                               font=(FONT, 13))
        self.ph_lbl.place(x=13, y=9)
        self.ph_lbl.bind("<Button-1>", lambda e: self.ent.focus_set())
        self.q.trace_add("write", lambda *a: self._on_type())

        # —— 分类胶囊 ——
        # 分类胶囊分两行（4 + 3），统一 8pt 间距、等高对齐，确保 440 宽窗口内不溢出
        rowf = tk.Frame(wrap, bg=BG, height=PILL_H)
        rowf.pack(fill="x", pady=(SP, 0))
        rowf.pack_propagate(False)
        for idx, (name, cat) in enumerate(CATS):
            if idx == 4:
                rowf = tk.Frame(wrap, bg=BG, height=PILL_H)
                rowf.pack(fill="x", pady=(SP, 0))
                rowf.pack_propagate(False)
            cv = self._make_pill(rowf, name, cat)
            cv.pack(side="left", padx=(0, SP), pady=0)
            self._pills[name] = cv

        # —— 候选列表 ——
        holder = tk.Frame(wrap, bg=CARD)
        holder.pack(fill="both", expand=True, pady=(PILL_GAP, 0))
        _roundify(holder, CARD, BG, 12)
        self.canvas = tk.Canvas(holder, bg=CARD, highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="both", expand=True, padx=(6, 2), pady=6)
        self.list_frame = tk.Frame(self.canvas, bg=CARD)
        self._item = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.canvas.bind("<Configure>", self._on_canvas_conf)
        self.list_frame.bind("<Configure>", self._on_list_conf)

        # —— 底部轻提示 ——
        self.hint = tk.Label(wrap, text="输入关键词筛选，单击任意行即可复制",
                             bg=BG, fg=FG_DIM, font=(FONT, 11), anchor="w")
        self.hint.pack(fill="x", pady=(10, 0))

        self.win.bind("<Return>", lambda e: self._copy_first())
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self.win.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.win.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

        self._refresh()
        try:
            self.ent.focus_set()
        except Exception:
            pass
        if self.on_log:
            try:
                self.on_log("快捷组句：载入 %d 条候选短语" % len(self.items))
            except Exception:
                pass

    # ---------- 胶囊分类 ----------
    def _make_pill(self, parent, name, cat):
        from tkinter import font as tkfont
        fnt = (FONT, 12)
        try:
            f = tkfont.Font(root=parent, font=fnt)
            tw = f.measure(name)
        except Exception:
            tw = len(name) * 12
        h = PILL_H
        w = max(PILL_H + 8, tw + 24)
        cv = tk.Canvas(parent, width=w, height=h, bg=BG, highlightthickness=0,
                       bd=0, cursor="hand2", takefocus=0)

        def draw(active, hover=False):
            if active:
                fill, fg, outline = ACC, "#FFFFFF", ""
            elif hover:
                fill, fg, outline = HOVER, "#FFFFFF", ""
            else:
                # 第三轮：胶囊是可操作项，未选中态用亮白文字（不用次级灰）
                fill, fg, outline = BG2, FG, ""
            cv.delete("all")
            r = h / 2.0
            cv.create_oval(0, 0, h, h, fill=fill, outline=outline)
            cv.create_oval(w - h, 0, w, h, fill=fill, outline=outline)
            cv.create_rectangle(r, 0, w - r, h, fill=fill, outline=outline)
            cv.create_text(w / 2.0, h / 2.0, text=name, fill=fg, font=fnt)

        cv._draw = draw
        cv._active = (cat == self.cat)
        draw(cv._active)
        cv.bind("<Enter>", lambda e: draw(cv._active, True))
        cv.bind("<Leave>", lambda e: draw(cv._active, False))
        cv.bind("<Button-1>", lambda e: self._set_cat(cat))
        return cv

    def _set_cat(self, cat):
        self.cat = cat
        for name, c in CATS:
            cv = self._pills.get(name)
            if cv is None:
                continue
            cv._active = (c == cat)
            try:
                cv._draw(cv._active)
            except Exception:
                pass
        self._refresh()

    # ---------- 列表 ----------
    def _on_canvas_conf(self, e):
        try:
            self.canvas.itemconfigure(self._item, width=e.width)
        except Exception:
            pass

    def _on_list_conf(self, e=None):
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except Exception:
            pass

    def _wheel(self, e):
        try:
            self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        except Exception:
            pass

    def _on_type(self):
        txt = self.q.get()
        try:
            if txt:
                self.ph_lbl.place_forget()
            else:
                self.ph_lbl.place(x=13, y=9)
        except Exception:
            pass
        self._refresh()

    def _match(self, kw):
        kw = (kw or "").strip().lower()
        out = []
        for en, zh, cat in self.items:
            if self.cat and cat != self.cat:
                continue
            if kw and kw not in en.lower() and kw not in zh.lower():
                continue
            out.append((en, zh))
            if len(out) >= MAX_ROWS:
                break
        return out

    def _refresh(self):
        for r in self._rows:
            try:
                r.destroy()
            except Exception:
                pass
        self._rows = []
        rows = self._match(self.q.get())
        if not rows:
            lb = tk.Label(self.list_frame, text="没有匹配的短语，换个关键词试试",
                          bg=CARD, fg=FG_DIM, font=(FONT, 11), anchor="w")
            lb.pack(fill="x", padx=12, pady=16)
            self._rows.append(lb)
        else:
            for en, zh in rows:
                self._rows.append(self._make_row(en, zh))
        self._on_list_conf()
        try:
            self.canvas.yview_moveto(0)
        except Exception:
            pass
        try:
            self.canvas.after(40, self._refit_rows)
        except Exception:
            pass

    def _refit_rows(self):
        """窗口尺寸稳定后重算一次省略号，避免首帧宽度未定时误截断。"""
        for r in list(self._rows):
            fn = getattr(r, "_fit", None)
            if fn:
                try:
                    fn()
                except Exception:
                    pass

    def _row_font(self, parent, fnt):
        from tkinter import font as tkfont
        key = ("_rowf", tuple(fnt))
        cache = getattr(self, "_fontcache", None)
        if cache is None:
            cache = {}
            self._fontcache = cache
        f = cache.get(key)
        if f is None:
            try:
                f = tkfont.Font(root=parent, font=fnt)
            except Exception:
                return None
            cache[key] = f
        return f

    def _fit_text(self, lbl, text, limit, fnt):
        """按可用像素宽度做省略号截断，绝不越过右边界。"""
        f = self._row_font(lbl, fnt)
        try:
            if f is None:
                lbl.configure(text=text)
                return
            if limit <= 10 or f.measure(text) <= limit:
                lbl.configure(text=text)
                return
            ell = "\u2026"
            ew = f.measure(ell)
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if f.measure(text[:mid]) + ew <= limit:
                    lo = mid
                else:
                    hi = mid - 1
            lbl.configure(text=(text[:lo] + ell) if lo > 0 else ell)
        except Exception:
            pass

    def _make_row(self, en, zh):
        f_en = (FONT, 13, "bold")
        f_zh = (FONT, 11)
        row = tk.Frame(self.list_frame, bg=CARD)
        row.pack(fill="x", padx=10, pady=1)
        row.columnconfigure(0, weight=1)
        # 第三轮：中文列为固定列宽 + 左对齐，首字起始 x 恒定，不随左侧英文长短浮动
        row.columnconfigure(1, weight=0, minsize=ZH_COL)
        right = tk.Label(row, text=zh, bg=CARD, fg=FG_DIM, font=f_zh,
                         anchor="w", justify="left")
        right.grid(row=0, column=1, sticky="w", padx=(12, 6), pady=4)
        left = tk.Label(row, text=en, bg=CARD, fg=FG, font=f_en,
                        anchor="w", justify="left")
        left.grid(row=0, column=0, sticky="ew", pady=4)

        def _fit(ev=None):
            try:
                avail = int(ev.width) if ev is not None else int(row.winfo_width())
            except Exception:
                return
            if avail <= 20:
                return
            inner = avail - 18               # 扣掉左右内距
            zh_cap = max(48, ZH_COL - 12)    # 中文列固定可用宽度（超出用省略号，不越界）
            self._fit_text(right, zh, zh_cap, f_zh)
            fzh = self._row_font(right, f_zh)
            try:
                zh_px = fzh.measure(right.cget("text")) if fzh else 0
            except Exception:
                zh_px = 0
            self._fit_text(left, en, max(48, inner - max(zh_px, zh_cap) - 18), f_en)

        row.bind("<Configure>", _fit)
        row._fit = _fit
        kids = (row, left, right)

        def paint(color):
            for w in kids:
                try:
                    w.configure(bg=color)
                except Exception:
                    pass

        for w in kids:
            w.configure(cursor="hand2")
            w.bind("<Enter>", lambda e: paint(ROW_HOVER))
            w.bind("<Leave>", lambda e: paint(CARD))
            w.bind("<Button-1>", lambda e, t=en: self._copy(t))
        return row

    # ---------- 复制 ----------
    def _copy(self, text):
        try:
            self.setclip(text)
            self.hint.configure(text="已复制：%s" % text[:28], fg=ACC)
        except Exception as ex:
            self.hint.configure(text="复制失败：%r" % (ex,), fg=ERR)
            if self.on_log:
                try:
                    self.on_log("快捷组句复制失败：%r" % (ex,))
                except Exception:
                    pass
            return
        if self.on_log:
            try:
                self.on_log("快捷组句：已复制 %s" % text)
            except Exception:
                pass

    def _copy_first(self):
        rows = self._match(self.q.get())
        if rows:
            self._copy(rows[0][0])


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
