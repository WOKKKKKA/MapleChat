"""
MapleChat · 数据看板模块（datawatch v2 · 数字识别版）
================================================
改版要点（v2）：
旧版靠"进度条像素比例"反推经验/HP/MP，碰上升级瞬间回绕、渐变条、满条、描边就会读错。
新版直接 OCR 读画面上的数字：
    HP(1298/1298)   MP(1087/1087)   EXP 97,550 [45.04%]   Lv. 37
有绝对经验值后，效率与升级时间就能算准：
    Δ经验 / Δ时间 = 每分钟经验  →  (本级总经验 - 当前经验) / 每分钟经验 = 剩余时间

设计原则：
1. 只读屏幕像素，不注入、不读内存
2. 多区域裁剪后竖排拼成一张图，一次 OCR 全部解析（一次 OCR ≈ 1 秒，省调用）
3. 解析用正则分级：精确前缀优先，兜底靠"排除 HP/MP/百分比后取最大数"
4. 纯逻辑模块，不依赖 main.py，不 import tkinter，可单独自测
5. 样本落 SQLite，供效率曲线与复盘
"""

import os
import re
import sqlite3
import threading
import time
from pathlib import Path

import cv2
import numpy as np


# ============================================================
# 默认配置（挂到主 config 的 "watch" 节点下）
# ============================================================
DEFAULT_WATCH = {
    "enabled": False,
    # 主用：一条能把 HP / MP / EXP 全框进去的横带（游戏底部状态条）
    "stat_region": None,
    # 可选：等级数字单独框（不框也能从同一条里解析出 Lv.）
    "level_region": None,
    # 兼容 v1 旧配置：三个区域任意填写，会与 stat_region 一起裁剪、合并解析
    "exp_region": None,
    "hp_region": None,
    "mp_region": None,
    "level": 1,                  # 手动兜底等级，OCR 读到就自动覆盖
    "interval_ms": 2000,         # 兼容旧配置：等价于 stat_interval_ms
    # v8 分层调度：状态条（HP/MP/EXP）与飘字区分开采样，把预算让给高频项
    "stat_interval_ms": 2000,    # 状态条间隔（含 EXP，2s 足够推算速度）
    "pop_interval_ms": 1000,     # 飘字间隔（飘字只活 2~3s，必须高频）
    "static_ttl_ms": 4000,       # 静态指标（等级）缓存 TTL，不单独框选则走状态条
    "only_when_foreground": True,
    "window_keyword": "",
    "keep_hours": 12,
}


# ============================================================
# 文本解析
# ============================================================
_RE_HP = re.compile(r"HP\s*[\[\(（【]?\s*([\d,\.]{1,12})\s*/\s*([\d,\.]{1,12})", re.I)
_RE_MP = re.compile(r"MP\s*[\[\(（【]?\s*([\d,\.]{1,12})\s*/\s*([\d,\.]{1,12})", re.I)
_RE_LV = re.compile(r"L\s*[VY][\s\.\:]*([\d]{1,3})", re.I)
_RE_PCT = re.compile(r"([\d]{1,3}(?:[.,][\d]{1,2})?)\s*%")
# 经验绝对值：前缀放宽（实测 EXP 会被读成 EXR / EKP）
_RE_EXP = re.compile(r"E[XRKPIF]\s*[\.\: ]?\s*([\d][\d,\.]{2,15})", re.I)
_RE_TOK = re.compile(r"\d[\d,\.]{2,15}")
_RE_DIGITS = re.compile(r"[^\d]")


def _to_int(s):
    """把 '97,550' / '97.550' / '1298' 归一成整数。"""
    if not s:
        return None
    d = _RE_DIGITS.sub("", str(s))
    if not d:
        return None
    try:
        return int(d)
    except Exception:
        return None


def _to_pct(s):
    if s is None: return None
    t = str(s).strip().replace(",", ".")
    try: v = float(t)
    except Exception: return None
    if 0.0 <= v <= 100.0: return v
    # OCR 常把左方括号 "[" 认成 "1"，使百分比多出一位：157.65 -> 57.65
    for k in (1, 2):
        if len(t) > k:
            try: v2 = float(t[k:])
            except Exception: continue
            if 0.0 <= v2 <= 100.0: return v2
    return None


def parse_stats(text):
    """
    从 OCR 文本里解析出各项读数，返回 dict：
      hp / hp_max / mp / mp_max / level / exp_abs / exp_pct
      缺失的键不出现（不会用 0 冒充）。
    """
    out = {}
    if not text:
        return out
    t = " " + str(text).replace("|", " ").replace("【", "[").replace("】", "]") + " "

    m = _RE_HP.search(t)
    if m:
        cur, mx = _to_int(m.group(1)), _to_int(m.group(2))
        if cur is not None and mx is not None and mx > 0:
            out["hp"], out["hp_max"] = cur, mx
    m = _RE_MP.search(t)
    if m:
        cur, mx = _to_int(m.group(1)), _to_int(m.group(2))
        if cur is not None and mx is not None and mx > 0:
            out["mp"], out["mp_max"] = cur, mx
    m = _RE_LV.search(t)
    if m:
        lv = _to_int(m.group(1))
        if lv and 1 <= lv <= 300:
            out["level"] = lv

    # 百分比：优先带方括号的（EXP 的显示格式），否则取任意 xx.xx%
    pct = None
    m = re.search(r"[\[\(（]\s*([\d]{1,3}(?:[.,][\d]{1,2})?)\s*%\s*[\]\)）]", t)
    if m:
        pct = _to_pct(m.group(1))
    else:
        m = _RE_PCT.search(t)
        if m:
            pct = _to_pct(m.group(1))
    if pct is not None and 0.0 <= pct <= 100.0:
        out["exp_pct"] = pct

    # 经验绝对值：先按 EXP 前缀抓，抓不到再排除法兜底
    exp_abs = None
    m = _RE_EXP.search(t)
    if m:
        v = _to_int(m.group(1))
        if v is not None and v >= 1:
            exp_abs = v
    if exp_abs is None:
        rest = t
        rest = _RE_HP.sub(" ", rest)
        rest = _RE_MP.sub(" ", rest)
        rest = re.sub(r"[\[\(（][^\]\)）]*%[^\]\)）]*[\]\)）]", " ", rest)
        rest = _RE_PCT.sub(" ", rest)
        rest = _RE_LV.sub(" ", rest)
        cands = [_to_int(x) for x in _RE_TOK.findall(rest)]
        cands = [c for c in cands if c is not None and c >= 1000]
        if cands:
            exp_abs = max(cands)
    if exp_abs is not None:
        out["exp_abs"] = exp_abs
    return out


# ============================================================
# OCR 引擎（懒加载，缺失时降级而非崩溃）
# ============================================================
_OCR_LOCK = threading.Lock()
_OCR_OBJ = [None, False]          # [实例, 是否已尝试]


def get_ocr():
    """返回 RapidOCR 实例；不可用时返回 None。"""
    with _OCR_LOCK:
        if _OCR_OBJ[1]:
            return _OCR_OBJ[0]
        _OCR_OBJ[1] = True
        try:
            from rapidocr_onnxruntime import RapidOCR
            _OCR_OBJ[0] = RapidOCR()
        except Exception:
            _OCR_OBJ[0] = None
        return _OCR_OBJ[0]


def stitch(imgs, gap=8):
    """把多张小图竖排拼成一张，供单次 OCR。宽度不足的用黑边补齐。"""
    imgs = [i for i in imgs if i is not None and getattr(i, "size", 0) > 0]
    if not imgs:
        return None
    if len(imgs) == 1:
        return imgs[0]
    w = max(i.shape[1] for i in imgs)
    rows = []
    for i in imgs:
        if i.shape[1] < w:
            i = cv2.copyMakeBorder(i, 0, 0, 0, w - i.shape[1],
                                   cv2.BORDER_CONSTANT, value=(0, 0, 0))
        rows.append(i)
        rows.append(np.zeros((gap, w, 3), np.uint8))
    return np.vstack(rows[:-1])


def _prep(img, scale=1.0, binar=False):
    """缩放 + 可选二值化，返回送进 OCR 的图。"""
    im = img
    if scale and abs(scale - 1.0) > 1e-3:
        im = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    if binar:
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY) if len(im.shape) == 3 else im.copy()
        g = cv2.GaussianBlur(g, (3, 3), 0)
        _, im = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return im


def ocr_raw(img, scale=1.0, ocr=None, binar=False):
    """OCR 后返回原始结果 [[box, text, score], ...]，失败返回 []。"""
    if img is None or getattr(img, "size", 0) == 0:
        return []
    eng = ocr if ocr is not None else get_ocr()
    if eng is None:
        return []
    try:
        res, _ = eng(_prep(img, scale, binar))
    except Exception:
        return []
    return res or []


def ocr_text(img, scale=1.0, ocr=None, binar=False):
    """对一张图做 OCR，返回拼接文本。失败返回空串。"""
    res = ocr_raw(img, scale=scale, ocr=ocr, binar=binar)
    if not res:
        return ""
    parts = []
    for item in res:
        try: parts.append(str(item[1]))
        except Exception: pass
    return " ".join(parts)


_STAT_TRIALS = ((1.0, False), (2.0, True), (3.0, True))


def read_stats(imgs, scale=1.0, ocr=None, retry=True):
    """
    对一组区域截图做 OCR 并解析，返回 (stats_dict, raw_text)。
    先按 scale 原样读；若关键字段（级别 / 经验百分比）缺失，再依次尝试放大 + 二值化。
    """
    img = stitch(imgs)
    if img is None or getattr(img, "size", 0) == 0:
        return {}, ""
    trials = [(scale, False)]
    if retry:
        trials += [t for t in _STAT_TRIALS if abs(t[0] - scale) > 1e-3]
    best_st, best_txt = {}, ""
    for sc, bi in trials:
        txt = ocr_text(img, scale=sc, ocr=ocr, binar=bi)
        st = parse_stats(txt)
        if (st.get("exp_pct") is not None) or (st.get("exp_abs") is not None) or len(st) >= 3:
            return st, txt
        if len(st) > len(best_st):
            best_st, best_txt = st, txt
    return best_st, best_txt


# ============================================================
# 飘字区：右侧 "You received EXP (+8)" / "You have gained mesos (+10)"
# ============================================================
_RE_POP_EXP = re.compile(
    r"(?:EXP|EXR|EPF|EHP|EhP|E\.P|E,P|E P|E[XRK]P)\s*[^\d\n]{0,10}?\+?\s*([\d][\d,]{0,8})",
    re.I)
_RE_POP_MESO = re.compile(
    r"(?:meso|me[sc]o|mes0|mesoi)[a-z0-9]*\s*[^\d\n]{0,12}?\+?\s*([\d][\d,]{0,8})",
    re.I)
_POP_SCALES = (1.5, 2.5, 3.0)


def parse_popups(text):
    """从飘字区文本里抽增量，返回 {"exp": int|None, "meso": int|None}。"""
    out = {"exp": None, "meso": None}
    if not text:
        return out
    t = str(text).replace("|", " \n ")
    exps, mesos = [], []
    for m in _RE_POP_EXP.finditer(t):
        v = _to_int(m.group(1))
        if v and 0 < v <= 999999:
            exps.append(v)
    for m in _RE_POP_MESO.finditer(t):
        v = _to_int(m.group(1))
        if v and 0 < v <= 9999999:
            mesos.append(v)
    if exps:
        out["exp"] = max(exps)
    if mesos:
        out["meso"] = max(mesos)
    return out


def _pop_from_res(res, scale):
    """把 OCR 原始框按纵坐标聚成行（同一条飘字的重复识别只算一行），再解析。"""
    out = {"exp": None, "meso": None, "raw": ""}
    rows = []
    for item in res:
        try:
            box, txt, score = item[0], str(item[1]), float(item[2])
        except Exception:
            continue
        try:
            y0 = min(p[1] for p in box) / max(scale, 1e-6)
        except Exception:
            y0 = 0.0
        rows.append((y0, -score, txt))
    if not rows:
        return out
    rows.sort()
    clusters, cur = [], []
    for y, nsc, txt in rows:
        if cur and abs(y - cur[-1][0]) > 14.0:
            clusters.append(cur)
            cur = []
        cur.append((y, nsc, txt))
    if cur:
        clusters.append(cur)
    texts = [" ".join(x[2] for x in c) for c in clusters]
    out["raw"] = " | ".join(texts)
    exps, mesos = [], []
    for line in texts:
        p = parse_popups(line)
        if p["exp"]:
            exps.append(p["exp"])
        if p["meso"]:
            mesos.append(p["meso"])
    if exps:
        out["exp"] = max(exps)
    if mesos:
        out["meso"] = max(mesos)
    return out


def read_popups(img, ocr=None, scale=None, q=None):
    """
    读飘字区，返回 {"exp": int|None, "meso": int|None, "raw": str}。
    未指定 scale 时按多档缩放依次尝试，取命中字段最多的那次结果。
    """
    empty = {"exp": None, "meso": None, "raw": ""}
    if img is None or getattr(img, "size", 0) == 0:
        return empty
    scales = (scale,) if scale else _POP_SCALES
    best = None
    for sc in scales:
        res = ocr_raw(img, scale=sc, ocr=ocr)
        if not res:
            continue
        r = _pop_from_res(res, sc)
        score = int(r["exp"] is not None) + int(r["meso"] is not None)
        if best is None or score > best[0]:
            best = (score, r)
        if score >= 2:
            break
    return best[1] if best else empty


def read_combined(stat_imgs, pop_img=None, ocr=None, scale=1.5):
    """
    一次 OCR 同时读状态条与飘字区：飘字在上、状态条在下，竖排拼接。
    返回 (stats, popups, raw_stat, raw_pop)。
    飘字区为空时等价于 read_stats。
    """
    st, pr = {}, {"exp": None, "meso": None, "raw": ""}
    stat_img = stitch(stat_imgs)
    if pop_img is None or getattr(pop_img, "size", 0) == 0 or stat_img is None:
        st, raw = read_stats(stat_imgs, ocr=ocr)
        return st, pr, raw, ""
    gap = 8
    big = stitch([pop_img, stat_img], gap=gap)
    res = ocr_raw(big, scale=scale, ocr=ocr)
    if not res:
        st, raw = read_stats(stat_imgs, ocr=ocr)
        return st, pr, raw, ""
    cut = pop_img.shape[0] * scale
    tops, bots = [], []
    for item in res:
        try:
            ys = [p[1] for p in item[0]]
            txt = str(item[1])
        except Exception:
            continue
        if (min(ys) + max(ys)) / 2.0 < cut:
            tops.append(txt)
        else:
            bots.append(txt)
    pr = parse_popups(" ".join(tops))
    pr["raw"] = " | ".join(tops)
    st = parse_stats(" ".join(bots))
    raw_stat = " ".join(bots)
    if st.get("exp_pct") is None and st.get("exp_abs") is None:
        st2, raw2 = read_stats(stat_imgs, ocr=ocr)
        if len(st2) > len(st):
            st, raw_stat = st2, raw2
    return st, pr, raw_stat, pr["raw"]


# ============================================================
# 样本存储
# ============================================================
# ============================================================
class WatchStore:
    """SQLite 样本库：时间 / 等级 / 经验百分比 / 经验绝对值 / HP / MP"""

    def __init__(self, path):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._lock = threading.Lock()
        self._init()

    def _conn(self):
        c = sqlite3.connect(str(self.path), timeout=5.0)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init(self):
        try:
            with self._conn() as c:
                c.execute("""CREATE TABLE IF NOT EXISTS samples(
                    ts REAL NOT NULL,
                    level INTEGER,
                    exp REAL,
                    hp REAL,
                    mp REAL
                )""")
                c.execute("CREATE INDEX IF NOT EXISTS idx_ts ON samples(ts)")
                # v2 追加列（旧库自动迁移，旧数据保留）
                cols = [r[1] for r in c.execute("PRAGMA table_info(samples)").fetchall()]
                for name, decl in (("exp_abs", "INTEGER"), ("hp_cur", "INTEGER"),
                                   ("hp_max", "INTEGER"), ("mp_cur", "INTEGER"),
                                   ("mp_max", "INTEGER")):
                    if name not in cols:
                        c.execute("ALTER TABLE samples ADD COLUMN %s %s" % (name, decl))
        except Exception:
            pass

    def add(self, ts, level, exp_pct=None, exp_abs=None,
            hp_cur=None, hp_max=None, mp_cur=None, mp_max=None):
        hp_pct = (hp_cur * 100.0 / hp_max) if (hp_cur is not None and hp_max) else None
        mp_pct = (mp_cur * 100.0 / mp_max) if (mp_cur is not None and mp_max) else None
        with self._lock:
            try:
                with self._conn() as c:
                    c.execute("""INSERT INTO samples
                        (ts,level,exp,hp,mp,exp_abs,hp_cur,hp_max,mp_cur,mp_max)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""",
                              (float(ts), int(level or 0), exp_pct, hp_pct, mp_pct,
                               exp_abs, hp_cur, hp_max, mp_cur, mp_max))
            except Exception:
                pass

    def recent(self, minutes=60):
        """返回 [(ts, level, exp_pct, exp_abs), ...] 按时间升序。"""
        cut = time.time() - minutes * 60.0
        with self._lock:
            try:
                with self._conn() as c:
                    return c.execute(
                        "SELECT ts,level,exp,exp_abs FROM samples "
                        "WHERE ts>=? ORDER BY ts ASC", (cut,)).fetchall()
            except Exception:
                return []

    def latest(self):
        with self._lock:
            try:
                with self._conn() as c:
                    return c.execute(
                        "SELECT ts,level,exp,exp_abs,hp_cur,hp_max,mp_cur,mp_max "
                        "FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
            except Exception:
                return None

    def purge(self, keep_hours=12):
        cut = time.time() - max(1, keep_hours) * 3600.0
        with self._lock:
            try:
                with self._conn() as c:
                    c.execute("DELETE FROM samples WHERE ts<?", (cut,))
            except Exception:
                pass

    def clear(self):
        with self._lock:
            try:
                with self._conn() as c:
                    c.execute("DELETE FROM samples")
            except Exception:
                pass


# ============================================================
# 效率与升级预测
# ============================================================
def _slope(xs, ys):
    """最小二乘斜率（单位：ys 每秒），比首尾差更抗噪。"""
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / float(n)
    my = sum(ys) / float(n)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 1e-9:
        return 0.0
    return num / den


def exp_rate(rows, level=None, window_min=30):
    """
    由样本计算经验效率与升级剩余时间。

    rows: [(ts, level, exp_pct, exp_abs), ...] 升序

    返回 dict：
      exp_per_min    每分钟获得经验（绝对值）
      exp_per_hour   每小时获得经验
      pct_per_min / pct_per_hour  百分比口径（无绝对值时的回退）
      total_exp      本级升级所需总经验（推算）
      remain_exp     距升级还差多少经验
      eta_min        预计还需多少分钟升级
      span_min       有效统计时长
      samples        参与统计样本数
      gained         本窗口内累计获得经验
      note           不可用时的人话原因
    """
    res = {"exp_per_min": None, "exp_per_hour": None,
           "pct_per_min": None, "pct_per_hour": None,
           "total_exp": None, "remain_exp": None, "eta_min": None,
           "span_min": 0.0, "samples": 0, "gained": None, "note": ""}
    if not rows:
        res["note"] = "还没有采样数据"
        return res

    cut = time.time() - window_min * 60.0
    seq = [r for r in rows if r[0] >= cut]
    if level is not None:
        seq = [r for r in seq if int(r[1] or 0) == int(level)]
    # 只保留最后一段"同等级连续"样本，跨级前的数据不参与
    if seq:
        tail = [seq[-1]]
        for r in reversed(seq[:-1]):
            if int(r[1] or 0) == int(tail[0][1] or 0):
                tail.append(r)
            else:
                break
        seq = list(reversed(tail))
    res["samples"] = len(seq)
    if len(seq) < 2:
        res["note"] = "样本不足，再挂一会儿（至少 2 条）"
        return res

    ts = [r[0] for r in seq]
    span = (ts[-1] - ts[0]) / 60.0
    res["span_min"] = span
    if span < 0.5:
        res["note"] = "统计时长不足 30 秒"
        return res

    pcts = [r[2] for r in seq]
    absv = [r[3] for r in seq]
    has_abs = all(v is not None for v in absv)
    has_pct = all(v is not None for v in pcts)

    cur_pct = pcts[-1] if has_pct else None
    cur_abs = absv[-1] if has_abs else None

    if has_abs:
        # —— 绝对经验口径（分辨率高，推荐）——
        if min(absv) < 0 or absv[-1] < absv[0] - 1:
            res["note"] = "检测到升级（经验已回绕），正在重建基准"
            return res
        per_sec = _slope(ts, [float(v) for v in absv])
        per_min = per_sec * 60.0
        res["gained"] = absv[-1] - absv[0]
        res["exp_per_min"] = per_min
        res["exp_per_hour"] = per_min * 60.0
        # 本级总经验：优先用 当前经验 / 当前百分比 反推
        total = None
        if cur_pct is not None and cur_pct >= 2.0 and cur_abs:
            total = cur_abs * 100.0 / cur_pct
        res["total_exp"] = int(round(total)) if total else None
        if total:
            remain = max(0.0, total - cur_abs)
            res["remain_exp"] = int(round(remain))
            if per_min > 1e-6:
                res["eta_min"] = remain / per_min
            else:
                res["note"] = "经验未增长（挂机或已满级）"
        elif has_pct:
            # 拿不到总经验时退回百分比口径
            per_pct_min = _slope(ts, [float(v) for v in pcts]) * 60.0
            res["pct_per_min"] = per_pct_min
            res["pct_per_hour"] = per_pct_min * 60.0
            if per_pct_min > 1e-6:
                res["eta_min"] = max(0.0, (100.0 - (cur_pct or 0)) / per_pct_min)
        else:
            res["note"] = "经验百分比缺失，无法算升级时间"
        return res

    # —— 纯百分比口径（客户端不显示绝对经验时）——
    if not has_pct:
        res["note"] = "既没有经验数值也没有百分比"
        return res
    if cur_pct is not None and pcts[0] is not None and cur_pct < pcts[0] - 1.0:
        res["note"] = "检测到升级（百分比已回绕），正在重建基准"
        return res
    per_pct_min = _slope(ts, [float(v) for v in pcts]) * 60.0
    res["pct_per_min"] = per_pct_min
    res["pct_per_hour"] = per_pct_min * 60.0
    res["gained"] = None
    if per_pct_min > 1e-6 and cur_pct is not None:
        res["eta_min"] = max(0.0, (100.0 - cur_pct) / per_pct_min)
    else:
        res["note"] = "经验未增长（挂机或已满级）"
    return res


def fmt_eta(minutes):
    if minutes is None:
        return "--"
    m = int(round(minutes))
    if m < 1:
        return "<1 分钟"
    if m < 60:
        return "%d 分钟" % m
    h, mm = divmod(m, 60)
    if h < 24:
        return "%d 小时 %02d 分" % (h, mm)
    d, hh = divmod(h, 24)
    return "%d 天 %d 小时" % (d, hh)


def fmt_int(v):
    if v is None:
        return "--"
    try:
        return "{:,}".format(int(round(float(v))))
    except Exception:
        return "--"


# ============================================================
# 监控引擎
# ============================================================
class WatchEngine(threading.Thread):
    """
    定时抓帧 → OCR 数字 → 解析 → 投递到队列。

    队列消息统一为 (kind, payload)：
      ("watch_sample", {...})   一次有效采样（含 hp/mp/exp 原始数值）
      ("watch_status", str)     状态文案
      ("watch_fatal",  str)     致命错误
      ("watch_purge", int)      请求清理旧样本
    """

    def __init__(self, cfg, out_queue, capturer=None, title_getter=None, ocr=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.q = out_queue
        self._capturer = capturer
        self._title = title_getter
        self._ocr = ocr
        self._stop_ev = threading.Event()
        self._sct = None
        self.stats = {"frames": 0, "ok": 0, "fail": 0}
        self.last_raw = ""
        # 飘字区（经验 / 金币获取）累计
        self.pop_total = {"exp": 0, "meso": 0}
        self.pop_t0 = time.time()
        self._pop_seen = []
        self._pop_dedup = 2.6
        self.last_pop_raw = ""

    # ---------- 内部 ----------
    def _sct_obj(self):
        if self._capturer is not None:
            return None
        if self._sct is None:
            import mss
            self._sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        return self._sct

    def grab(self, region):
        if self._capturer is not None:
            return self._capturer.grab(region)
        left, top, w, h = [int(x) for x in region]
        shot = self._sct_obj().grab({"left": left, "top": top,
                                     "width": max(1, w), "height": max(1, h)})
        img = np.asarray(shot)
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    def _foreground_ok(self):
        if not self.cfg.get("only_when_foreground", True):
            return True
        kw = (self.cfg.get("window_keyword") or "").strip().lower()
        if not kw:
            return True
        if self._title is None:
            return True
        try:
            return kw in (self._title() or "").lower()
        except Exception:
            return True

    @staticmethod
    def regions_of(cfg):
        """合并所有已配置区域，去重保序。"""
        out = []
        for k in ("stat_region", "level_region", "exp_region", "hp_region", "mp_region", "pop_region"):
            r = (cfg or {}).get(k)
            if r and len(r) == 4 and int(r[2]) > 2 and int(r[3]) > 2:
                rr = [int(x) for x in r]
                if rr not in out:
                    out.append(rr)
        return out

    # ---------- 生命周期 ----------
    def _pop_accum(self, pr):
        """飘字去重累计：同一类型同一数值在 _pop_dedup 秒内只计一次。返回本次新增。"""
        now = time.time()
        add = {"exp": 0, "meso": 0}
        for kind in ("exp", "meso"):
            v = pr.get(kind)
            if not v:
                continue
            hit = False
            for k, val, ts in self._pop_seen:
                if k == kind and val == v and now - ts < self._pop_dedup:
                    hit = True
                    break
            if hit:
                continue
            self._pop_seen.append((kind, v, now))
            while len(self._pop_seen) > 60:
                self._pop_seen.pop(0)
            add[kind] = v
            self.pop_total[kind] = self.pop_total.get(kind, 0) + v
        return add

    def stop(self):
        self._stop_ev.set()

    def run(self):
        w = self.cfg or {}
        stat_iv = max(0.3, int(w.get("stat_interval_ms", w.get("interval_ms", 2000)) or 2000) / 1000.0)
        pop_iv = max(0.3, int(w.get("pop_interval_ms", 1000) or 1000) / 1000.0)
        ttl = max(1.0, int(w.get("static_ttl_ms", 4000) or 4000) / 1000.0)
        main = []
        for key in ("stat_region", "exp_region", "hp_region", "mp_region"):
            r = w.get(key)
            if r and len(r) == 4 and int(r[2]) > 4 and int(r[3]) > 4:
                r = [int(x) for x in r]
                if r not in main:
                    main.append(r)
        lvl = w.get("level_region")
        if lvl and len(lvl) == 4 and int(lvl[2]) > 4 and int(lvl[3]) > 4:
            lvl = [int(x) for x in lvl]
        else:
            lvl = None
        pop_region = w.get("pop_region")
        if pop_region and len(pop_region) == 4 and int(pop_region[2]) > 4 and int(pop_region[3]) > 4:
            pop_region = [int(x) for x in pop_region]
        else:
            pop_region = None
        if not main and lvl is None and pop_region is None:
            self.q.put(("watch_fatal", "还没有框选状态区域（HP/MP/EXP 数字所在的横条）"))
            return
        if self._ocr is None:
            self.q.put(("watch_fatal", "OCR 引擎加载失败，请确认 rapidocr_onnxruntime 已安装"))
            return
        self.q.put(("watch_status", "数字识别已启动（状态条 %.1fs / 飘字 %.1fs）" % (stat_iv, pop_iv)))
        if pop_region is not None:
            self.q.put(("watch_status", "经验/金币飘字识别已启动"))
        next_stat = 0.0
        next_pop = 0.0
        next_lvl = 0.0
        last_purge = time.time()
        slow_mark = 0.0
        while not self._stop_ev.is_set():
            if not self._foreground_ok():
                self.q.put(("watch_status", "游戏窗口不在前台，暂停采样"))
                self._stop_ev.wait(min(stat_iv, pop_iv))
                continue
            now = time.time()
            do_main = bool(main) and now >= next_stat
            do_lvl = lvl is not None and now >= next_lvl
            do_stat = do_main or do_lvl
            do_pop = pop_region is not None and now >= next_pop
            if not do_stat and not do_pop:
                cands = []
                if main:
                    cands.append(next_stat)
                if lvl is not None:
                    cands.append(next_lvl)
                if pop_region is not None:
                    cands.append(next_pop)
                gap = (min(cands) - now) if cands else 0.2
                self._stop_ev.wait(max(0.02, min(0.3, gap)))
                continue
            t0 = time.time()
            regions_now = []
            if do_main:
                regions_now.extend(main)
            if do_lvl and lvl is not None and lvl not in regions_now:
                regions_now.append(lvl)
            shots = []
            for r in regions_now:
                try:
                    shots.append(self.grab(r))
                except Exception:
                    pass
            pop_img = None
            if do_pop:
                try:
                    pop_img = self.grab(pop_region)
                except Exception:
                    pop_img = None
            try:
                if do_stat and shots and pop_img is not None:
                    st, pr, raw, praw = read_combined(shots, pop_img, ocr=self._ocr)
                elif do_stat and shots:
                    st, raw = read_stats(shots, ocr=self._ocr)
                    pr, praw = {"exp": None, "meso": None, "raw": ""}, ""
                elif pop_img is not None:
                    ret = read_popups(pop_img, ocr=self._ocr)
                    if isinstance(ret, tuple) and len(ret) == 2:
                        pr, praw = ret
                    else:
                        pr, praw = ret, ""
                    st, raw = None, ""
                else:
                    st, raw, pr, praw = None, "", {"exp": None, "meso": None, "raw": ""}, ""
                if praw:
                    self.last_pop_raw = praw
                if raw:
                    self.last_raw = raw
                if pop_img is not None:
                    add = self._pop_accum(pr)
                    if add.get("exp") or add.get("meso"):
                        span = max((time.time() - self.pop_t0) / 60.0, 1e-6)
                        self.q.put(("watch_pop", {
                            "ts": time.time(),
                            "exp": add.get("exp", 0),
                            "meso": add.get("meso", 0),
                            "total_exp": self.pop_total.get("exp", 0),
                            "total_meso": self.pop_total.get("meso", 0),
                            "span_min": span,
                            "exp_per_min": self.pop_total.get("exp", 0) / span,
                            "meso_per_hour": self.pop_total.get("meso", 0) / span * 60.0,
                            "raw": self.last_pop_raw,
                        }))
                    next_pop = max(now + pop_iv, time.time() + 0.2)
                self.stats["frames"] += 1
                if do_stat:
                    if st:
                        self.stats["ok"] += 1
                        lv = st.get("level") or int(w.get("level", 1) or 1)
                        self.q.put(("watch_sample", {
                            "ts": time.time(), "level": int(lv),
                            "exp_abs": st.get("exp_abs"), "exp_pct": st.get("exp_pct"),
                            "hp": st.get("hp"), "hp_max": st.get("hp_max"),
                            "mp": st.get("mp"), "mp_max": st.get("mp_max"),
                            "raw": raw,
                        }))
                    else:
                        self.stats["fail"] += 1
                        self.q.put(("watch_status", "没识别到数字，请重新框选，确保框住 HP/MP/EXP 那一整条"))
                    if do_lvl:
                        next_lvl = max(now + ttl, time.time() + 0.2)
                    if do_main:
                        next_stat = max(now + stat_iv, time.time() + 0.2)
                if time.time() - last_purge > 600:
                    last_purge = time.time()
                    self.q.put(("watch_purge", int(w.get("keep_hours", 12))))
            except Exception as e:
                self.stats["fail"] += 1
                self.q.put(("watch_status", "采样异常：%s" % e))
            spent = time.time() - t0
            if spent > max(stat_iv, pop_iv) * 0.85:
                now2 = time.time()
                if do_main:
                    next_stat = now2 + stat_iv
                if do_pop:
                    next_pop = now2 + pop_iv
                if now2 - slow_mark > 30:
                    slow_mark = now2
                    self.q.put(("watch_status", "单轮 OCR 耗时 %.2fs，已自动按实际耗时间隔采样" % spent))
        self.q.put(("watch_status", "监控已停止"))
