# -*- coding: utf-8 -*-
"""
MapleChat Translator  ——  冒险岛国际服聊天实时 OCR 翻译（单文件自用版）

链路：区域截屏 -> 图像预处理 -> RapidOCR 识别 -> 增量去重 -> LLM 批量翻译 -> 置顶悬浮窗

只做屏幕像素截取，不注入游戏进程、不读内存，对反作弊透明。
"""

import ctypes
import hashlib
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import traceback
from collections import OrderedDict
from pathlib import Path

# ---------- 平台适配层（Windows / macOS）：DPI 感知必须在 tkinter / mss 之前设置 ----------
from plat import (IS_WIN, IS_MAC, FONT_UI, FONT_MONO, MOD_NAME, set_dpi_aware,
                  init_pixel_scale, pixel_scale, get_foreground_title as _fg_title,
                  set_clipboard as _set_clip, apply_floating)
set_dpi_aware()

import tkinter as tk
from ui_flat import FlatButton
from tkinter import ttk, messagebox
try:
    import chatgen
except Exception:
    chatgen = None


import cv2
import numpy as np
import requests

try:
    import datawatch                      # 数据看板读数模块（经验/HP/MP）
except Exception:                         # 缺依赖时不拖垮主程序
    datawatch = None

if getattr(sys, "frozen", False):
    APP_DIR = Path(os.path.abspath(os.path.dirname(sys.argv[0])))
else:
    APP_DIR = Path(os.path.abspath(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = APP_DIR / "config.json"
TERMS_PATH = APP_DIR / "terms.json"

if IS_MAC:
    # macOS 的 .app 是只读包：配置按苹果规范落到用户目录，替换/升级 app 不丢 API Key 与框选坐标
    try:
        _mac_cfg_dir = Path.home() / "Library" / "Application Support" / "MapleChat"
        _mac_cfg_dir.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH = _mac_cfg_dir / "config.json"
    except Exception:
        pass

# 落盘目录：mac 的 .app 是只读包，watch.db 与调试图必须写到用户数据目录，否则 sqlite 建库直接抛错
DATA_DIR = CONFIG_PATH.parent if IS_MAC else APP_DIR


# ============================================================
# 配置
# ============================================================
# ============================================================
# 主题（深色，与悬浮窗统一）
# ============================================================
C_BG     = "#0b0d11"
C_CARD   = "#151922"
C_CARD2  = "#1d222d"
C_LINE   = "#252b38"
C_TXT    = "#e9edf4"
C_MUTED  = "#79839a"
C_DIM    = "#4b5364"
C_ACCENT = "#00e0a0"
C_BLUE   = "#7c8cff"
C_WARN   = "#ffb454"
C_ERR    = "#ff6b6b"
C_FONT   = FONT_UI


DEFAULT_CONFIG = {
    "region": None,
    "api_key": "",
    "api_base": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "target_lang": "简体中文",
    "interval_ms": 700,
    "scale": 2.5,
    "binarize": True,
    "only_when_foreground": True,
    "window_keyword": "",
    "skip_first_scan": True,
    "terms_enabled": True,          # 术语库（terms.json）
    "auto_start": True,            # 启动即开始翻译（省一次点击）
    "start_minimized": False,      # 启动只留悬浮窗，热键唤回主窗
    "hotkeys": True,               # 全局热键
    "win_geometry": "",            # 主窗位置记忆
    "overlay": {
        "x": 80, "y": 480, "w": 480, "h": 340,
        "alpha": 0.9, "font_size": 11, "max_lines": 12,
    },
    "watch": {                     # 数据看板（OCR 直接读 HP/MP/EXP 数字）
        "enabled": False,
        "stat_region": None,       # 主用：底部状态条（HP/MP/EXP 数字都在里面）
        "level_region": None,      # 可选：等级数字
        "exp_region": None,        # 兼容旧配置
        "hp_region": None,
        "mp_region": None,
        "pop_region": None,        # 可选：经验/金币飘字区（读「+8 EXP」「+10 mesos」）
        "level": 1,
        "interval_ms": 2000,       # 兼容旧配置，等价于 stat_interval_ms
        "stat_interval_ms": 2000,  # v8 分层调度：状态条（HP/MP/EXP）间隔
        "pop_interval_ms": 1000,   # v8 分层调度：飘字间隔（飘字只活 2~3s，需高频）
        "static_ttl_ms": 4000,     # v8 分层调度：静态指标（等级）缓存 TTL
        "only_when_foreground": True,
        "window_keyword": "",
        "keep_hours": 12,
    },
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception:
            pass
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ============================================================
# 术语库（MSCW 中英对照，来源 mscw-guidebook.com）
# ============================================================
TERM_FIX = {
    "str": "力量", "strength": "力量",
    "dex": "敏捷", "dexterity": "敏捷",
    "int": "智力", "intelligence": "智力",
    "luk": "运气", "luck": "运气",
    "hat": "帽子", "helm": "头盔",
    "top": "上衣", "bottom": "裤子", "overall": "套服",
    "shoes": "鞋子", "gloves": "手套",
    "shield": "盾牌", "cape": "披风",
    "rng": "看脸、随机", "random number generator": "看脸、随机",
}


def _term_rank(s):
    """英文写法的优先度：纯字母缩写(wts/ty/lf) > 普通短语(want to sell) > 带符号写法(s>)，同档按长度升序。"""
    if re.fullmatch(r"[a-z]{2,5}", s):
        return (0, len(s), s)
    if re.fullmatch(r"[a-z0-9%\.\-\+ ]{1,16}", s):
        return (1, len(s), s)
    return (2, len(s), s)


def load_terms():
    """返回 (flat_map, meta)。flat_map: 小写英文 -> 中文；meta 含 count/version 等信息。"""
    flat = {}
    meta = {"count": 0, "version": "", "groups": 0, "source": ""}
    if not TERMS_PATH.exists():
        return flat, meta
    try:
        raw = json.loads(TERMS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return flat, meta
    groups = raw.get("groups") or {}
    for _, table in groups.items():
        if not isinstance(table, dict):
            continue
        for k, v in table.items():
            k2 = str(k).strip().lower()
            if not k2 or not isinstance(v, str) or not v.strip():
                continue
            flat[k2] = v.strip()
    meta["count"] = len(flat)
    meta["version"] = str(raw.get("version", ""))
    meta["groups"] = len(groups)
    meta["source"] = str(raw.get("source", ""))
    # 原站点这几行是"整行一条释义"，抓下来后同组每个词都拿到了"力量、敏捷、智力、运气"这种并列串，
    # 直接用于翻译会把 dex 译成"力量"。这里按游戏实际含义纠正，覆盖在最上层，重新抓取也不会回退。
    for k, v in TERM_FIX.items():
        if k in flat:
            flat[k] = v
    return flat, meta


_PREFIX_RE = re.compile(r"^\s*(?:\[[^\]]{1,24}\]\s*)?[^\s:：\[\]]{0,20}\s*[:：]\s*")


def split_prefix(text):
    """把 "[Party] Bob: gg" 拆成 ("[Party] Bob: ", "gg")"""
    m = _PREFIX_RE.match(text)
    if not m:
        return "", text
    return text[:m.end()], text[m.end():]


def norm_term(text):
    """归一化用于术语精确匹配：小写、去首尾标点、压缩空格"""
    s = text.strip().lower()
    s = re.sub(r"[.!?,;~\"'`*]+$", "", s)
    s = re.sub(r"^[.!?,;~\"'`*]+", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def term_hits(text, terms, max_n=40):
    """在一行文本里找命中的术语（词边界、长词优先、不重叠），返回 [(term, zh)]"""
    if not terms:
        return []
    low = text.lower()
    cands = sorted(terms.keys(), key=len, reverse=True)
    hits, used = [], []
    for k in cands:
        if len(k) < 2:
            continue
        start = 0
        while True:
            i = low.find(k, start)
            if i < 0:
                break
            j = i + len(k)
            left_ok = i == 0 or not low[i - 1].isalnum()
            right_ok = j >= len(low) or not low[j].isalnum()
            if left_ok and right_ok and not any(a < j and i < b for a, b in used):
                used.append((i, j))
                hits.append((k, terms[k]))
                break
            start = i + 1
        if len(hits) >= max_n:
            break
    return hits


def get_foreground_title():
    return _fg_title()


# ============================================================
# 剪贴板
# ============================================================
def set_clipboard(text):
    """写系统剪贴板：Windows 直写 CF_UNICODETEXT，macOS 走 pbcopy，兜底 tk。"""
    return _set_clip(text)


# ============================================================
def new_sct():
    """mss 10.x 里 mss.mss() 旧入口会直接把进程干掉，必须走 mss.MSS()，这里做版本兼容"""
    import mss
    return mss.MSS() if hasattr(mss, "MSS") else mss.mss()


class Capturer:
    def __init__(self):
        self._sct = None

    def grab(self, region):
        if self._sct is None:
            self._sct = new_sct()
        left, top, w, h = [int(x) for x in region]
        if IS_MAC:
            # Retina：框选拿到的是逻辑点，mss 要的是物理像素
            s = pixel_scale()
            left, top, w, h = int(left * s), int(top * s), int(w * s), int(h * s)
        shot = self._sct.grab({"left": left, "top": top, "width": max(1, w), "height": max(1, h)})
        img = np.asarray(shot)          # BGRA
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


# ============================================================
# 图像预处理（游戏小字识别的关键）
# ============================================================
def preprocess(frame, scale=2.5, binarize=False):
    if scale and scale > 1:
        frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # 冒险岛聊天是暗底亮字：整体偏暗时反色成白底黑字，识别率高很多
    if float(gray.mean()) < 118:
        gray = cv2.bitwise_not(gray)
    if binarize:
        gray = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 25, 12
        )
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


# ============================================================
# OCR
# ============================================================
class OcrEngine:
    def __init__(self):
        self.engine = None
        self.lock = threading.Lock()

    def ensure(self):
        with self.lock:
            if self.engine is None:
                from rapidocr_onnxruntime import RapidOCR
                self.engine = RapidOCR()
        return self.engine

    def read(self, frame, scale=2.5, binarize=False):
        self.ensure()
        img = preprocess(frame, scale, binarize)
        try:
            res, _ = self.engine(img, use_det=True, use_cls=False, use_rec=True)
        except TypeError:
            res, _ = self.engine(img)
        if not res:
            return []

        items = []
        for row in res:
            try:
                box, text, score = row[0], row[1], float(row[2])
            except Exception:
                continue
            if score < 0.35:
                continue
            text = (text or "").strip()
            if not text:
                continue
            ys = [p[1] for p in box]
            xs = [p[0] for p in box]
            items.append({
                "x": min(xs),
                "y": (min(ys) + max(ys)) / 2.0,
                "h": max(ys) - min(ys),
                "t": text,
            })

        if not items:
            return []

        # 按纵向位置聚合成行，行内按横向拼接
        items.sort(key=lambda d: d["y"])
        groups, cur = [], []
        for it in items:
            if not cur:
                cur = [it]
                continue
            ref = cur[0]
            tol = max(ref["h"], it["h"]) * 0.6
            if abs(it["y"] - ref["y"]) <= tol:
                cur.append(it)
            else:
                groups.append(cur)
                cur = [it]
        if cur:
            groups.append(cur)

        out = []
        for g in groups:
            g.sort(key=lambda d: d["x"])
            text = " ".join(x["t"] for x in g)
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                out.append(text)
        return out


# ============================================================
# 翻译
# ============================================================
SYS_PROMPT = """你是《冒险岛》(MapleStory) 国际服的聊天翻译助手。玩家给你若干条游戏内聊天消息，你要翻译成{lang}。

硬性规则：
1. 只输出译文本身，不要原文、不要解释、不要引号或括号补充说明。
2. 保留消息里的频道标记和玩家ID原样，例如 "[Party] Bob: hello" -> "[Party] Bob: 你好"。
3. 游戏黑话按玩家习惯翻译：gg=打得不错, ez=轻松碾压, diff=差距, noob=菜鸟, lag=卡, dc=掉线, ks=抢怪, pq=组队任务, pt=组队, lf=求组, mesos=金币, fm=自由市场, wts=出售, wtb=收购, wtt=交换, ty=谢谢, np=不客气, omw=马上到, afk=挂机, brb=马上回来, re=再来一次, rip=可惜, mule=小号, gpq=公会组队任务, boss=首领, drop=掉落, exp=经验, pot=药水。
4. 拼写错误、缩写、语法破碎的蹩脚英语，按玩家实际想表达的意思翻译，不要按字面硬译。
5. 纯符号、纯表情、纯数字、无意义刷屏，输出空字符串。
6. 玩家ID、数字、装备名保留原文。
7. 不要添加"玩家说"之类的描述性文字。
8. 若下方给出「术语参考」，其中的术语（装备名、技能名、地图名、怪物名、缩写）必须严格按给定译法翻译，不得自创或替换成别的词；即使你认为有更常见的叫法，也必须使用术语参考里的译法；术语参考为空则忽略本条。

{terms}输入每行一条，行首带 [序号]。输出必须逐行对应，行首同样带 [序号]，例如：
[1] 你好
[2] 有人一起打boss吗"""


REPLY_PROMPT = """你是《冒险岛》(MapleStory) 国际服中文玩家的聊天助手。玩家给你一句中文，你要把它变成可以直接粘贴进游戏聊天框的英文。

硬性规则：
1. 只输出一行英文，不要中文、不要解释、不要引号、不要括号注释。
2. 说人话：像国际服玩家真实打字那样，短、口语化，能用缩写就用缩写：wtb=想收, wts=出售, wtt=换, lf=求组, pt=组队, pq=组队任务, fm=自由市场, ch=频道, ea=每个, obo=可议价, pm=私聊, ty=谢谢, np=不客气, omw=马上到, afk=挂机, brb=马上回来, gtg=得走了, gg=打得好, mesos=金币, pot=药水, exp=经验, drop=掉落, boss=首领, mule=小号, ks=抢怪。
3. 一句话说完，不要写作文式长句，不要堆礼貌语。
4. 玩家ID、数字、百分比、装备名原样保留，例如 30% dex wg。
5. 中文里的语气词（啊/吧/呢/啦/嘛）不要直译，不要加表情符号。
6. 交易类按「wtb/wts + 物品 + 价格 + pm me」的结构写；求组按「lf pt / lf pq + pm me」的结构写。
7. 若下方给出「术语参考」，其中出现的词必须使用它给出的英文写法，不得自创或换成别的词；术语参考为空则忽略本条。

示例（只学格式，不要照抄）：
想在自由市场1频道收30%敏捷工地手套，私聊我 -> wtb 30% dex wg pm me fm ch1
有人一起做组队任务吗 -> anyone pq? lf pt

{terms}"""

class Translator:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cache = OrderedDict()
        self.cache_limit = 3000
        self.lock = threading.Lock()
        self.stats = {"requests": 0, "hits": 0}
        # 术语库：terms.json（可自行增删）；enabled 关闭时完全不参与
        self.terms, self.terms_meta = load_terms()
        if not cfg.get("terms_enabled", True):
            self.terms = {}
        self.terms_ver = hashlib.md5(
            (str(sorted(self.terms.items()))).encode("utf-8")).hexdigest()[:8]
        self._inv = None      # 中文 -> 英文 的反向术语索引（懒构建）

    def _key(self, text):
        raw = (self.cfg.get("target_lang", "") + "|" + self.terms_ver + "|" + text).encode("utf-8")
        return hashlib.md5(raw).hexdigest()

    def get_cached(self, text):
        k = self._key(text)
        with self.lock:
            if k in self.cache:
                self.cache.move_to_end(k)
                self.stats["hits"] += 1
                return self.cache[k]
        return None

    def put_cache(self, text, dst):
        k = self._key(text)
        with self.lock:
            self.cache[k] = dst
            self.cache.move_to_end(k)
            while len(self.cache) > self.cache_limit:
                self.cache.popitem(last=False)

    @staticmethod
    def translatable(text):
        return bool(re.search(r"[A-Za-z]{2,}", text))

    @staticmethod
    def strict(zh):
        """整句直译用：只认唯一、简短、无歧义的译法"""
        return bool(zh) and "、" not in zh and "（" not in zh and len(zh) <= 8

    @staticmethod
    def inject_zh(zh):
        """提示词注入用：剥掉括号注释后仍唯一才注入，多义条目让模型按语境判断"""
        z = re.sub(r"[（(][^）)]*[）)]", "", zh or "").strip()
        if not z or "、" in z or len(z) > 10:
            return ""
        return z

    def direct_hit(self, text):
        """整条消息就是一个术语时直接出译文，不走 API（零成本、零延迟）。"""
        if not self.terms:
            return None
        pre, core = split_prefix(text)
        zh = self.terms.get(norm_term(core))
        if not self.strict(zh):
            return None
        return pre + zh

    def terms_block(self, texts):
        """只把本批次里实际出现过的术语塞进提示词，避免整库注入爆 token"""
        if not self.terms:
            return ""
        seen, pairs = set(), []
        for t in texts:
            for k, zh in term_hits(t, self.terms, max_n=20):
                if k in seen:
                    continue
                z = self.inject_zh(zh)
                if not z:
                    continue
                seen.add(k)
                pairs.append("%s=%s" % (k, z))
                if len(pairs) >= 40:
                    break
            if len(pairs) >= 40:
                break
        if not pairs:
            return ""
        return "术语参考（必须按此译法）：" + "；".join(pairs) + "\n\n"

    # ---------- 反向：中文 -> 英文（用来打字回复对方） ----------
    def inv(self):
        """把 en->zh 术语表翻成 中文词 -> [英文写法...] 索引，短写法优先。"""
        if self._inv is not None:
            return self._inv
        inv = {}
        for en, zh in (self.terms or {}).items():
            for part in re.split(r"[、,，/;；]", zh or ""):
                z = re.sub(r"[（(][^）)]*[）)]", "", part).strip(" \t.。:：")
                if not z or len(z) > 10 or not re.search(r"[\u4e00-\u9fff]", z):
                    continue
                inv.setdefault(z, set()).add(en)
        self._inv = {k: sorted(v, key=_term_rank) for k, v in inv.items()}
        return self._inv

    def rev_hit(self, zh):
        """整句就是一个术语时零成本直出英文；多候选按 rank 取最常用的那个（wts/ty 这类缩写优先）。"""
        inv = self.inv()
        key = re.sub(r"[\s。！？!?~，,\.、]+", "", zh or "")
        cands = inv.get(key)
        if cands:
            return cands[0]
        return None

    def rev_terms_block(self, zh):
        """从中文句子里揪出命中的术语，给出英文候选，供模型选词。"""
        inv = self.inv()
        if not inv or not zh:
            return ""
        pairs, seen = [], set()
        for z in sorted(inv.keys(), key=len, reverse=True):
            if len(z) < 2 or z in seen or z not in zh:
                continue
            seen.add(z)
            pairs.append("%s=%s" % (z, "/".join(inv[z][:2])))
            if len(pairs) >= 12:
                break
        if not pairs:
            return ""
        return "游戏常用写法（优先采用，再按语境调整）：" + "；".join(pairs) + "\n\n"

    def reverse(self, zh):
        """中文 -> 一行英文。命中缓存/术语零成本，否则一次 API。"""
        zh = (zh or "").strip()
        if not zh:
            return ""
        k = "R|" + zh
        c = self.get_cached(k)
        if c is not None:
            return c
        d = self.rev_hit(zh)
        if d is not None:
            self.put_cache(k, d)
            return d
        if not self.cfg.get("api_key"):
            return "__ERR__未配置 API Key，请先在「翻译设置」里填 Key"
        out = self._request_reverse(zh)
        if out and not out.startswith("__ERR__"):
            self.put_cache(k, out)
        return out

    def _request_reverse(self, zh):
        cfg = self.cfg
        tb = self.rev_terms_block(zh)
        url = cfg["api_base"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json"}
        body = {"model": cfg["model"],
                "messages": [{"role": "system", "content": REPLY_PROMPT.format(terms=tb)},
                             {"role": "user", "content": zh}],
                "temperature": 0.3, "max_tokens": 160, "stream": False}
        try:
            self.stats["requests"] += 1
            r = requests.post(url, headers=headers, json=body, timeout=(4, 15))
            if r.status_code != 200:
                return "__ERR__〔接口 %d〕%s" % (r.status_code, r.text[:80])
            content = r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            return "__ERR__〔请求失败〕%s" % e
        return self._clean_reply(content)

    @staticmethod
    def _clean_reply(content):
        s = (content or "").strip()
        s = re.sub(r"^\s*(?:英文|English|译文|结果|Output)\s*[:：]\s*", "", s, flags=re.I)
        for line in s.splitlines():
            line = line.strip().strip('"“”\'`').strip()
            if line and re.search(r"[A-Za-z]", line):
                return line
        return ""

    def translate_batch(self, texts):
        results = [None] * len(texts)
        pending = []
        for i, t in enumerate(texts):
            c = self.get_cached(t)
            if c is not None:
                results[i] = c
            elif not self.translatable(t):
                results[i] = ""
            else:
                d = self.direct_hit(t)
                if d is not None:
                    results[i] = d
                    self.put_cache(t, d)
                else:
                    pending.append(i)

        if not pending:
            return [r or "" for r in results]

        if not self.cfg.get("api_key"):
            # 无 Key 时不翻译，悬浮窗只显示原文，避免每行都刷提示
            return [r or "" for r in results]

        for start in range(0, len(pending), 20):
            chunk = pending[start:start + 20]
            payload = [texts[i] for i in chunk]
            outs = self._request(payload)
            for idx, src, dst in zip(chunk, payload, outs):
                if dst.startswith("__ERR__"):
                    results[idx] = dst[7:]      # 错误信息不写缓存，下一轮仍可重试
                else:
                    results[idx] = dst
                    if dst:
                        self.put_cache(src, dst)

        return [r or "" for r in results]

    def _request(self, texts):
        cfg = self.cfg
        tb = self.terms_block(texts)
        numbered = "\n".join("[%d] %s" % (i + 1, t) for i, t in enumerate(texts))
        url = cfg["api_base"].rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": "Bearer " + cfg["api_key"],
            "Content-Type": "application/json",
        }
        body = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": SYS_PROMPT.format(
                    lang=cfg.get("target_lang", "简体中文"), terms=tb)},
                {"role": "user", "content": numbered},
            ],
            "temperature": 0.2,
            "max_tokens": 1200,
            "stream": False,
        }
        try:
            self.stats["requests"] += 1
            r = requests.post(url, headers=headers, json=body, timeout=(4, 15))
            if r.status_code != 200:
                return ["__ERR__〔接口 %d〕%s" % (r.status_code, r.text[:80])] * len(texts)
            content = r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            return ["__ERR__〔请求失败〕%s" % e] * len(texts)
        return self._parse(content, len(texts))

    @staticmethod
    def _parse(content, n):
        # 兼容 [1] / 1. / 1、 / 1) / 1: 等编号形态，编号后的标点可省略
        idx_re = re.compile(r"^\s*[\[\(【]?(\d{1,3})[\]\)】]?\s*[\.、:：\-\)]?\s*(.*)$")

        def strip_idx(s):
            m = idx_re.match(s.strip())
            if m and m.group(2).strip():
                return m.group(2).strip()
            return s.strip()

        lines = [l.strip() for l in content.splitlines() if l.strip()]
        parsed = {}
        for l in lines:
            m = idx_re.match(l)
            if m and m.group(2).strip():
                parsed[int(m.group(1))] = m.group(2).strip()
        if parsed and all(i in parsed for i in range(1, n + 1)):
            return [parsed[i] for i in range(1, n + 1)]
        if n == 1:
            return [strip_idx(content)]
        cleaned = [strip_idx(l) for l in lines]
        if len(cleaned) == n:
            return cleaned
        return [content.strip()] + [""] * (n - 1)


# ============================================================
# 扫描引擎（后台线程）
# ============================================================
class ScanEngine(threading.Thread):
    def __init__(self, cfg, out_queue):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.q = out_queue
        self.capturer = Capturer()
        self.ocr = OcrEngine()
        self.translator = Translator(cfg)
        self.seen = OrderedDict()
        self._stop = threading.Event()
        self._first_scan = True

    def stop(self):
        self._stop.set()

    def run(self):
        self.q.put(("status", "正在加载 OCR 模型…"))
        try:
            self.ocr.ensure()
            # 预热：onnxruntime 首次推理有编译开销，先空跑两遍，避免第一帧卡 1.8 秒
            warm = np.full((48, 160, 3), 255, dtype=np.uint8)
            for _ in range(2):
                self.ocr.read(warm, 1.0, False)
        except Exception as e:
            self.q.put(("fatal", "OCR 引擎加载失败：%s" % e))
            return
        self.q.put(("status", "运行中"))

        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._tick()
            except Exception:
                self.q.put(("error", traceback.format_exc(limit=3)))
            cost_ms = (time.time() - t0) * 1000.0
            wait_ms = max(60, int(self.cfg.get("interval_ms", 500)) - int(cost_ms))
            self._stop.wait(wait_ms / 1000.0)

        self.q.put(("status", "已停止"))

    def _tick(self):
        region = self.cfg.get("region")
        if not region or len(region) != 4:
            self._stop.wait(0.5)
            return

        if self.cfg.get("only_when_foreground", True):
            kw = (self.cfg.get("window_keyword") or "").strip().lower()
            if kw and kw not in get_foreground_title().lower():
                self.q.put(("status", "游戏不在前台，暂停识别"))
                self._stop.wait(0.4)
                return

        frame = self.capturer.grab(region)

        t_ocr = time.time()
        lines = self.ocr.read(frame, self.cfg.get("scale", 2.5), self.cfg.get("binarize", False))
        ocr_ms = (time.time() - t_ocr) * 1000.0

        if not lines:
            self.q.put(("status", "未识别到文字"))
            return

        # 首轮只登记不翻译，避免启动瞬间把历史聊天全部送去翻译
        if self._first_scan and self.cfg.get("skip_first_scan", True):
            for t in lines:
                self.seen[t] = time.time()
            self._first_scan = False
            self.q.put(("status", "已建立基线，等待新消息…"))
            return
        self._first_scan = False

        new_lines = []
        for t in lines:
            if t in self.seen:
                self.seen.move_to_end(t)
                continue
            new_lines.append(t)

        if not new_lines:
            self.q.put(("status", "OCR %.0fms | 无新消息" % ocr_ms))
            return

        t_tr = time.time()
        outs = self.translator.translate_batch(new_lines)
        tr_ms = (time.time() - t_tr) * 1000.0

        now = time.time()
        for src, dst in zip(new_lines, outs):
            self.seen[src] = now
            self.q.put(("line", (src, dst)))

        while len(self.seen) > 400:
            self.seen.popitem(last=False)

        self.q.put(("status", "OCR %.0fms | 翻译 %.0fms | 新增 %d 条" % (ocr_ms, tr_ms, len(new_lines))))


# ============================================================
# 区域框选
# ============================================================
class RegionSelector:
    def __init__(self, root, callback, on_cancel=None, min_w=30, min_h=20, tip=None):
        self.root = root
        self.callback = callback
        self.on_cancel = on_cancel
        self.start = None
        self.rect = None
        self.min_w = min_w
        self.min_h = min_h

        if IS_MAC:
            # Tk 在 mac 上按逻辑点报屏幕尺寸，mss 报物理像素；遮罩必须用逻辑尺寸，否则超出屏幕
            self.vx, self.vy = 0, 0
            vw, vh = root.winfo_screenwidth(), root.winfo_screenheight()
        else:
            mon = new_sct().monitors[0]
            self.vx, self.vy = int(mon["left"]), int(mon["top"])
            vw, vh = int(mon["width"]), int(mon["height"])

        self.top = tk.Toplevel(root)
        apply_floating(self.top)
        self.top.attributes("-topmost", True)
        self.top.attributes("-alpha", 0.28)
        self.top.configure(bg="#000000")
        self.top.geometry("%dx%d+%d+%d" % (vw, vh, self.vx, self.vy))

        self.canvas = tk.Canvas(self.top, bg="#000000", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)

        tip_text = tip or "按住鼠标左键，框住游戏聊天区域；松开即保存。Esc 取消"
        self.canvas.create_text(vw // 2, 40, text=tip_text, fill="#ffffff",
                                font=(C_FONT, 16, "bold"))

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.top.bind("<Escape>", self._cancel)
        self.top.focus_force()
        self.canvas.focus_set()

    def _cancel(self, e=None):
        try:
            self.top.destroy()
        except Exception:
            pass
        if self.on_cancel:
            self.on_cancel()

    def _press(self, e):
        self.start = (e.x, e.y)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#00e0a0", width=2)

    def _move(self, e):
        if self.rect and self.start:
            self.canvas.coords(self.rect, self.start[0], self.start[1], e.x, e.y)

    def _release(self, e):
        if not self.start:
            return
        x1, y1 = self.start
        x2, y2 = e.x, e.y
        left, top = min(x1, x2), min(y1, y2)
        w, h = abs(x2 - x1), abs(y2 - y1)
        self.top.destroy()
        if w >= self.min_w and h >= self.min_h:
            self.callback([int(left) + self.vx, int(top) + self.vy, int(w), int(h)])
        else:
            messagebox.showwarning("区域太小",
                                   "框选区域小于 %dx%d，请重新框选。" % (self.min_w, self.min_h),
                                   parent=self.root)
            self._cancel()


# ============================================================
# 悬浮窗
# ============================================================
class Overlay:
    def __init__(self, root, cfg, on_save, on_reply=None, on_quick=None):
        self.cfg = cfg
        self.on_save = on_save
        self.on_reply = on_reply
        self.on_quick = on_quick
        self._last_en = ""
        o = cfg["overlay"]

        self.win = tk.Toplevel(root)
        apply_floating(self.win)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", float(o.get("alpha", 0.9)))
        # 位置兜底：屏幕分辨率变化 / 配置越界时，自动拉回可见区域
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        ow, oh = int(o.get("w", 480)), int(o.get("h", 340))
        ox = max(0, min(int(o.get("x", 80)), max(0, sw - ow)))
        oy = max(0, min(int(o.get("y", 480)), max(0, sh - oh)))
        if (ox, oy) != (int(o.get("x", 80)), int(o.get("y", 480))):
            cfg["overlay"]["x"], cfg["overlay"]["y"] = ox, oy
            try:
                on_save(cfg)
            except Exception:
                pass
        self.win.geometry("%dx%d+%d+%d" % (ow, oh, ox, oy))
        self.win.configure(bg="#0d0d12")

        self.bar = tk.Frame(self.win, bg="#0d0d12", height=20)
        self.bar.pack(fill="x")
        self.hint = tk.Label(self.bar, text="MapleChat  |  拖动移动  右键菜单",
                             fg="#5d6470", bg="#0d0d12",
                             font=(C_FONT, 8), anchor="w")
        self.hint.pack(side="left", padx=8)

        self._build_reply()
        self.text = tk.Text(self.win, bg="#0d0d12", fg="#ffffff", bd=0, wrap="word",
                            font=(C_FONT, int(o.get("font_size", 11))),
                            padx=10, pady=4, highlightthickness=0)
        self.text.pack(fill="both", expand=True)
        self.text.configure(state="disabled")

        self._apply_tags()

        for w in (self.bar, self.hint):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
        self.text.bind("<Double-Button-1>", lambda e: (self.copy_reply(), "break")[1])
        self.text.bind("<MouseWheel>", self._on_wheel)
        self.text.bind("<ButtonPress-1>", self._drag_start)
        self.text.bind("<B1-Motion>", self._drag_move)

        self.menu = tk.Menu(self.win, tearoff=0)
        for a in (0.55, 0.7, 0.85, 0.95, 1.0):
            self.menu.add_command(label="不透明度 %d%%" % int(a * 100),
                                  command=lambda v=a: self.set_alpha(v))
        self.menu.add_separator()
        self.menu.add_command(label="字号 +", command=lambda: self.bump_font(1))
        self.menu.add_command(label="字号 -", command=lambda: self.bump_font(-1))
        self.menu.add_separator()
        self.menu.add_command(label="复制回复内容", command=self.copy_reply)
        self.menu.add_command(label="清空", command=self.clear)
        self.menu.add_command(label="隐藏悬浮窗", command=self.hide)

        for w in (self.win, self.text, self.hint):
            w.bind("<Button-3>", self._popup)

        self._drag = (0, 0)

    # ---------- 底部打字回复条 ----------
    def _build_reply(self):
        o = self.cfg["overlay"]
        self.reply_bar = tk.Frame(self.win, bg="#0d0d12")
        self.reply_bar.pack(side="bottom", fill="x")
        tk.Frame(self.reply_bar, bg="#242833", height=1).pack(fill="x")
        row = tk.Frame(self.reply_bar, bg="#0d0d12")
        row.pack(fill="x", padx=7, pady=(4, 2))
        tk.Label(row, text="回复", fg="#00e0a0", bg="#0d0d12",
                 font=(C_FONT, 8)).pack(side="left")
        self.reply_var = tk.StringVar()
        self.reply_entry = tk.Entry(row, textvariable=self.reply_var, bg="#171a21", fg="#eef1f6",
                                    insertbackground="#00e0a0", relief="flat", bd=0,
                                    font=(C_FONT, max(8, int(o.get("font_size", 11)) - 1)))
        self.reply_entry.pack(side="left", fill="x", expand=True, padx=6, ipady=3)
        self.reply_entry.bind("<Return>", self._submit_reply)
        self.reply_hint = tk.Label(self.reply_bar, text="打中文 → 回车 → 自动译成英文并复制",
                                   fg="#5d6470", bg="#0d0d12",
                                   font=(C_FONT, 7), anchor="w")
        self.reply_hint.pack(fill="x", padx=8, pady=(0, 3))
        self.quick_row = tk.Frame(self.reply_bar, bg="#0d0d12")
        self.quick_row.pack(fill="x", padx=8, pady=(0, 4))
        self.quick_btn = FlatButton(self.quick_row, text="快捷组句", command=self._open_quick,
                                   bg="#00e0a0", fg="#04120c", bd=0, relief="flat",
                                   padx=8, pady=1, font=(C_FONT, 8, "bold"),
                                   cursor="hand2")
        self.quick_btn.pack(side="left")
        tk.Label(self.quick_row, text="点选模板直接出句，不花钱", fg="#4a5160", bg="#0d0d12",
                 font=(C_FONT, 7)).pack(side="left", padx=6)


    def _open_quick(self):
        if callable(self.on_quick):
            self.on_quick()
        else:
            self.set_hint("快捷组句不可用（chatgen.py 缺失）", "#ff8f6b")

    def _submit_reply(self, e=None):
        txt = (self.reply_var.get() or "").strip()
        if not txt:
            return "break"
        if txt == self._last_en:            # 已译过的结果，再回车只复制，不重复花钱
            self.copy_reply()
            return "break"
        if callable(self.on_reply):
            self.set_hint("翻译中…", "#7f8a9a")
            self.on_reply(txt)
        return "break"

    def set_hint(self, text, color="#5d6470"):
        try:
            self.reply_hint.configure(text=text, fg=color)
        except Exception:
            pass

    def set_reply(self, en, ok=True):
        """把译好的英文回填输入框并全选，方便直接 Ctrl+C 或手动再改。"""
        if ok and en:
            self._last_en = en
            self.reply_var.set(en)
            self.reply_entry.selection_range(0, "end")
            self.reply_entry.icursor("end")
            self.set_hint("已复制 · 游戏里 Enter 开聊天框 → Ctrl+V → Enter 发送", "#00e0a0")
        elif en:
            self._last_en = ""
            self.reply_var.set("")
            self.set_hint(en[:70], "#ff8f6b")
        else:
            self._last_en = ""
            self.set_hint("没译出来，换个说法再回车", "#ff8f6b")

    def _on_wheel(self, e):
        if e.state & 0x0004:                     # Ctrl+滚轮 = 字号
            self.bump_font(1 if e.delta > 0 else -1)
        else:                                    # 滚轮 = 透明度
            cur = float(self.cfg["overlay"].get("alpha", 0.9))
            self.set_alpha(max(0.35, min(1.0, cur + (0.05 if e.delta > 0 else -0.05))))
        return "break"

    def copy_reply(self):
        txt = (self.reply_var.get() or "").strip()
        if txt and set_clipboard(txt):
            self.set_hint("已复制 · 游戏里 Ctrl+V 发送", "#00e0a0")

    def _apply_tags(self):
        fs = int(self.cfg["overlay"].get("font_size", 11))
        self.text.tag_configure("src", foreground="#7f8a9a",
                                font=(C_FONT, max(7, fs - 2)))
        self.text.tag_configure("dst", foreground="#f2f4f7",
                                font=(C_FONT, fs))
        self.text.tag_configure("gap", foreground="#0d0d12",
                                font=(C_FONT, 3))

    def _drag_start(self, e):
        self._drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _drag_move(self, e):
        x = e.x_root - self._drag[0]
        y = e.y_root - self._drag[1]
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        ww = max(200, self.win.winfo_width())
        # 顶栏必须留在屏内，左右至少留 120px 方便再次抓取
        x = max(120 - ww, min(x, sw - 120))
        y = max(0, min(y, sh - 32))
        self.win.geometry("+%d+%d" % (x, y))
        self.cfg["overlay"]["x"] = x
        self.cfg["overlay"]["y"] = y

    def _popup(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def set_alpha(self, v):
        self.win.attributes("-alpha", float(v))
        self.cfg["overlay"]["alpha"] = float(v)
        self.on_save()

    def bump_font(self, delta):
        fs = int(self.cfg["overlay"].get("font_size", 11)) + delta
        fs = max(8, min(20, fs))
        self.cfg["overlay"]["font_size"] = fs
        self._apply_tags()
        self.on_save()

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def hide(self):
        self.win.withdraw()

    def show(self):
        self.win.deiconify()
        self.win.attributes("-topmost", True)

    def push(self, src, dst):
        self.text.configure(state="normal")
        self.text.insert("end", src + "\n", "src")
        if dst:
            self.text.insert("end", dst + "\n", "dst")
        self.text.insert("end", "\n", "gap")

        total = int(self.text.index("end-1c").split(".")[0])
        limit = int(self.cfg["overlay"].get("max_lines", 12)) * 2 + 8
        if total > limit:
            self.text.delete("1.0", "%d.0" % (total - limit + 1))

        self.text.see("end")
        self.text.configure(state="disabled")


# ============================================================
# 主窗口
# ============================================================
# ============================================================
# 全局热键（Ctrl+Alt+S 开关 / R 快译 / Q 组句 / H 悬浮窗 / M 主窗）
# ============================================================
class HotkeyWatcher(threading.Thread):
    MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY = 0x0001, 0x0002, 0x4000, 0x0312

    def __init__(self, q):
        super().__init__(daemon=True)
        self.q = q
        # name: (候选虚拟键, 说明)；同一功能可给多个候选键，占用时自动降级
        self.mapping = {
            1: ((0x53,), "toggle"),           # Ctrl+Alt+S 开始/停止
            2: ((0x44, 0x47), "reply"),       # Ctrl+Alt+D（备选 Ctrl+Alt+G）快译
            3: ((0x51,), "quick"),            # Ctrl+Alt+Q 快捷组句
            4: ((0x48,), "overlay"),          # Ctrl+Alt+H 悬浮窗显隐
            5: ((0x4D,), "main"),             # Ctrl+Alt+M 主窗显隐
        }
        self.labels = {"toggle": "S", "reply": "D", "quick": "Q", "overlay": "H", "main": "M"}

    # macOS 虚拟键码（Carbon kVK_*）
    MAC_KEYS = (
        ("toggle",  (0x01,),      "S"),   # kVK_ANSI_S
        ("reply",   (0x02, 0x05), "D"),   # kVK_ANSI_D / kVK_ANSI_G 备选
        ("quick",   (0x0C,),      "Q"),   # kVK_ANSI_Q
        ("overlay", (0x04,),      "H"),   # kVK_ANSI_H
        ("main",    (0x2E,),      "M"),   # kVK_ANSI_M
    )
    MAC_CTRL = (0x3B, 0x3E)               # 左右 control
    MAC_ALT = (0x3A, 0x3D)                # 左右 option

    def run(self):
        if IS_MAC:
            return self._run_mac()
        try:
            from ctypes import wintypes
            u = ctypes.windll.user32
            ok = set()
            for hid, (vks, name) in self.mapping.items():
                for vk in vks:
                    if u.RegisterHotKey(None, hid, self.MOD_CONTROL | self.MOD_ALT | self.MOD_NOREPEAT, vk):
                        ok.add(name)
                        self.labels[name] = chr(vk)
                        break
            self.q.put(("log", "全局热键就绪：" + MOD_NAME + "+%s 开关 / %s 快译 / %s 组句 / %s 悬浮窗 / %s 主窗"
                        % (self.labels["toggle"], self.labels["reply"], self.labels["quick"],
                           self.labels["overlay"], self.labels["main"])))
            missing = [n for n in self.labels if n not in ok]
            if missing:
                self.q.put(("log", "这些热键被别的软件占了：%s，用界面按钮同样可以操作" % "、".join(missing)))
            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == self.WM_HOTKEY:
                    name = self.mapping.get(int(msg.wParam), ((), ""))[1]
                    if name:
                        self.q.put(("hotkey", name))
        except Exception as e:
            self.q.put(("log", "热键不可用：%s" % e))

    def _run_mac(self):
        """
        macOS 全局热键：轮询 Quartz 的 HID 键状态，不再使用 pynput。

        为什么弃用 pynput：它的键码翻译在监听线程里通过 ctypes 调 Carbon 的
        TISCopyCurrentKeyboardInputSource / TISGetInputSourceProperty，而 HIToolbox 的
        islGetInputSourceListWithAdditions 断言必须主队列执行，子线程一调就
        _dispatch_assert_queue_fail -> SIGTRAP（EXC_BREAKPOINT）整进程闪退。
        CGEventSourceKeyState 是纯状态查询：无线程亲和性、不触发 TSM，任意线程安全。
        """
        try:
            import Quartz
        except Exception as e:
            self.q.put(("log", "热键不可用：需要 pyobjc-framework-Quartz，%s" % e))
            return
        try:
            hid = Quartz.kCGEventSourceStateHIDSystemState
        except Exception:
            hid = 1

        def down(kc):
            try:
                return bool(Quartz.CGEventSourceKeyState(hid, kc))
            except Exception:
                return False

        def ctrl_alt():
            if not any(down(k) for k in self.MAC_CTRL):
                return False
            return any(down(k) for k in self.MAC_ALT)

        prev = {}
        self.q.put(("log", "全局热键就绪：" + MOD_NAME + "+S 开关 / D 快译 / Q 组句 / H 悬浮窗 / M 主窗"))
        self.q.put(("log", "首次使用请在 系统设置 → 隐私与安全性 → 辅助功能 里勾选本程序，否则热键无响应"))
        try:
            while True:
                base = ctrl_alt()
                for name, vks, _label in self.MAC_KEYS:
                    cur = bool(base) and any(down(k) for k in vks)
                    if cur and not prev.get(name, False):
                        self.q.put(("hotkey", name))
                    prev[name] = cur
                time.sleep(0.05)
        except Exception as e:
            self.q.put(("log", "热键不可用：%s" % e))


class App:
    def __init__(self):
        self.cfg = load_config()
        self.q = queue.Queue()
        self.engine = None
        self.watch_engine = None
        self.watch_store = None
        self.watch_note = ""
        self.watch_last = {"ts": 0, "exp": None, "hp": None, "mp": None}
        self.watch_pop = {"exp": 0, "meso": 0, "total_exp": 0, "total_meso": 0,
                          "span_min": 0.0, "exp_per_min": 0.0, "meso_per_hour": 0.0}

        self.root = tk.Tk()
        self.root.title("MapleChat · 冒险岛国际服聊天翻译")
        self._apply_geometry()
        self.root.minsize(520, 460)
        init_pixel_scale(self.root)
        self.root.configure(bg=C_BG)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._init_style()

        self.reply_tr = None
        self.reply_busy = False
        self.overlay = Overlay(self.root, self.cfg, save_config, on_reply=self.queue_reply,
                               on_quick=self.open_quickgen)
        self._build_ui()
        self.root.after(120, self._poll)

        if self.cfg.get("hotkeys", True):
            try:
                HotkeyWatcher(self.q).start()
            except Exception:
                pass
        if self.cfg.get("start_minimized"):
            self.root.after(260, self.root.withdraw)
        if self.cfg.get("auto_start") and self.cfg.get("region") and self.cfg.get("api_key"):
            self.root.after(900, self._auto_start)

    # ---------- 窗口尺寸 ----------
    def _apply_geometry(self):
        """按当前屏幕自适应，记忆尺寸超屏时自动收敛。"""
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = min(760, max(520, sw - 120))
        h = min(1040, max(460, sh - 160))
        m = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", self.cfg.get("win_geometry") or "")
        if m:
            w = min(int(m.group(1)), w)
            h = min(int(m.group(2)), h)
            x = max(0, min(int(m.group(3)), max(0, sw - w)))
            y = max(0, min(int(m.group(4)), max(0, sh - h - 40)))
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        else:
            self.root.geometry("%dx%d" % (w, h))

    # ---------- 样式 ----------
    def _init_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=C_BG, foreground=C_TXT, font=(C_FONT, 9))
        st.configure("TButton", background=C_CARD2, foreground=C_TXT, borderwidth=0,
                     focusthickness=0, padding=(13, 8), font=(C_FONT, 9))
        st.map("TButton",
               background=[("active", "#252b36"), ("pressed", "#2d3543"), ("disabled", "#171b23")],
               foreground=[("disabled", "#4b5364")])
        st.configure("Accent.TButton", background=C_ACCENT, foreground="#04120c",
                     font=(C_FONT, 9, "bold"), padding=(16, 8))
        st.map("Accent.TButton",
               background=[("active", "#2af3b8"), ("pressed", "#00c78f"), ("disabled", "#16332b")],
               foreground=[("disabled", "#5b7a70")])
        st.configure("Stop.TButton", background="#3a2228", foreground="#ff9c9c",
                     font=(C_FONT, 9, "bold"), padding=(16, 8))
        st.map("Stop.TButton",
               background=[("active", "#4d2b33"), ("pressed", "#5c333c"), ("disabled", "#20242d")],
               foreground=[("active", "#ffbdbd"), ("disabled", "#5a606e")])
        st.configure("TCheckbutton", background=C_CARD, foreground=C_TXT, font=(C_FONT, 9))
        st.map("TCheckbutton", background=[("active", C_CARD)], foreground=[("active", C_TXT)])
        st.configure("TCombobox", fieldbackground=C_CARD2, background=C_CARD2, foreground=C_TXT,
                     arrowcolor=C_MUTED, borderwidth=0, padding=4)
        st.map("TCombobox",
               fieldbackground=[("readonly", C_CARD2)], foreground=[("readonly", C_TXT)])
        st.configure("TScrollbar", background=C_CARD2, troughcolor=C_BG,
                     borderwidth=0, arrowcolor=C_MUTED)
        st.map("TScrollbar", background=[("active", "#31394a")])
        self.root.option_add("*TCombobox*Listbox.background", C_CARD2)
        self.root.option_add("*TCombobox*Listbox.foreground", C_TXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", C_ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#04120c")

    def _card(self, title, sub="", accent=None, collapsible=False, start_open=True):
        accent = accent or C_ACCENT
        outer = tk.Frame(self.wrap, bg=C_CARD)
        head = tk.Frame(outer, bg=C_CARD)
        head.pack(fill="x", padx=14, pady=(11, 5))
        bar = tk.Frame(head, bg=accent, width=3, height=14)
        bar.pack(side="left", padx=(0, 9))
        bar.pack_propagate(False)
        ttl = tk.Label(head, text=title, bg=C_CARD, fg=C_TXT, font=(C_FONT, 10, "bold"))
        ttl.pack(side="left")
        if sub:
            tk.Label(head, text=sub, bg=C_CARD, fg=C_MUTED, font=(C_FONT, 8)).pack(side="left", padx=9)
        arrow = None
        if collapsible:
            arrow = tk.Label(head, text=("\u25be" if start_open else "\u25b8"), bg=C_CARD,
                             fg=C_MUTED, font=(C_FONT, 9))
            arrow.pack(side="right", padx=6)
        body = tk.Frame(outer, bg=C_CARD)
        if start_open:
            body.pack(fill="x", padx=14, pady=(0, 12))

        def _toggle(e=None):
            if not collapsible:
                return
            if body.winfo_ismapped():
                body.pack_forget()
                arrow.configure(text="\u25b8")
            else:
                body.pack(fill="x", padx=14, pady=(0, 12))
                arrow.configure(text="\u25be")

        if collapsible:
            for w in (head, ttl, arrow):
                w.configure(cursor="hand2")
                w.bind("<Button-1>", _toggle)
        outer.pack(fill="x", padx=10, pady=4)
        return body

    def _entry(self, parent, var, show=""):
        return tk.Entry(parent, textvariable=var, show=show, bg=C_CARD2, fg=C_TXT,
                        insertbackground=C_ACCENT, relief="flat", bd=0,
                        highlightthickness=1, highlightbackground=C_LINE,
                        highlightcolor=C_ACCENT, font=(C_FONT, 9))

    # ---------- UI ----------
    def _build_ui(self):
        self.status_var = tk.StringVar(value="就绪 · 等你说开始")
        # 顶部品牌条
        shell = tk.Frame(self.root, bg=C_BG)
        shell.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(shell, bg=C_BG, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(shell, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self.wrap = tk.Frame(self._canvas, bg=C_BG)
        self._cwin = self._canvas.create_window((0, 0), window=self.wrap, anchor="nw")
        def _on_wrap(e):
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
            try:
                self._canvas.itemconfigure(
                    self._cwin, height=max(self.wrap.winfo_reqheight(),
                                           self._canvas.winfo_height()))
            except Exception:
                pass

        self.wrap.bind("<Configure>", _on_wrap)
        def _sync_win(e):
            self._canvas.itemconfigure(
                self._cwin, width=e.width,
                height=max(self.wrap.winfo_reqheight(), e.height))

        self._canvas.bind("<Configure>", _sync_win)
        self.root.bind_all("<MouseWheel>", self._on_main_wheel)

        tk.Label(self.wrap,
                 text="Ctrl+Alt+S 开关 · D 快译 · Q 组句 · H 悬浮窗 · M 主窗    |    "
                      "悬浮窗：双击复制、滚轮调透明度、Ctrl+滚轮调字号",
                 bg=C_BG, fg=C_DIM, font=(C_FONT, 8), wraplength=700,
                 justify="left").pack(side="bottom", fill="x", padx=16, pady=(6, 10))

        head = tk.Frame(self.wrap, bg=C_BG)
        head.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(head, text="MapleChat", bg=C_BG, fg=C_TXT,
                 font=(C_FONT, 16, "bold")).pack(side="left")
        tk.Label(head, text="冒险岛国际服 · 聊天翻译", bg=C_BG, fg=C_MUTED,
                 font=(C_FONT, 8)).pack(side="left", padx=(9, 0), pady=(6, 0))
        tk.Label(head, text="Ctrl+Alt+S 开关 · Ctrl+Alt+D 快译", bg=C_BG, fg=C_DIM,
                 font=(C_FONT, 8)).pack(side="right")
        self.lamp = tk.Label(head, text="\u25cf", bg=C_BG, fg=C_DIM, font=(C_FONT, 12))
        self.lamp.pack(side="right", padx=(0, 8))

        # 1. 控制
        b = self._card("控制", "点一下就能跑，热键也行", accent=C_ACCENT)
        row = tk.Frame(b, bg=C_CARD)
        row.pack(fill="x")
        self.btn_start = ttk.Button(row, text="\u25b6  开始翻译", style="Accent.TButton", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(row, text="\u25a0  停止", style="Stop.TButton",
                                   command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        ttk.Button(row, text="悬浮窗显隐", command=self.toggle_overlay).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="清空", command=self.overlay.clear).pack(side="left")

        info = tk.Frame(b, bg=C_CARD)
        info.pack(fill="x", pady=(9, 0))
        self.status_lbl = tk.Label(info, textvariable=self.status_var, bg=C_CARD, fg=C_ACCENT,
                                   anchor="w", justify="left", font=(C_FONT, 9))
        self.status_lbl.pack(side="left")
        self.region_lbl = tk.Label(info, text=self._region_text(), bg=C_CARD, fg=C_MUTED,
                                   anchor="w", font=(C_FONT, 8))
        self.region_lbl.pack(side="left", padx=(14, 0))
        ttk.Button(info, text="重新框选", command=self.pick_region).pack(side="right")

        # 2. 打字回复（最高频，前置）
        b = self._card("打字回复", "打中文 \u2192 回车 \u2192 自动译成英文并复制", accent=C_BLUE)
        self.reply_in = tk.Text(b, height=2, wrap="word", bg=C_CARD2, fg=C_TXT,
                                insertbackground=C_ACCENT, relief="flat", bd=0,
                                highlightthickness=1, highlightbackground=C_LINE,
                                highlightcolor=C_ACCENT, font=(C_FONT, 10), padx=9, pady=6)
        self.reply_in.pack(fill="x")
        self.reply_in.bind("<Control-Return>", self._on_reply_enter)
        self.reply_in.bind("<Return>", self._on_reply_enter)
        rr = tk.Frame(b, bg=C_CARD)
        rr.pack(fill="x", pady=(7, 0))
        ttk.Button(rr, text="译成英文并复制", style="Accent.TButton",
                   command=self.reply_from_main).pack(side="left")
        ttk.Button(rr, text="再复制一次", command=self.overlay.copy_reply).pack(side="left", padx=8)
        tk.Label(rr, text="回车直接发 · Shift+回车换行", bg=C_CARD, fg=C_DIM,
                 font=(C_FONT, 8)).pack(side="left", padx=4)
        self.reply_out = tk.Label(b, text="译好的英文会出现在这里，同时自动进剪贴板",
                                  bg=C_CARD, fg=C_MUTED, anchor="w", justify="left",
                                  wraplength=560, font=(C_FONT, 9))
        self.reply_out.pack(fill="x", pady=(7, 0))

        # 3. 快捷组句
        b = self._card("快捷组句", "交易 / 组队 / 带练 / 频道 / 技能，一键出地道英文（0 成本 0 延迟）",
                       accent=C_WARN)
        qrow = tk.Frame(b, bg=C_CARD)
        qrow.pack(fill="x")
        ttk.Button(qrow, text="打开快捷组句面板", command=self.open_quickgen).pack(side="left")
        tk.Label(qrow, text="Ctrl+Alt+Q", bg=C_CARD, fg=C_DIM,
                 font=(C_FONT, 8)).pack(side="left", padx=9)

        # 5. 数据看板（只读画面读数，纯本地、不注入游戏）
        self._build_watch_card()

        # 6. 翻译设置（默认收起，不挡眼）
        b = self._card("翻译设置", "API Key / 模型 / 术语库", accent=C_MUTED,
                       collapsible=True, start_open=False)
        g = tk.Frame(b, bg=C_CARD)
        g.pack(fill="x")

        def lab(txt, r, c, pad=(0, 8)):
            tk.Label(g, text=txt, bg=C_CARD, fg=C_MUTED, font=(C_FONT, 9)).grid(
                row=r, column=c, sticky="e", padx=pad, pady=4)

        lab("API Key", 0, 0, (0, 8))
        self.key_var = tk.StringVar(value=self.cfg.get("api_key", ""))
        e = self._entry(g, self.key_var, show="*")
        e.grid(row=0, column=1, columnspan=3, sticky="we", pady=4)
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(g, text="显示", variable=self.show_key,
                        command=lambda: e.configure(show="" if self.show_key.get() else "*")
                        ).grid(row=0, column=4, padx=(6, 0))

        lab("接口地址", 1, 0)
        self.base_var = tk.StringVar(value=self.cfg.get("api_base", ""))
        self._entry(g, self.base_var).grid(row=1, column=1, columnspan=3, sticky="we", pady=4)

        lab("模型", 2, 0)
        self.model_var = tk.StringVar(value=self.cfg.get("model", "deepseek-chat"))
        ttk.Combobox(g, textvariable=self.model_var, width=18,
                     values=["deepseek-chat", "deepseek-reasoner", "gpt-4o-mini", "qwen-plus"]
                     ).grid(row=2, column=1, sticky="w", pady=4)
        lab("每帧间隔", 2, 2)
        self.interval_var = tk.StringVar(value=str(self.cfg.get("interval_ms", 500)))
        ttk.Combobox(g, textvariable=self.interval_var, width=6,
                     values=["300", "400", "500", "700", "1000"]).grid(row=2, column=3, sticky="w")
        g.columnconfigure(1, weight=1)

        row3 = tk.Frame(b, bg=C_CARD)
        row3.pack(fill="x", pady=(10, 0))
        ttk.Button(row3, text="保存设置", command=self.save_settings).pack(side="left")
        ttk.Button(row3, text="测试翻译", command=self.test_translate).pack(side="left", padx=8)
        ttk.Button(row3, text="编辑术语库", command=self.open_terms).pack(side="left")

        self.terms_var = tk.BooleanVar(value=bool(self.cfg.get("terms_enabled", True)))
        ttk.Checkbutton(b, text="启用术语库", variable=self.terms_var,
                        command=self.toggle_terms).pack(anchor="w", pady=(10, 2))
        _tf, _tm = load_terms()
        self.terms_lbl = tk.Label(
            b, text=("术语库 %d 条 / %d 组" % (_tm["count"], _tm["groups"])) if _tf else "术语库未找到（terms.json）",
            bg=C_CARD, fg=(C_MUTED if _tf else C_ERR), font=(C_FONT, 8))
        self.terms_lbl.pack(anchor="w")

        # 6. 运行日志（可收起）
        b = self._card("运行日志", "最近 600 行 · 点标题展开", accent=C_MUTED,
                       collapsible=True, start_open=False)
        lg = tk.Frame(b, bg="#0d1016")
        lg.pack(fill="x")
        self.log = tk.Text(lg, height=7, bg="#0d1016", fg="#9aa6b8", relief="flat", bd=0,
                           font=(FONT_MONO, 9), padx=9, pady=6, insertbackground=C_ACCENT,
                           highlightthickness=0)
        sb = ttk.Scrollbar(lg, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.configure(state="disabled")



    # ---------- 便捷交互 ----------
    def _auto_start(self):
        try:
            self.start()
        except Exception as e:
            self._log("自动开始失败：%s" % e)

    def _on_reply_enter(self, e=None):
        self.reply_from_main(e)
        return "break"

    def _on_main_wheel(self, e):
        try:
            w = self.root.winfo_containing(e.x_root, e.y_root)
        except Exception:
            w = None
        while w is not None:
            if isinstance(w, (tk.Text, tk.Listbox)) or w.winfo_class() in ("TCombobox", "Text", "Listbox"):
                return
            if w is self._canvas:
                break
            w = getattr(w, "master", None)
        try:
            self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        except Exception:
            pass

    def toggle_overlay(self):
        try:
            if self.overlay.win.state() == "withdrawn":
                self.overlay.show()
            else:
                self.overlay.hide()
        except Exception:
            self.overlay.show()

    def _on_hotkey(self, name):
        if name == "toggle":
            if str(self.btn_stop["state"]) == "normal":
                self.stop()
            else:
                self.start()
        elif name == "reply":
            txt = self.reply_in.get("1.0", "end").strip()
            if not txt:
                try:
                    txt = self.root.clipboard_get().strip()
                except Exception:
                    txt = ""
            if txt:
                self.do_reply(txt)
                self.status_var.set("快译：%s" % txt[:24])
            else:
                self.status_var.set("没有可翻译的中文（先复制一段中文）")
        elif name == "quick":
            self.open_quickgen()
        elif name == "overlay":
            self.toggle_overlay()
        elif name == "main":
            if self.root.state() == "withdrawn":
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
            else:
                self.root.withdraw()

    # ---------- 打字回复：中文 -> 英文 ----------
    def open_quickgen(self):
        """打开场景化组句窗（单例，重复点只置顶）。"""
        if chatgen is None:
            messagebox.showwarning("快捷组句不可用", "同目录缺少 chatgen.py，请重新解压完整程序目录。")
            return
        win = getattr(self, "_quick_win", None)
        if win is not None:
            try:
                win.win.deiconify()
                win.win.lift()
                win.win.focus_force()
                return
            except Exception:
                self._quick_win = None
        try:
            flat, _meta = load_terms()
            qg = chatgen.QuickGen(self.root, flat, set_clipboard, on_log=self._log)
            self._quick_win = qg
            qg.win.protocol("WM_DELETE_WINDOW", lambda: self._close_quick())
            if callable(getattr(self.overlay, "set_hint", None)):
                self.overlay.set_hint("快捷组句已打开，点选即出句", "#00e0a0")
        except Exception as ex:
            self._quick_win = None
            messagebox.showerror("快捷组句启动失败", str(ex))
            self._log("快捷组句启动失败：" + str(ex))

    def _close_quick(self):
        try:
            if self._quick_win is not None:
                self._quick_win.win.destroy()
        except Exception:
            pass
        self._quick_win = None

    def reply_from_main(self, e=None):
        txt = self.reply_in.get("1.0", "end").strip()
        if txt:
            self.do_reply(txt)
        return "break"

    def queue_reply(self, text):
        self.q.put(("reply_req", text))

    def do_reply(self, text):
        text = (text or "").strip()
        if not text:
            return
        if not self.cfg.get("api_key"):
            self.overlay.set_reply("", False)
            self.overlay.set_hint("未配置 API Key，先在第 2 步填 Key", "#ff8f6b")
            self._log("回复失败：未配置 API Key")
            return
        if self.reply_busy:
            return
        self.reply_busy = True

        def work():
            try:
                if self.reply_tr is None:
                    self.reply_tr = Translator(self.cfg)
                out = self.reply_tr.reverse(text)
            except Exception as ex:
                out = "__ERR__%s" % ex
            self.q.put(("reply_done", (text, out)))

        threading.Thread(target=work, daemon=True).start()

    def _show_reply(self, zh, out):
        ok = bool(out) and not out.startswith("__ERR__")
        if ok:
            set_clipboard(out)
            self.overlay.set_reply(out, True)
            self.reply_out.configure(text="→ " + out + "    （已复制，切回游戏 Ctrl+V）", fg=C_ACCENT)
            self.status_var.set("回复已复制，切回游戏 Ctrl+V 发送")
            self._log("回复 %s  ->  %s" % (zh[:24], out[:70]))
        else:
            msg = out[7:] if out.startswith("__ERR__") else "没译出来，换个说法"
            self.overlay.set_reply("", False)
            self.overlay.set_hint(msg[:70], "#ff8f6b")
            self.reply_out.configure(text="× " + msg, fg=C_ERR)
            self._log("回复失败：" + msg)

    def _region_text(self):
        r = self.cfg.get("region")
        if not r:
            return "未设置 —— 请先框选游戏聊天区域"
        return "已设置：左 %d  上 %d  宽 %d  高 %d" % tuple(r)

    def _log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", "[%s] %s\n" % (ts, msg))
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 600:
            self.log.delete("1.0", "100.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------- 行为 ----------
    # ---------- 数据看板 ----------
    def _build_watch_card(self):
        wcfg = self.cfg.setdefault("watch", {})
        b = self._card("数据看板",
                       "OCR 直接读画面数字（HP / MP / EXP / 等级）· 纯本地，不注入游戏",
                       accent=C_WARN, collapsible=True, start_open=True)

        self.watch_last = {"ts": 0, "level": None, "exp_abs": None, "exp_pct": None,
                           "meso": None,
                           "hp": None, "hp_max": None, "mp": None, "mp_max": None,
                           "raw": ""}
        self.watch_info = {}
        self.watch_total = None
        self._lv_seen = None
        self.watch_exp_per_min = None
        self.watch_meso_per_min = None

        row = tk.Frame(b, bg=C_CARD)
        row.pack(fill="x", pady=(0, 7))
        tk.Label(row, text="等级", bg=C_CARD, fg=C_MUTED, font=(C_FONT, 9)).pack(side="left")
        self.level_var = tk.IntVar(value=int(wcfg.get("level", 1) or 1))
        # 等级完全由截图 OCR 识别，不再提供手动 +/- 与「升级了」按钮
        self.w_lv = tk.StringVar(value="Lv. --")
        tk.Label(row, textvariable=self.w_lv, bg=C_CARD, fg=C_ACCENT,
                 font=(C_FONT, 14, "bold")).pack(side="left", padx=(6, 10))
        tk.Label(row, text="状态条", bg=C_CARD, fg=C_MUTED,
                 font=(C_FONT, 9)).pack(side="left", padx=(16, 4))
        self.w_interval = tk.StringVar(value=str(int(wcfg.get("stat_interval_ms", wcfg.get("interval_ms", 2000)))))
        cb = ttk.Combobox(row, textvariable=self.w_interval, width=5, state="readonly",
                          values=("1000", "1500", "2000", "3000", "5000", "8000"))
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._watch_apply_interval())
        tk.Label(row, text="ms", bg=C_CARD, fg=C_DIM,
                 font=(C_FONT, 8)).pack(side="left", padx=(3, 4))
        self.w_lamp = tk.Label(row, text="\u25cf", bg=C_CARD, fg=C_DIM, font=(C_FONT, 12))
        self.w_lamp.pack(side="right")
        row2 = tk.Frame(b, bg=C_CARD)
        row2.pack(fill="x", pady=(0, 7))
        tk.Label(row2, text="飘字间隔", bg=C_CARD, fg=C_MUTED,
                 font=(C_FONT, 9)).pack(side="left")
        self.w_popiv = tk.StringVar(value=str(int(wcfg.get("pop_interval_ms", 1000))))
        cb2 = ttk.Combobox(row2, textvariable=self.w_popiv, width=5, state="readonly",
                           values=("600", "1000", "1500", "2000", "3000"))
        cb2.pack(side="left", padx=(6, 0))
        cb2.bind("<<ComboboxSelected>>", lambda e: self._watch_apply_interval())
        tk.Label(row2, text="ms", bg=C_CARD, fg=C_DIM,
                 font=(C_FONT, 8)).pack(side="left", padx=(3, 0))
        tk.Label(row2, text="引擎", bg=C_CARD, fg=C_MUTED,
                 font=(C_FONT, 9)).pack(side="left", padx=(16, 4))
        self.w_engine = tk.StringVar(
            value={"auto": "自动", "winrt": "系统OCR", "rapid": "高精度"}.get(
                str(wcfg.get("ocr_engine", "auto")), "自动"))
        cb3 = ttk.Combobox(row2, textvariable=self.w_engine, width=8, state="readonly",
                           values=("自动", "系统OCR", "高精度"))
        cb3.pack(side="left")
        cb3.bind("<<ComboboxSelected>>", lambda e: self._watch_apply_ocr())
        self.w_lowcpu = tk.BooleanVar(value=bool(wcfg.get("low_cpu", False)))
        tk.Checkbutton(row2, text="低配模式", variable=self.w_lowcpu, bg=C_CARD,
                       fg=C_MUTED, selectcolor=C_BG, activebackground=C_CARD,
                       activeforeground=C_MUTED, font=(C_FONT, 8), bd=0,
                       highlightthickness=0,
                       command=self._watch_apply_lowcpu).pack(side="left", padx=(10, 0))
        tk.Label(row2, text="飘字越短漏得越少", bg=C_CARD, fg=C_DIM,
                 font=(C_FONT, 8)).pack(side="right")

        # —— 四个独立短框并排：HP / MP / EXP / 金币，各自单独框选、单独识别 ——
        boxes = tk.Frame(b, bg=C_CARD)
        boxes.pack(fill="x", pady=(0, 6))
        self.w_hp_text = tk.StringVar(value="--")
        self.w_mp_text = tk.StringVar(value="--")
        self.w_exp_text = tk.StringVar(value="--")
        self.w_meso_text = tk.StringVar(value="--")

        def _short_box(col, cap, var, fg, bar=True):
            f = tk.Frame(boxes, bg=C_CARD)
            f.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 6, 0))
            tk.Label(f, text=cap, bg=C_CARD, fg=C_MUTED,
                     font=(C_FONT, 8), anchor="w").pack(fill="x")
            tk.Label(f, textvariable=var, bg=C_CARD, fg=fg,
                     font=(C_FONT, 11 if bar else 13, "bold"),
                     anchor="w").pack(fill="x")
            if not bar:
                return None
            cv = tk.Canvas(f, height=7, bg=C_BG, highlightthickness=0)
            cv.pack(fill="x", pady=(3, 0))
            return cv

        for _i in range(4):
            boxes.columnconfigure(_i, weight=1, uniform="wb")
        self.w_bar_hp = _short_box(0, "HP", self.w_hp_text, C_ERR)
        self.w_bar_mp = _short_box(1, "MP", self.w_mp_text, C_BLUE)
        self.w_bar_exp = _short_box(2, "EXP", self.w_exp_text, C_ACCENT)
        # 金币框只显示数字，不再挂进度条（数字用「万」为单位，字号放大）
        self.w_bar_meso = _short_box(3, "金币", self.w_meso_text, C_WARN, bar=False)

        self.w_eta = tk.Label(b, text="经验获取：--/分钟 · 预估 --/小时",
                              bg=C_CARD, fg=C_ACCENT, font=(C_FONT, 9), anchor="w",
                              justify="left", wraplength=620)
        self.w_eta.pack(fill="x", pady=(0, 3))

        self.w_pop = tk.Label(b, text="金币收入：--/分钟 · 预估 --/小时",
                              bg=C_CARD, fg=C_WARN, font=(C_FONT, 9), anchor="w",
                              justify="left", wraplength=620)
        self.w_pop.pack(fill="x", pady=(0, 6))

        rg = tk.Frame(b, bg=C_CARD)
        rg.pack(fill="x", pady=(0, 5))
        for _txt, _key, _w in (("框选HP", "hp", 8), ("框选MP", "mp", 8),
                               ("框选EXP", "exp", 9), ("框选金币", "meso", 10),
                               ("框选等级", "level", 10)):
            ttk.Button(rg, text=_txt, width=_w,
                       command=lambda k=_key: self._watch_pick(k)
                       ).pack(side="left", padx=(0, 5))
        ttk.Button(rg, text="框选整条状态栏", width=14,
                   command=lambda: self._watch_pick("stat")).pack(side="left", padx=(0, 5))
        ttk.Button(rg, text="框选飘字区", width=11,
                   command=lambda: self._watch_pick("pop")).pack(side="left")

        ct = tk.Frame(b, bg=C_CARD)
        ct.pack(fill="x")
        self.w_btn_start = ttk.Button(ct, text="\u25b6 开始监控", style="Accent.TButton",
                                      command=self._watch_start)
        self.w_btn_start.pack(side="left", padx=(0, 6))
        self.w_btn_stop = ttk.Button(ct, text="\u25a0 停止", style="Stop.TButton",
                                     command=self._watch_stop, state="disabled")
        self.w_btn_stop.pack(side="left", padx=(0, 6))
        self.w_btn_preview = ttk.Button(ct, text="预览抓图", command=self._watch_preview)
        self.w_btn_preview.pack(side="left", padx=(0, 6))
        ttk.Button(ct, text="导出抓图", command=self._watch_dump).pack(side="left", padx=(0, 6))
        ttk.Button(ct, text="清空记录", command=self._watch_clear).pack(side="left")

        self.w_state = tk.Label(b, text=self._watch_region_text(), bg=C_CARD, fg=C_DIM,
                                font=(C_FONT, 8), anchor="w", justify="left")
        self.w_state.pack(fill="x", pady=(6, 0))
        tk.Label(b, text="框选要点：四个短框各自只框数字本身（宽 90~150、高 24~40 像素）最准；"
                         "图省事也可以只框「整条状态栏」一次读出 HP/MP/EXP。",
                 bg=C_CARD, fg=C_DIM, font=(C_FONT, 8), anchor="w",
                 wraplength=620, justify="left").pack(fill="x", pady=(2, 0))

        for cv in (self.w_bar_hp, self.w_bar_mp, self.w_bar_exp, self.w_bar_meso):
            if cv is not None:
                cv.bind("<Configure>", lambda e: self._watch_redraw())
        self._watch_redraw()

        if wcfg.get("enabled") and datawatch is not None \
                and datawatch.WatchEngine.regions_of(wcfg):
            self.root.after(1600, self._watch_start)

    def _watch_region_text(self):
        w = self.cfg.get("watch", {})
        names = (("hp_region", "HP"), ("mp_region", "MP"), ("exp_region", "EXP"),
                 ("meso_region", "金币"), ("level_region", "等级"),
                 ("pop_region", "飘字区"), ("stat_region", "整条状态栏"))
        parts = []
        for key, name in names:
            r = w.get(key)
            if r:
                parts.append("%s %dx%d" % (name, int(r[2]), int(r[3])))
            else:
                parts.append("%s 未框选" % name)
        return "区域：" + " · ".join(parts) if parts else "区域：未框选"

    def _watch_draw(self, cv, ratio, color, text):
        try:
            cv.delete("all")
            w = max(80, cv.winfo_width())
            h = max(16, cv.winfo_height())
            cv.create_rectangle(0, 0, w, h, fill=C_BG, outline="")
            r = 0.0 if ratio is None else max(0.0, min(1.0, float(ratio)))
            if r > 0:
                cv.create_rectangle(0, 0, max(2, w * r), h, fill=color, outline="")
            fg = "#0b0d11" if r >= 0.58 else C_TXT
            cv.create_text(w // 2, h // 2, text=text, fill=fg, font=(C_FONT, 9))
        except Exception:
            pass

    def _watch_redraw(self):
        d = self.watch_last or {}
        def _fmt(v):
            try:
                return "{:,}".format(int(v))
            except Exception:
                return "--"
        def _wan(v):
            """数字过长时改用「万」为单位，例如 84,000 -> 8.4万、1,080,000 -> 108万。"""
            try:
                n = float(v)
            except Exception:
                return "--"
            if abs(n) < 10000:
                return "{:,}".format(int(round(n)))
            s = "%.1f" % (n / 10000.0)
            if s.endswith(".0"):
                s = s[:-2]
            return s + "万"
        def _bar(cv, pct, color):
            if cv is None:
                return
            try:
                cv.delete("all")
                w, h = cv.winfo_width(), cv.winfo_height()
                if w <= 1:
                    w, h = 96, 7
                cv.create_rectangle(0, 0, w, h, fill=C_BG, outline="")
                if pct is None:
                    return
                pp = max(0.0, min(100.0, float(pct)))
                if pp > 0:
                    cv.create_rectangle(0, 0, max(2, int(w * pp / 100.0)), h,
                                        fill=color, outline="")
            except Exception:
                pass
        hp, hpm = d.get("hp"), d.get("hp_max")
        mp, mpm = d.get("mp"), d.get("mp_max")
        exp_abs, exp_pct = d.get("exp_abs"), d.get("exp_pct")
        meso, lv = d.get("meso"), d.get("level")
        self.w_hp_text.set(("%s/%s" % (_fmt(hp), _fmt(hpm))) if hp is not None else "--")
        self.w_mp_text.set(("%s/%s" % (_fmt(mp), _fmt(mpm))) if mp is not None else "--")
        if exp_abs is not None or exp_pct is not None:
            txt = _wan(exp_abs) if exp_abs is not None else ""
            if exp_pct is not None:
                txt = (txt + " %.1f%%" % exp_pct).strip()
            self.w_exp_text.set(txt)
        else:
            self.w_exp_text.set("--")
        self.w_meso_text.set(_wan(meso) if meso is not None else "--")
        try:
            self.w_lv.set(("Lv. %d" % int(lv)) if lv else "Lv. --")
        except Exception:
            self.w_lv.set("Lv. --")
        _bar(self.w_bar_hp, (hp * 100.0 / hpm) if (hp is not None and hpm) else None, C_ERR)
        _bar(self.w_bar_mp, (mp * 100.0 / mpm) if (mp is not None and mpm) else None, C_BLUE)
        _bar(self.w_bar_exp, exp_pct, C_ACCENT)

    def _watch_redraw_pop(self, d=None):
        d = d or getattr(self, "watch_pop", {}) or {}
        def _wan(v):
            """数字过长时改用「万」为单位，例如 84,000 -> 8.4万、1,080,000 -> 108万。"""
            try:
                n = float(v)
            except Exception:
                return "--"
            if abs(n) < 10000:
                return "{:,}".format(int(round(n)))
            s = "%.1f" % (n / 10000.0)
            if s.endswith(".0"):
                s = s[:-2]
            return s + "万"
        epm = d.get("exp_per_min")
        if epm is None:
            epm = getattr(self, "watch_exp_per_min", None)
        mpm = d.get("meso_per_min")
        if mpm is None:
            mpm = getattr(self, "watch_meso_per_min", None)
        self.watch_exp_per_min = epm
        self.watch_meso_per_min = mpm
        try:
            if epm is not None:
                eph = _wan(float(epm) * 60.0)
                self.w_eta.configure(text="经验获取：%s/分钟 · 预估 %s/小时" % (_wan(epm), eph),
                                     fg=C_ACCENT)
            else:
                self.w_eta.configure(text="经验获取：--/分钟 · 预估 --/小时（等 2 分钟样本）",
                                     fg=C_MUTED)
        except Exception:
            pass
        try:
            if mpm is not None and float(mpm) > 0:
                mph = _wan(float(mpm) * 60.0)
                note = d.get("note") or ""
                txt = "金币收入：%s/分钟 · 预估 %s/小时" % (_wan(mpm), mph)
                if note:
                    txt += "（%s）" % note
                self.w_pop.configure(text=txt, fg=C_WARN)
            else:
                note = d.get("note") or "等 2 分钟金币样本"
                self.w_pop.configure(text="金币收入：--/分钟 · 预估 --/小时（%s）" % note,
                                     fg=C_MUTED)
        except Exception:
            pass
        try:
            self._watch_redraw()
        except Exception:
            pass
    def _watch_on_pop(self, d):
        self.watch_pop.update(d)
        self._watch_redraw_pop()

    def _watch_pick(self, key):
        if datawatch is None:
            messagebox.showwarning("缺少模块",
                                   "datawatch.py 未加载，请确认它与主程序在同一目录。")
            return
        tips = {
            "stat": "框住游戏底部那条状态栏：HP(…) MP(…) EXP 数值与百分比一起框进来",
            "level": "（可选）只框等级数字，例如 Lv. 37 里的 37",
            "pop": "框住屏幕右侧「You received EXP (+8)」「You have gained mesos (+10)」"
                   "这类飘字会出现的区域（可略宽，能容纳整行）",
        }
        tips.update({
            "hp": "只框 HP 数字那一小段，例如 1298/1298",
            "mp": "只框 MP 数字那一小段，例如 4560/4560",
            "exp": "只框 EXP 数字与百分比，例如 97,550 [57.65%]",
            "meso": "只框金币数字，例如 1,234,567（用于换算每小时预估收入）",
        })
        mins = {"stat": (80, 8), "level": (10, 8), "pop": (60, 20),
                "hp": (40, 10), "mp": (40, 10), "exp": (60, 10), "meso": (40, 10)}

        def done(region):
            self.cfg.setdefault("watch", {})[key + "_region"] = region
            save_config(self.cfg)
            self.w_state.configure(text=self._watch_region_text())
            self._log("数据看板：%s 区域已保存 %s" % (key, region))
            self.root.after(260, self._watch_preview)

        mw, mh = mins.get(key, (40, 8))
        RegionSelector(self.root, done, min_w=mw, min_h=mh, tip=tips.get(key))

    def _store(self):
        if datawatch is None:
            return None
        if self.watch_store is None:
            try:
                self.watch_store = datawatch.WatchStore(str(DATA_DIR / "watch.db"))
            except Exception as e:
                self._log("数据看板：样本库不可用 %s" % e)
                return None
        return self.watch_store

    def _watch_apply_interval(self):
        try:
            ms = int(self.w_interval.get())
        except Exception:
            return
        try:
            pms = int(self.w_popiv.get())
        except Exception:
            pms = 1000
        wc = self.cfg.setdefault("watch", {})
        wc["stat_interval_ms"] = ms
        wc["interval_ms"] = ms
        wc["pop_interval_ms"] = pms
        if self.watch_engine:
            self.watch_engine.cfg["stat_interval_ms"] = ms
            self.watch_engine.cfg["interval_ms"] = ms
            self.watch_engine.cfg["pop_interval_ms"] = pms
        save_config(self.cfg)

    def _watch_apply_ocr(self):
        """切识别引擎：写配置后重建引擎（监控中则重启采样线程）。"""
        kind = {"自动": "auto", "系统OCR": "winrt", "高精度": "rapid"}.get(
            str(self.w_engine.get()), "auto")
        wc = self.cfg.setdefault("watch", {})
        wc["ocr_engine"] = kind
        save_config(self.cfg)
        running = self.watch_engine is not None
        if running:
            self._watch_stop()
        info = datawatch.ocr_info(wc)
        name = {"winrt": "系统OCR", "rapid": "RapidOCR"}.get(str(info.get("engine")), "未知")
        th = info.get("threads") or 0
        self._log("数据看板：识别引擎 = %s%s" % (name, ("（%d 线程）" % th) if th else ""))
        if running:
            self.root.after(150, self._watch_start)

    def _watch_apply_lowcpu(self):
        """低配模式：单线程 + 降频，优先保证游戏不卡。"""
        on = bool(self.w_lowcpu.get())
        wc = self.cfg.setdefault("watch", {})
        wc["low_cpu"] = on
        wc["ocr_threads"] = 1 if on else 2
        if on:
            wc["stat_interval_ms"] = max(3000, int(wc.get("stat_interval_ms") or 2000))
            wc["pop_interval_ms"] = max(1500, int(wc.get("pop_interval_ms") or 1000))
        for var, key in ((self.w_interval, "stat_interval_ms"),
                         (self.w_popiv, "pop_interval_ms")):
            try:
                var.set(str(int(wc.get(key) or 2000)))
            except Exception:
                pass
        self._watch_apply_interval()
        self._log("数据看板：低配模式 %s" % (
            "开（状态条 3s / 飘字 1.5s / 单线程）" if on
            else "关（状态条 2s / 飘字 1s / 双线程）"))

    def _watch_level_changed(self, *a):
        try:
            lv = int(self.level_var.get())
        except Exception:
            return
        self.cfg.setdefault("watch", {})["level"] = lv
        if self.watch_engine:
            self.watch_engine.cfg["level"] = lv
        self.watch_total = None
        self._watch_refresh()
        self._watch_redraw()

    def _watch_start(self):
        if datawatch is None:
            messagebox.showwarning("缺少模块",
                                   "datawatch.py 未加载，请确认它与主程序在同一目录。")
            return
        if self.watch_engine:
            return
        w = self.cfg.setdefault("watch", {})
        if not datawatch.WatchEngine.regions_of(w):
            messagebox.showwarning(
                "还没框选区域",
                "请先点「框选状态条」，把游戏底部 HP/MP/EXP 那一整条框进来。")
            return
        self._watch_level_changed()
        try:
            if not w.get("window_keyword"):
                w["window_keyword"] = "maplestory"
        except Exception:
            pass
        w["enabled"] = True
        try:
            cap = None
            self.watch_engine = datawatch.WatchEngine(
                w, self.q,
                capturer=cap,
                title_getter=get_foreground_title)
            self.watch_engine.start()
        except Exception as e:
            self.watch_engine = None
            messagebox.showerror("启动失败", "数据看板启动失败：%s" % e)
            return
        self.w_btn_start.configure(state="disabled")
        self.w_btn_stop.configure(state="normal")
        self.w_lamp.configure(fg=C_ACCENT)
        self._log("数据看板：开始数字识别采样")

    def _watch_stop(self):
        if self.watch_engine:
            try:
                self.watch_engine.stop()
            except Exception:
                pass
            self.watch_engine = None
        self.cfg.setdefault("watch", {})["enabled"] = False
        try:
            self.w_btn_start.configure(state="normal")
            self.w_btn_stop.configure(state="disabled")
            self.w_lamp.configure(fg=C_DIM)
        except Exception:
            pass

    def _watch_clear(self):
        st = self._store()
        if st:
            st.clear()
        self.watch_total = None
        self.watch_last = {"ts": 0, "level": None, "exp_abs": None, "exp_pct": None,
                           "meso": None,
                           "hp": None, "hp_max": None, "mp": None, "mp_max": None,
                           "raw": ""}
        self.watch_pop = {"exp": 0, "meso": 0, "total_exp": 0, "total_meso": 0,
                          "span_min": 0.0, "exp_per_min": None, "meso_per_min": None,
                          "meso_per_hour": None, "note": ""}
        self.watch_exp_per_min = None
        self.watch_meso_per_min = None
        self._watch_redraw()
        self._watch_redraw_pop(self.watch_pop)
        self._log("数据看板：历史样本已清空")

    def _watch_dump(self):
        if datawatch is None:
            return
        w = self.cfg.get("watch", {})
        regions = datawatch.WatchEngine.regions_of(w)
        if not regions:
            messagebox.showwarning("没有区域", "还没有框选任何区域。")
            return
        out_dir = DATA_DIR / "watch_debug"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("创建目录失败", str(e))
            return
        try:
            from PIL import ImageGrab  # noqa: F401
        except Exception:
            pass
        cap = Capturer()
        stamp = time.strftime("%H%M%S")
        saved = 0
        for idx, r in enumerate(regions):
            try:
                img = cap.grab(r)
                if img is None:
                    continue
                fn = out_dir / ("region%d_%s.png" % (idx, stamp))
                cv2.imwrite(str(fn), img)
                saved += 1
            except Exception as e:
                self._log("导出区域 %s 失败：%s" % (r, e))
        if saved:
            self._log("数据看板：已导出 %d 张区域抓图到 %s" % (saved, out_dir))
            try:
                os.startfile(str(out_dir))
            except Exception:
                pass
        else:
            messagebox.showwarning("导出失败", "区域抓图没有成功保存。")

    # ---------- 预览抓图 ----------
    def _watch_preview(self, silent=False):
        """抓一帧做预览：显示各区域裁剪图与 OCR 原文，用来判断框得对不对。"""
        if datawatch is None:
            return
        try:
            regs = datawatch.preview_regions(self.cfg.get("watch", {}))
        except Exception:
            regs = []
        if not regs:
            if not silent:
                messagebox.showwarning("没有区域", "还没有框选任何区域。")
            return
        if getattr(self, "_pv_busy", False):
            return
        self._pv_busy = True
        try:
            self.w_btn_preview.configure(state="disabled")
        except Exception:
            pass
        try:
            self.w_state.configure(text="%s\u4e28 \u6b63\u5728\u6293\u56fe\u2026" % self._watch_region_text())
        except Exception:
            pass
        threading.Thread(target=self._preview_worker, daemon=True).start()

    def _preview_worker(self):
        info = None
        try:
            cap = Capturer()
            out_png = DATA_DIR / "watch_debug" / "preview.png"
            info = datawatch.preview_frame(self.cfg.get("watch", {}), capturer=cap,
                                           out_png=str(out_png))
        except Exception as e:
            info = {"ok": False, "msg": "\u6293\u56fe\u5931\u8d25\uff1a%s" % e, "items": []}
        if not isinstance(info, dict):
            info = {"ok": False, "msg": "\u6293\u56fe\u5931\u8d25", "items": []}
        self.q.put(("watch_preview", info))

    def _watch_on_preview(self, d):
        self._pv_busy = False
        try:
            self.w_btn_preview.configure(state="normal")
        except Exception:
            pass
        try:
            self.w_state.configure(text=self._watch_region_text())
        except Exception:
            pass
        if not (isinstance(d, dict) and d.get("ok")):
            msg = (d or {}).get("msg") or "\u6293\u56fe\u5931\u8d25"
            self.watch_note = msg
            try:
                self.w_state.configure(text="%s \uff5c %s" % (self._watch_region_text(), msg))
            except Exception:
                pass
            self._log("\u6570\u636e\u770b\u677f\uff1a\u9884\u89c8\u5931\u8d25 " + str(msg))
            return
        try:
            self._show_preview_window(d)
        except Exception as e:
            self._log("\u6570\u636e\u770b\u677f\uff1a\u9884\u89c8\u7a97\u53e3\u5f02\u5e38 %s" % e)

    def _show_preview_window(self, d):
        win = getattr(self, "_pv_win", None)
        try:
            alive = win is not None and win.winfo_exists()
        except Exception:
            alive = False
        if not alive:
            win = tk.Toplevel(self.root)
            win.title("数据看板 · 预览抓图")
            win.configure(bg=C_CARD)
            win.geometry("800x700")
            win.minsize(520, 420)
            self._pv_win = win
        for ch in list(win.winfo_children()):
            try:
                ch.destroy()
            except Exception:
                pass

        top = tk.Frame(win, bg=C_CARD)
        top.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(top, text="预览抓图", bg=C_CARD, fg=C_TXT,
                 font=(C_FONT, 11, "bold")).pack(side="left")
        tk.Label(top, text="原尺寸显示 · 图糊多半是框偏了，可拖动查看",
                 bg=C_CARD, fg=C_DIM, font=(C_FONT, 8)).pack(side="left", padx=8)
        ttk.Button(top, text="重新抓一帧",
                   command=lambda: self._watch_preview()).pack(side="right")
        ttk.Button(top, text="关闭",
                   command=win.destroy).pack(side="right", padx=(0, 6))

        box = tk.Text(win, height=8, bg=C_BG, fg=C_TXT, relief="flat", bd=0,
                      font=(C_FONT, 9), wrap="word")
        box.pack(side="bottom", fill="x", padx=12, pady=(2, 10))
        lines_txt = []
        for it in (d.get("items") or []):
            raw = (it.get("raw") or "").strip()
            if not raw:
                raw = "（没读出任何字 → 这个框大概是偏的）"
            lines_txt.append("%d. %s  %dx%d  @ %s" % (it.get("idx", 0), it.get("name", ""),
                                                      it.get("w", 0), it.get("h", 0),
                                                      it.get("region")))
            lines_txt.append("   原文：%s" % raw.replace("\n", " / ")[:220])
        st = d.get("stats") or {}
        if st:
            lines_txt.append("")
            lines_txt.append("状态条解析：HP %s/%s   MP %s/%s   EXP %s [%s]" % (
                datawatch.fmt_int(st.get("hp")), datawatch.fmt_int(st.get("hp_max")),
                datawatch.fmt_int(st.get("mp")), datawatch.fmt_int(st.get("mp_max")),
                datawatch.fmt_int(st.get("exp_abs")),
                "--" if st.get("exp_pct") is None else "%.2f%%" % st.get("exp_pct")))
        elif d.get("raw"):
            lines_txt.append("")
            lines_txt.append("状态条原文：" + str(d.get("raw")).replace("\n", " / ")[:220])
        pr = d.get("pop") or {}
        if pr.get("exp") is not None or pr.get("meso") is not None:
            lines_txt.append("飘字区：EXP +%s　meso +%s" % (pr.get("exp"), pr.get("meso")))
        box.insert("1.0", "\n".join(lines_txt))
        box.configure(state="disabled")

        png = d.get("png") or ""
        if png:
            try:
                img = tk.PhotoImage(file=png)
                self._pv_img = img
                holder = tk.Frame(win, bg=C_CARD)
                holder.pack(fill="both", expand=True, padx=12, pady=4)
                cvs = tk.Canvas(holder, bg="#14181f", highlightthickness=1,
                                highlightbackground=C_LINE, xscrollincrement=20,
                                yscrollincrement=20)
                vs = ttk.Scrollbar(holder, orient="vertical", command=cvs.yview)
                hs = ttk.Scrollbar(holder, orient="horizontal", command=cvs.xview)
                cvs.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
                cvs.grid(row=0, column=0, sticky="nsew")
                vs.grid(row=0, column=1, sticky="ns")
                hs.grid(row=1, column=0, sticky="ew")
                holder.rowconfigure(0, weight=1)
                holder.columnconfigure(0, weight=1)
                cvs.create_image(2, 2, anchor="nw", image=img)
                cvs.configure(scrollregion=(0, 0, img.width() + 4, img.height() + 4))

                def _wheel(e):
                    cvs.yview_scroll(-1 if getattr(e, "delta", 0) > 0 else 1, "units")
                cvs.bind("<MouseWheel>", _wheel)
            except Exception as e:
                tk.Label(win, text="预览图显示失败：%s" % e, bg=C_CARD,
                         fg=C_WARN).pack(pady=8)
        else:
            tk.Label(win, text="未生成预览图", bg=C_CARD, fg=C_DIM).pack(pady=8)
        try:
            win.lift()
            win.focus_force()
        except Exception:
            pass


    def _watch_on_sample(self, d):
        self.watch_last.update(d)
        st = self._store()
        if st:
            st.add(d.get("ts") or time.time(), d.get("level") or 0,
                   d.get("exp_pct"), d.get("exp_abs"),
                   d.get("hp"), d.get("hp_max"), d.get("mp"), d.get("mp_max"),
                   d.get("meso"))
        # 等级以 OCR 为准，连续两次一致才切换，避免误读抖动
        lv = d.get("level")
        if lv:
            try:
                if int(self.level_var.get()) != int(lv):
                    if self._lv_seen == int(lv):
                        self.level_var.set(int(lv))
                        self.cfg.setdefault("watch", {})["level"] = int(lv)
                        self.watch_total = None
                        self._lv_seen = None
                        self._log("数据看板：等级自动识别为 %d" % int(lv))
                    else:
                        self._lv_seen = int(lv)
                else:
                    self._lv_seen = None
            except Exception:
                pass
        self._watch_redraw()
        self._watch_refresh()
    def _watch_refresh(self):
        if datawatch is None:
            return
        st = self._store()
        if not st:
            return
        info = {}
        try:
            lv = int(self.level_var.get())
            rows = st.recent(60 * 6)
            info = datawatch.exp_rate(rows, lv, window_min=60) or {}
        except Exception:
            info = {}
        self.watch_info = info
        d = self.watch_last or {}
        # 本级总经验：优先用「当前经验 / 当前百分比」反推，比历史累计更即时
        if info.get("total_exp"):
            self.watch_total = int(info["total_exp"])
        if d.get("exp_abs") and d.get("exp_pct") and d["exp_pct"] >= 2.0:
            self.watch_total = int(round(d["exp_abs"] * 100.0 / d["exp_pct"]))
        # 经验/小时：统一以「每分钟经验」为基准换算，分钟值 ×60
        epm = info.get("exp_per_min")
        if epm is None and info.get("exp_per_hour") is not None:
            epm = float(info["exp_per_hour"]) / 60.0
        if epm is None:
            epm = getattr(self, "watch_exp_per_min", None)
        # 金币/小时：窗口内金币增量最小二乘 → 每分钟金额 ×60 得每小时预估
        mrate = {}
        try:
            mrows = st.recent_meso(60 * 10)
            mrate = datawatch.meso_rate(mrows) or {}
        except Exception:
            mrate = {}
        self.watch_pop = self.watch_pop or {}
        self.watch_pop.update({"ts": time.time(),
                               "exp_per_min": epm,
                               "meso_per_min": mrate.get("meso_per_min"),
                               "meso_per_hour": mrate.get("meso_per_hour"),
                               "note": mrate.get("note") or ""})
        self._watch_redraw_pop(self.watch_pop)
        # 升级预估作为附加信息，挂在经验行尾部
        try:
            if epm is not None and info.get("eta_min") is not None:
                extra = "｜升级预计 %s" % datawatch.fmt_eta(info["eta_min"])
                if info.get("remain_exp"):
                    extra += "（还差 %s）" % datawatch.fmt_int(info["remain_exp"])
                self.w_eta.configure(text=self.w_eta.cget("text") + extra)
        except Exception:
            pass
    def pick_region(self):
        state = self.btn_stop["state"]
        if state == "normal":
            messagebox.showinfo("提示", "请先停止翻译，再框选区域。")
            return
        self.root.withdraw()
        self.root.after(220, lambda: RegionSelector(
            self.root, self._region_done, on_cancel=self.root.deiconify))

    def _region_done(self, region):
        self.cfg["region"] = region
        save_config(self.cfg)
        self.region_lbl.configure(text=self._region_text())
        self.root.deiconify()
        self._log("聊天区域已保存：%s" % (region,))

    def save_settings(self):
        self.cfg["api_key"] = self.key_var.get().strip()
        self.cfg["api_base"] = self.base_var.get().strip() or DEFAULT_CONFIG["api_base"]
        self.cfg["model"] = self.model_var.get().strip() or "deepseek-chat"
        self.cfg["terms_enabled"] = bool(self.terms_var.get())
        try:
            self.cfg["interval_ms"] = max(150, int(self.interval_var.get()))
        except Exception:
            self.cfg["interval_ms"] = 500
        save_config(self.cfg)
        self.reply_tr = None          # 让回复翻译器用新设置重建
        self._log("设置已保存")

    def toggle_terms(self):
        self.cfg["terms_enabled"] = bool(self.terms_var.get())
        save_config(self.cfg)
        self._log("术语库：%s（重启翻译后生效）" % ("已启用" if self.cfg["terms_enabled"] else "已关闭"))

    def open_terms(self):
        if not TERMS_PATH.exists():
            messagebox.showwarning("未找到术语库", "缺少 terms.json，请重新解压完整程序目录。")
            return
        try:
            os.startfile(str(TERMS_PATH))
        except Exception as e:
            messagebox.showinfo("术语库路径", "%s\n\n（打开失败：%s）" % (TERMS_PATH, e))

    def test_translate(self):
        self.save_settings()
        if not self.cfg.get("api_key"):
            messagebox.showwarning("缺少 API Key", "请先填入 DeepSeek 兼容接口的 API Key。")
            return

        def work():
            tr = Translator(self.cfg)
            samples = ["gg ez", "wtb 30% dex wg pm me", "anyone doing pq? lf pt",
                       "selling steely 2m ea fm ch1", "ty all, gtg cya"]
            outs = tr.translate_batch(samples)
            for s, o in zip(samples, outs):
                self.q.put(("line", (s, o)))
            self.q.put(("status", "测试完成，结果已写入悬浮窗"))
            self.q.put(("log", "测试翻译完成"))

        threading.Thread(target=work, daemon=True).start()

    def start(self):
        if not self.cfg.get("region"):
            messagebox.showwarning("缺少区域", "请先点击「框选聊天区域」，框住游戏聊天框。")
            return
        self.save_settings()
        if not self.cfg.get("api_key"):
            if not messagebox.askyesno("未配置 API Key",
                                       "没有 API Key 时只能识别原文，无法翻译。\n仍要开始吗？"):
                return
        self.overlay.show()
        if self.engine is None or not self.engine.is_alive():
            self.engine = ScanEngine(self.cfg, self.q)
            self.engine.start()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._log("已启动扫描")
        self.lamp.configure(fg=C_ACCENT)

    def stop(self):
        if self.engine:
            self.engine.stop()
            self.engine = None
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self._log("已停止")
        self.lamp.configure(fg=C_DIM)

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    self.overlay.push(payload[0], payload[1])
                    self._log("%s  ->  %s" % (payload[0][:46], (payload[1] or "")[:46]))
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "log":
                    self._log(payload)
                elif kind == "reply_req":
                    self.do_reply(payload)
                elif kind == "reply_done":
                    self.reply_busy = False
                    self._show_reply(payload[0], payload[1])
                elif kind == "hotkey":
                    self._on_hotkey(payload)
                elif kind == "error":
                    self._log("异常：" + payload.replace("\n", " | ")[:300])
                elif kind == "fatal":
                    self.status_var.set(payload)
                    self._log(payload)
                    self.stop()
                elif kind == "watch_preview":
                    self._watch_on_preview(payload)
                elif kind == "watch_sample":
                    self._watch_on_sample(payload)
                elif kind == "watch_pop":
                    self._watch_on_pop(payload)
                elif kind == "watch_status":
                    self.watch_note = payload
                    self.w_state.configure(text="%s ｜ %s" % (self._watch_region_text(), payload))
                elif kind == "watch_fatal":
                    self.watch_note = payload
                    self._log("数据看板：" + payload)
                    self._watch_stop()
                elif kind == "watch_purge":
                    st = self._store()
                    if st:
                        st.purge(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def on_close(self):
        try:
            self.save_settings()
        except Exception:
            pass
        try:
            self.cfg["win_geometry"] = self.root.geometry()
            save_config(self.cfg)
        except Exception:
            pass
        if self.engine:
            self.engine.stop()
        if self.watch_engine:
            self.watch_engine.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def _selftest():
    """命令行自检：python main.py --selftest / MapleChat.exe --selftest"""
    try:  # 控制台默认代码页是 936，中文输出会变乱码，这里切到 UTF-8
        os.system("chcp 65001 >nul 2>&1")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("[selftest] python  =", sys.version.split()[0])
    print("[selftest] exe     =", sys.executable)
    print("[selftest] frozen  =", getattr(sys, "frozen", False))
    print("[selftest] app_dir =", APP_DIR)
    try:
        import rapidocr_onnxruntime as _r
        print("[selftest] pkg_dir =", Path(_r.__file__).resolve().parent)
    except Exception as e:
        print("[selftest] import rapidocr FAILED:", repr(e))
        return 2
    try:
        eng = OcrEngine()
        eng.ensure()
        print("[selftest] RapidOCR init OK")
    except Exception as e:
        print("[selftest] OCR init FAILED:", repr(e))
        return 3
    try:
        img = np.full((70, 620, 3), 255, np.uint8)
        cv2.putText(img, "gg ez ks pq lf wtb", (12, 48),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 2, cv2.LINE_AA)
        lines = eng.read(img, scale=2.5, binarize=True)
        print("[selftest] OCR lines =", lines)
        if not lines:
            print("[selftest] OCR produced no text")
            return 4
    except Exception as e:
        print("[selftest] OCR run FAILED:", repr(e))
        return 5
    try:
        import mss  # noqa: F401
        cap = Capturer()
        frame = cap.grab((0, 0, 200, 100))
        print("[selftest] screen grab OK, shape =", None if frame is None else frame.shape)
    except Exception as e:
        print("[selftest] screen grab FAILED:", repr(e))
        return 6
    try:
        terms, meta = load_terms()
        print("[selftest] terms =", meta["count"], "keys /", meta["groups"], "groups, ver", meta["version"])
        if not terms:
            print("[selftest] terms.json MISSING or empty:", TERMS_PATH)
            return 7
        tr = Translator({"target_lang": "简体中文", "terms_enabled": True})
        probe = ["[Party] Bob: ty", "henesys ch1", "wtb 30% dex wg pm me"]
        print("[selftest] direct =", [tr.direct_hit(t) for t in probe])
        print("[selftest] hits   =", [tr.terms_block([t]).strip() for t in probe])
        inv = tr.inv()
        print("[selftest] inv    =", len(inv), "中文词")
        print("[selftest] rev_hit    =", [tr.rev_hit(z) for z in ["你好", "卖", "谢谢"]])
        print("[selftest] rev_terms  =", tr.rev_terms_block("想在自由市场收30%敏捷工地手套，私聊我").strip())
        print("[selftest] setclip   =", set_clipboard("MapleChat selftest"))
        # 快捷组句引擎（chatgen）自检
        if chatgen is None:
            print("[selftest] chatgen   = MISSING")
            return 8
        flat2, _meta2 = load_terms()
        rev2 = chatgen.build_rev(flat2)
        cases = [
            ("trade", "buy", dict(target="工地手套", extra="500k")),
            ("trade", "pc", dict(target="枫叶短刀")),
            ("party", "recruit", dict(target="kerning pq", extra="cc1")),
            ("pq", "need", dict(target="ludi pq", role="cleric")),
            ("leech", "buyingLeech", dict(target="sg2", slots="2 slots", extra="10m")),
            ("location", "tellChannel", dict(channel="cc3")),
            ("skill", "canUse", dict(target="HB")),
            ("server", "lag", dict()),
            ("custom", "LF>", dict(target="zakum 2 slots")),
        ]
        outs = []
        for mode, act, kw in cases:
            r = chatgen.build(mode, act, **kw)
            outs.append(chatgen.auto_en(kw.get("target", ""), rev2)[0] if kw.get("target") else "")
            if not r.get("english"):
                print("[selftest] chatgen EMPTY:", mode, act, r)
                return 8
        print("[selftest] chatgen   = OK, %d 场景 / %d 动作" % (len(chatgen.SCENES), sum(len(v) for v in chatgen.ACTIONS.values())))
        print("[selftest] cn2en     = 枫叶短刀->%s | 工地手套->%s" % (chatgen.auto_en("枫叶短刀", rev2)[0], chatgen.auto_en("工地手套", rev2)[0]))
    except Exception as e:
        print("[selftest] terms FAILED:", repr(e))
        return 7

    # 数据看板（datawatch v2）：数字解析 + 样本库读写 + 升级时间算法
    try:
        if datawatch is None:
            print("[selftest] datawatch = MISSING")
            return 9
        try:
            _i = datawatch.ocr_info(datawatch.DEFAULT_WATCH)
            print("[selftest] datawatch engine = %s / %s 线程 / lang=%s" % (
                _i.get("engine"), _i.get("threads") or 0, _i.get("lang") or "-"))
            _we = datawatch.get_win_ocr()
            if getattr(_we, "available", False):
                _im = np.full((66, 440, 3), 40, np.uint8)
                cv2.putText(_im, "HP[1298/1298] MP[1087/1087] EXP 294,267 [57.65%]",
                            (6, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (235, 235, 235), 2)
                print("[selftest] datawatch winrt = ok / %r" % (
                    " ".join(str(x[1]) for x in _we.raw(_im))[:56],))
            else:
                print("[selftest] datawatch winrt = 不可用 / %s" % (_we.err or "?"))
            print("[selftest] datawatch fallback = %s" % (
                datawatch.get_ocr_alt({"ocr_engine": "auto", "ocr_threads": 1}) is not None))
        except Exception as _e2:
            print("[selftest] datawatch engine = 异常", repr(_e2))
        probe = ("Assassin HP[1298/1298] MP(1087/1087] "
                 "EXP 97,550 [45.04%] OldTeng")
        st_num = datawatch.parse_stats(probe)
        print("[selftest] datawatch parse =", st_num)
        want = {"hp": 1298, "hp_max": 1298, "mp": 1087, "mp_max": 1087,
                "exp_abs": 97550, "exp_pct": 45.04}
        if any(st_num.get(k) != v for k, v in want.items()):
            print("[selftest] datawatch parse FAILED:", st_num)
            return 9
        db = Path(tempfile.gettempdir()) / "_maplechat_selftest_watch.db"
        for suf in ("", "-wal", "-shm"):
            try:
                Path(str(db) + suf).unlink()
            except Exception:
                pass
        store = datawatch.WatchStore(str(db))
        now = time.time()
        for i in range(6):
            e = 90000 + i * 1000
            store.add(now - 300 + i * 60, 37, e * 100.0 / 216585, e, 1298, 1298, 1087, 1087)
        rows = store.recent(60)
        info = datawatch.exp_rate(rows, 37, window_min=60)
        print("[selftest] datawatch rate  =",
              None if info["exp_per_hour"] is None else round(info["exp_per_hour"], 1),
              "exp/h, total =", info.get("total_exp"),
              ", eta =", datawatch.fmt_eta(info["eta_min"]))
        if len(rows) < 6 or info.get("exp_per_hour") is None \
                or abs(info["exp_per_hour"] - 60000) > 2000:
            print("[selftest] datawatch rate FAILED:", info)
            return 9
        # v8 分层调度：假抓帧 + 假 OCR，验证「状态条 2s / 飘字 1s / 等级 TTL」三条时间线
        class _FakeCap(object):
            def __init__(self):
                self.log = []

            def grab(self, region):
                self.log.append(tuple(int(x) for x in region[:4]))
                return datawatch.np.zeros((8, 8, 3), datawatch.np.uint8)

        class _FakeOCR(object):
            def __call__(self, img, **kw):
                return []

        import queue as _queue
        _cap = _FakeCap()
        _q = _queue.Queue()
        _eng = datawatch.WatchEngine(
            {"stat_interval_ms": 2000, "pop_interval_ms": 1000, "static_ttl_ms": 4000,
             "stat_region": [0, 0, 100, 20], "pop_region": [0, 40, 100, 20],
             "only_when_foreground": False},
            _q, capturer=_cap, title_getter=lambda: "MapleStory", ocr=_FakeOCR())
        _eng.start()
        time.sleep(3.4)
        _eng.stop()
        _eng.join(timeout=5)
        _stat_n = sum(1 for r in _cap.log if r == (0, 0, 100, 20))
        _pop_n = sum(1 for r in _cap.log if r == (0, 40, 100, 20))
        print("[selftest] datawatch sched = 3.4s 内 状态条 %d 次 / 飘字 %d 次，线程存活 %s"
              % (_stat_n, _pop_n, _eng.is_alive()))
        if not (1 <= _stat_n <= 3 and 3 <= _pop_n <= 6 and not _eng.is_alive()):
            print("[selftest] datawatch sched FAILED:", _cap.log)
            return 9
        _pv_cfg = {"stat_region": [0, 0, 40, 12], "pop_region": [0, 40, 40, 12]}
        _pv_png = Path(tempfile.gettempdir()) / "_maplechat_preview_selftest.png"
        _pv = datawatch.build_preview(_pv_cfg, _cap.grab, ocr=_FakeOCR(), out_png=str(_pv_png))
        _pv_items = _pv.get("items") or []
        _pv_shape = None if not _pv.get("png") else cv2.imread(_pv.get("png")).shape
        print("[selftest] datawatch preview = ok %s / 区域 %d 个 / 图 %s 尺寸 %s"
              % (_pv.get("ok"), len(_pv_items), bool(_pv.get("png")), _pv_shape))
        if not (_pv.get("ok") and len(_pv_items) == 2 and _pv_shape):
            print("[selftest] datawatch preview FAILED:", _pv)
            return 10

        store.clear()
        del store
        for suf in ("", "-wal", "-shm"):
            try:
                Path(str(db) + suf).unlink()
            except Exception:
                pass
    except Exception as e:
        print("[selftest] datawatch FAILED:", repr(e))
        return 9

    print("[selftest] ALL PASS")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    App().run()
