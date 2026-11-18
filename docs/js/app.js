/*!
 * app.js — MapleChat Web 主控
 * 链路：getDisplayMedia 捕获 → 区域裁剪 → PP-OCR 识别 → 增量去重 → DeepSeek 翻译 → PiP 置顶悬浮窗
 */
(function () {
  'use strict';

  /* 冒险岛黑话在 OCR 里的高频混淆修正（l/I/1、i/l 等） */
  const OCR_FIX = [
    [/\bIf\s+pt\b/g, 'lf pt'],
    [/\bIfp\b/g, 'lfp'],
    [/\bLfp\b/g, 'lfp'],
  ];
  function fixOcr(t) {
    let r = t;
    for (const pair of OCR_FIX) r = r.replace(pair[0], pair[1]);
    return r;
  }


  /* ============================================================
   * 0. 常量：翻译提示词（与桌面版 main.py 完全一致）
   * ============================================================ */
  const SYS_PROMPT = `你是《冒险岛》(MapleStory) 国际服的聊天翻译助手。玩家给你若干条游戏内聊天消息，你要翻译成{lang}。

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
[2] 有人一起打boss吗`;

  const REPLY_PROMPT = `你是《冒险岛》(MapleStory) 国际服中文玩家的聊天助手。玩家给你一句中文，你要把它变成可以直接粘贴进游戏聊天框的英文。

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

{terms}`;

  const TERM_FIX = {
    str: '力量', strength: '力量', dex: '敏捷', dexterity: '敏捷',
    int: '智力', intelligence: '智力', luk: '运气', luck: '运气',
    hat: '帽子', helm: '头盔', top: '上衣', bottom: '裤子', overall: '套服',
    shoes: '鞋子', gloves: '手套', shield: '盾牌', cape: '披风',
    rng: '看脸、随机', 'random number generator': '看脸、随机',
  };

  const DEFAULTS = {
    api_base: 'https://api.deepseek.com',
    model: 'deepseek-chat',
    interval_ms: 900,
    scale: 2.0,
    binarize: false,
    engine: 'det',          // det | projection
    skip_first: true,
    skip_static: true,      // 画面无变化时跳过 OCR
    terms_enabled: true,
    target_lang: '简体中文',
    alpha: 0.95,
    font: 15,
  };

  const LS = {
    cfg: 'maplechat.web.cfg',
    key: 'maplechat.web.key',
    region: 'maplechat.web.region',
  };

  /* ============================================================
   * 1. 工具
   * ============================================================ */
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const now = () => performance.now();

  function el(tag, cls, txt) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function loadJSON(k, def) {
    try { const v = JSON.parse(localStorage.getItem(k)); return v == null ? def : v; } catch (e) { return def; }
  }
  function saveJSON(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }

  const CFG = Object.assign({}, DEFAULTS, loadJSON(LS.cfg, {}));
  let API_KEY = localStorage.getItem(LS.key) || '';

  function saveCfg() { saveJSON(LS.cfg, CFG); }

  /* ============================================================
   * 2. 术语库
   * ============================================================ */
  const Terms = {
    flat: {}, meta: {}, inv: null, ready: false,

    async load(enabled) {
      try {
        const raw = await fetch('data/terms.json', { cache: 'force-cache' }).then(r => r.json());
        const flat = {};
        const groups = raw.groups || {};
        for (const g in groups) {
          const table = groups[g];
          if (!table || typeof table !== 'object') continue;
          for (const kk in table) {
            const k2 = String(kk).trim().toLowerCase();
            const v = table[kk];
            if (!k2 || typeof v !== 'string' || !v.trim()) continue;
            flat[k2] = v.trim();
          }
        }
        for (const kk in TERM_FIX) if (kk in flat) flat[kk] = TERM_FIX[kk];
        this.flat = enabled ? flat : {};
        this.all = flat;
        this.meta = { count: Object.keys(flat).length, version: raw.version, groups: Object.keys(groups).length, source: raw.source };
        this.ready = true;
        this.inv = null;
        return this.meta;
      } catch (e) {
        this.ready = true;
        return { count: 0, error: String(e) };
      }
    },

    setEnabled(on) {
      this.flat = on ? this.all : {};
      this.inv = null;
    },

    /* 长词优先、词边界、不重叠 */
    hits(text, maxN) {
      maxN = maxN || 20;
      const terms = this.flat;
      if (!terms || !Object.keys(terms).length) return [];
      const low = text.toLowerCase();
      const keys = Object.keys(terms).sort((a, b) => b.length - a.length);
      const used = [], out = [];
      for (const kk of keys) {
        if (kk.length < 2) continue;
        let start = 0;
        while (true) {
          const i = low.indexOf(kk, start);
          if (i < 0) break;
          const j = i + kk.length;
          const leftOk = i === 0 || !/[a-z0-9]/i.test(low[i - 1]);
          const rightOk = j >= low.length || !/[a-z0-9]/i.test(low[j]);
          const overlap = used.some(p => p[0] < j && i < p[1]);
          if (leftOk && rightOk && !overlap) {
            used.push([i, j]);
            out.push([kk, terms[kk]]);
            break;
          }
          start = i + 1;
        }
        if (out.length >= maxN) break;
      }
      return out;
    },

    /* 整条消息就是术语时零成本直出 */
    directHit(text) {
      const parts = splitPrefix(text);
      const zh = this.flat[normTerm(parts.core)];
      if (!strictZh(zh)) return null;
      return parts.pre + zh;
    },

    termsBlock(texts) {
      if (!this.flat || !Object.keys(this.flat).length) return '';
      const seen = new Set(), pairs = [];
      for (const t of texts) {
        for (const [k, zh] of this.hits(t, 20)) {
          if (seen.has(k)) continue;
          const z = injectZh(zh);
          if (!z) continue;
          seen.add(k);
          pairs.push(k + '=' + z);
          if (pairs.length >= 40) break;
        }
        if (pairs.length >= 40) break;
      }
      if (!pairs.length) return '';
      return '术语参考（必须按此译法）：' + pairs.join('；') + '\n\n';
    },

    /* 中文 → 英文反向索引 */
    invIndex() {
      if (this.inv) return this.inv;
      const flat = this.all || {};
      const inv = {};
      for (const en in flat) {
        for (const part of String(flat[en]).split(/[、,，/;；]/)) {
          const z = part.replace(/[（(][^）)]*[）)]/g, '').replace(/^[\s.。:：]+|[\s.。:：]+$/g, '');
          if (!z || z.length > 10 || !/[\u4e00-\u9fff]/.test(z)) continue;
          (inv[z] = inv[z] || new Set()).add(en);
        }
      }
      const out = {};
      for (const kk in inv) {
        out[kk] = Array.from(inv[kk]).sort((a, b) => {
          const ra = /^[a-z]{2,5}$/.test(a) ? 0 : 1, rb = /^[a-z]{2,5}$/.test(b) ? 0 : 1;
          return ra - rb || a.length - b.length;
        });
      }
      this.inv = out;
      return out;
    },

    revHit(zh) {
      const inv = this.invIndex();
      const key = String(zh || '').replace(/[\s。！？!?~，,\.、]+/g, '');
      const c = inv[key];
      return c && c.length ? c[0] : null;
    },

    revTermsBlock(zh) {
      const inv = this.invIndex();
      const pairs = [], seen = new Set();
      const keys = Object.keys(inv).sort((a, b) => b.length - a.length);
      for (const z of keys) {
        if (z.length < 2 || seen.has(z) || String(zh).indexOf(z) < 0) continue;
        seen.add(z);
        pairs.push(z + '=' + inv[z].slice(0, 2).join('/'));
        if (pairs.length >= 12) break;
      }
      if (!pairs.length) return '';
      return '游戏常用写法（优先采用，再按语境调整）：' + pairs.join('；') + '\n\n';
    },
  };

  const PREFIX_RE = /^\s*(?:\[[^\]]{1,24}\]\s*)?[^\s:：\[\]]{0,20}\s*[:：]\s*/;

  function splitPrefix(text) {
    const m = PREFIX_RE.exec(text);
    if (!m) return { pre: '', core: text };
    return { pre: text.slice(0, m[0].length), core: text.slice(m[0].length) };
  }

  function normTerm(text) {
    let s = String(text).trim().toLowerCase();
    s = s.replace(/[.!?,;~"'`*]+$/, '').replace(/^[.!?,;~"'`*]+/, '').replace(/\s+/g, ' ');
    return s;
  }

  function strictZh(zh) {
    return !!zh && zh.indexOf('、') < 0 && zh.indexOf('（') < 0 && zh.length <= 8;
  }

  function injectZh(zh) {
    const z = String(zh || '').replace(/[（(][^）)]*[）)]/g, '').trim();
    if (!z || z.indexOf('、') >= 0 || z.length > 10) return '';
    return z;
  }

  /* ============================================================
   * 3. 翻译器
   * ============================================================ */
  const TR = {
    cache: new Map(),
    limit: 3000,
    stats: { req: 0, hit: 0, tokens: 0 },

    key(t) { return (CFG.target_lang || '') + '|' + t; },
    get(t) {
      const k = this.key(t);
      if (this.cache.has(k)) { this.stats.hit++; return this.cache.get(k); }
      return null;
    },
    put(t, v) {
      const k = this.key(t);
      this.cache.set(k, v);
      if (this.cache.size > this.limit) {
        const first = this.cache.keys().next().value;
        this.cache.delete(first);
      }
    },

    translatable(t) { return /[A-Za-z]{2,}/.test(t); },

    async api(messages, opt) {
      if (!API_KEY) throw new Error('未配置 API Key');
      const url = (CFG.api_base || DEFAULTS.api_base).replace(/\/+$/, '') + '/chat/completions';
      const ac = new AbortController();
      const timer = setTimeout(() => ac.abort(), (opt && opt.timeout) || 20000);
      try {
        this.stats.req++;
        const r = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + API_KEY },
          body: JSON.stringify(Object.assign({
            model: CFG.model || DEFAULTS.model,
            messages,
            temperature: 0.2,
            max_tokens: 1200,
            stream: false,
          }, opt && opt.body)),
          signal: ac.signal,
        });
        if (!r.ok) {
          const txt = await r.text().catch(() => '');
          let msg = txt.slice(0, 160);
          try { const j = JSON.parse(txt); msg = (j.error && j.error.message) || msg; } catch (e) {}
          throw new Error('接口 ' + r.status + ' · ' + msg);
        }
        const j = await r.json();
        if (j.usage) this.stats.tokens += (j.usage.total_tokens || 0);
        return j.choices[0].message.content;
      } finally { clearTimeout(timer); }
    },

    parse(content, n) {
      const idx = /^\s*[\[\(【]?(\d{1,3})[\]\)】]?\s*[\.、:：\-\)]?\s*(.*)$/;
      const strip = s => {
        const m = idx.exec(s.trim());
        return (m && m[2].trim()) ? m[2].trim() : s.trim();
      };
      const lines = String(content).split('\n').map(l => l.trim()).filter(Boolean);
      const parsed = {};
      for (const l of lines) {
        const m = idx.exec(l);
        if (m && m[2].trim()) parsed[parseInt(m[1], 10)] = m[2].trim();
      }
      let complete = true;
      for (let i = 1; i <= n; i++) if (!parsed[i]) { complete = false; break; }
      if (complete) { const out = []; for (let i = 1; i <= n; i++) out.push(parsed[i]); return out; }
      if (n === 1) return [strip(content)];
      const cleaned = lines.map(strip);
      if (cleaned.length === n) return cleaned;
      return [String(content).trim()].concat(new Array(n - 1).fill(''));
    },

    async translateBatch(texts) {
      const results = new Array(texts.length).fill(null);
      const pending = [];
      for (let i = 0; i < texts.length; i++) {
        const t = texts[i];
        const c = this.get(t);
        if (c != null) { results[i] = c; continue; }
        if (!this.translatable(t)) { results[i] = ''; continue; }
        const d = Terms.directHit(t);
        if (d != null) { results[i] = d; this.put(t, d); continue; }
        pending.push(i);
      }
      if (!pending.length) return results.map(r => r || '');
      if (!API_KEY) return results.map(r => r || '');

      for (let s = 0; s < pending.length; s += 20) {
        const chunk = pending.slice(s, s + 20);
        const payload = chunk.map(i => texts[i]);
        const tb = Terms.termsBlock(payload);
        const numbered = payload.map((t, i) => '[' + (i + 1) + '] ' + t).join('\n');
        try {
          const content = await this.api([
            { role: 'system', content: SYS_PROMPT.replace('{lang}', CFG.target_lang || '简体中文').replace('{terms}', tb) },
            { role: 'user', content: numbered },
          ], { body: { temperature: 0.2, max_tokens: 1200 } });
          const outs = this.parse(content, payload.length);
          chunk.forEach((idx, j) => {
            results[idx] = outs[j] || '';
            if (outs[j]) this.put(texts[idx], outs[j]);
          });
        } catch (e) {
          chunk.forEach(idx => { results[idx] = ''; });
          throw e;
        }
      }
      return results.map(r => r || '');
    },

    async reverse(zh) {
      zh = String(zh || '').trim();
      if (!zh) return '';
      const k = 'R|' + zh;
      const c = this.get(k);
      if (c != null) return c;
      const d = Terms.revHit(zh);
      if (d != null) { this.put(k, d); return d; }
      if (!API_KEY) throw new Error('未配置 API Key');
      const tb = Terms.revTermsBlock(zh);
      const content = await this.api([
        { role: 'system', content: REPLY_PROMPT.replace('{terms}', tb) },
        { role: 'user', content: zh },
      ], { body: { temperature: 0.3, max_tokens: 160 } });
      const s = String(content).trim().replace(/^\s*(?:英文|English|译文|结果|Output)\s*[:：]\s*/i, '');
      for (const line of s.split('\n')) {
        const l = line.trim().replace(/^["“”'`]+|["“”'`]+$/g, '');
        if (l && /[A-Za-z]/.test(l)) { this.put(k, l); return l; }
      }
      return '';
    },
  };

  /* ============================================================
   * 4. 主状态
   * ============================================================ */
  const S = {
    stream: null, video: null, frameCanvas: null,
    region: loadJSON(LS.region, null),   // {x,y,w,h} 归一化 0-1
    running: false, timer: null, busy: false,
    firstScan: true, seen: new Map(),
    ocr: null, ocrReady: false,
    pipWin: null, pipRoot: null,
    lastThumb: null,
    lines: [],           // {src, dst, ts}
    selecting: false,
    audioCtx: null, wakeLock: null,
    stat: { ocr: 0, tr: 0, newN: 0, loop: 0 },
    cg: null,
  };

  /* ============================================================
   * 5. UI 日志
   * ============================================================ */
  function toast(msg, kind) {
    const box = $('#toasts');
    const t = el('div', 'toast' + (kind ? ' ' + kind : ''), msg);
    box.appendChild(t);
    setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 300); }, kind === 'err' ? 6000 : 3200);
  }

  function setStatus(txt, kind) {
    const s = $('#status');
    s.textContent = txt;
    s.className = 'status' + (kind ? ' ' + kind : '');
  }

  function setStat() {
    $('#stat-ocr').textContent = S.stat.ocr ? S.stat.ocr.toFixed(0) + 'ms' : '—';
    $('#stat-tr').textContent = S.stat.tr ? S.stat.tr.toFixed(0) + 'ms' : '—';
    $('#stat-n').textContent = String(S.stat.newN);
    $('#stat-req').textContent = String(TR.stats.req);
  }

  /* ============================================================
   * 6. 译文渲染（主面板 + PiP）
   * ============================================================ */
  function addLine(src, dst) {
    const rec = { src, dst, ts: Date.now() };
    S.lines.push(rec);
    if (S.lines.length > 300) S.lines.shift();
    renderLine(rec);
    renderLine(rec, true);
  }

  function renderLine(rec, toPip) {
    const list = toPip ? (S.pipRoot && $('.plist', S.pipRoot)) : $('#lines');
    if (!list) return;
    const row = el('div', 'line');
    const zh = el('div', 'zh', rec.dst || '—');
    const en = el('div', 'en', rec.src);
    row.appendChild(zh);
    row.appendChild(en);
    row.addEventListener('click', () => {
      navigator.clipboard.writeText(rec.dst || rec.src).then(() => toast('已复制'));
    });
    list.appendChild(row);
    while (list.children.length > 200) list.removeChild(list.firstChild);
    list.scrollTop = list.scrollHeight;
  }

  function clearLines() {
    S.lines = [];
    $('#lines').innerHTML = '';
    if (S.pipRoot) { const l = $('.plist', S.pipRoot); if (l) l.innerHTML = ''; }
  }

  /* ============================================================
   * 7. 屏幕捕获
   * ============================================================ */
  async function pickSource() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
      toast('当前浏览器不支持屏幕捕获，请用 Chrome / Edge 桌面版', 'err');
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: { ideal: 15, max: 30 }, width: { ideal: 1920 }, height: { ideal: 1080 } },
        audio: false,
        selfBrowserSurface: 'exclude',
        surfaceSwitching: 'include',
        systemAudio: 'exclude',
      });
      S.stream = stream;
      const v = S.video;
      v.srcObject = stream;
      await v.play().catch(() => {});
      stream.getVideoTracks()[0].addEventListener('ended', () => {
        stopScan();
        toast('画面共享已结束', 'err');
        $('#step2').classList.remove('active');
      });
      // 等元数据
      if (!v.videoWidth) await new Promise(r => v.addEventListener('loadedmetadata', r, { once: true }));
      $('#preview-wrap').classList.add('has-src');
      $('#step1').classList.add('done');
      $('#step2').classList.add('active');
      toast('已连接画面，现在框选聊天区域');
      // 沿用上次选区
      if (S.region) { drawRegionBox(); $('#step3').classList.add('active'); }
      startKeepAlive();
    } catch (e) {
      if (e && e.name !== 'NotAllowedError') toast('捕获失败：' + e.message, 'err');
    }
  }

  /* 框选 */
  function initSelector() {
    const wrap = $('#preview-wrap');
    const box = $('#sel-box');
    let sx = 0, sy = 0, dragging = false;

    function rel(e) {
      const r = wrap.getBoundingClientRect();
      return {
        x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
        y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
      };
    }

    wrap.addEventListener('mousedown', e => {
      if (!S.stream) return;
      if (e.target.closest('.sel-actions')) return;
      dragging = true;
      const p = rel(e); sx = p.x; sy = p.y;
      box.style.display = 'block';
      box.style.left = (sx * 100) + '%'; box.style.top = (sy * 100) + '%';
      box.style.width = '0px'; box.style.height = '0px';
      e.preventDefault();
    });

    wrap.addEventListener('mousemove', e => {
      if (!dragging) return;
      const p = rel(e);
      const x0 = Math.min(sx, p.x), y0 = Math.min(sy, p.y);
      const w = Math.abs(p.x - sx), h = Math.abs(p.y - sy);
      box.style.left = (x0 * 100) + '%'; box.style.top = (y0 * 100) + '%';
      box.style.width = (w * 100) + '%'; box.style.height = (h * 100) + '%';
      box.dataset.rect = JSON.stringify({ x: x0, y: y0, w, h });
    });

    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      let rect = null;
      try { rect = JSON.parse(box.dataset.rect || 'null'); } catch (e) {}
      if (!rect || rect.w < 0.03 || rect.h < 0.02) {
        box.style.display = 'none';
        toast('选区太小，重新拖一次', 'err');
        return;
      }
      S.region = rect;
      saveJSON(LS.region, rect);
      $('#step3').classList.add('active');
      toast('区域已选定：' + Math.round(rect.w * 100) + '% × ' + Math.round(rect.h * 100) + '%');
      renderRegionInfo();
    });
  }

  function drawRegionBox() {
    const box = $('#sel-box');
    if (!S.region) return;
    box.style.display = 'block';
    box.style.left = (S.region.x * 100) + '%';
    box.style.top = (S.region.y * 100) + '%';
    box.style.width = (S.region.w * 100) + '%';
    box.style.height = (S.region.h * 100) + '%';
    box.dataset.rect = JSON.stringify(S.region);
  }

  function renderRegionInfo() {
    const r = S.region;
    const info = $('#region-info');
    if (!r) { info.textContent = '未框选'; return; }
    const vw = S.video.videoWidth || 0, vh = S.video.videoHeight || 0;
    info.textContent = `${Math.round(r.w * vw)}×${Math.round(r.h * vh)} px`;
  }

  /* ============================================================
   * 8. 抓帧 + 扫描循环
   * ============================================================ */
  function grabRegion() {
    const v = S.video;
    if (!v || !v.videoWidth) return null;
    const fc = S.frameCanvas;
    if (fc.width !== v.videoWidth || fc.height !== v.videoHeight) {
      fc.width = v.videoWidth; fc.height = v.videoHeight;
    }
    const c = fc.getContext('2d', { willReadFrequently: true });
    c.drawImage(v, 0, 0, fc.width, fc.height);
    const r = S.region;
    return {
      canvas: fc,
      region: {
        x: Math.round(r.x * fc.width),
        y: Math.round(r.y * fc.height),
        w: Math.max(8, Math.round(r.w * fc.width)),
        h: Math.max(6, Math.round(r.h * fc.height)),
      },
    };
  }

  /* 画面静止检测：抓 64×64 缩略图比较平均差 */
  function staticCheck(fc, region) {
    const cv = OcrUtil.makeCanvas(64, 64);
    const c = OcrUtil.ctx2d(cv);
    c.drawImage(fc, region.x, region.y, region.w, region.h, 0, 0, 64, 64);
    const d = c.getImageData(0, 0, 64, 64).data;
    const cur = new Uint8Array(64 * 64);
    for (let i = 0, j = 0; i < 64 * 64; i++, j += 4) cur[i] = (d[j] * 0.3 + d[j + 1] * 0.59 + d[j + 2] * 0.11) | 0;
    if (!S.lastThumb) { S.lastThumb = cur; return false; }
    let sum = 0;
    for (let i = 0; i < cur.length; i++) sum += Math.abs(cur[i] - S.lastThumb[i]);
    const diff = sum / cur.length;
    S.lastThumb = cur;
    return diff < 0.6;
  }

  async function processRegion(canvas, region, opts) {
    opts = opts || {};
    const t0 = now();
    const tPre = now();
    const pre = OcrUtil.preprocess(canvas, region, CFG.scale, CFG.binarize);
    let lines;
    if (CFG.engine === 'projection') {
      lines = await S.ocr.recognizeProjection(pre.gray, pre.w, pre.h);
    } else {
      lines = await S.ocr.recognize(pre.gray, pre.w, pre.h, { maxLines: 40 });
    }
    S.stat.ocr = now() - tPre;

    if (!lines.length) {
      setStatus('未识别到文字', 'dim');
      return 0;
    }
    lines = lines.map(fixOcr);

    if (!opts.force && S.firstScan && CFG.skip_first) {
      lines.forEach(t => S.seen.set(t, Date.now()));
      S.firstScan = false;
      setStatus('已建立基线，等待新消息…', 'ok');
      return 0;
    }
    S.firstScan = false;

    const newLines = [];
    for (const t of lines) {
      if (S.seen.has(t)) { S.seen.delete(t); S.seen.set(t, Date.now()); continue; }
      newLines.push(t);
    }
    if (!newLines.length) {
      setStatus('OCR ' + S.stat.ocr.toFixed(0) + 'ms · 无新消息', 'dim');
      return 0;
    }

    const tTr = now();
    let outs;
    try {
      outs = await TR.translateBatch(newLines);
    } catch (e) {
      outs = newLines.map(() => '');
      toast('翻译失败：' + e.message, 'err');
    }
    S.stat.tr = now() - tTr;
    S.stat.newN = newLines.length;
    S.stat.loop = now() - t0;

    newLines.forEach((src, i) => {
      S.seen.set(src, Date.now());
      addLine(src, outs[i] || '');
    });
    while (S.seen.size > 400) {
      const k = S.seen.keys().next().value;
      S.seen.delete(k);
    }
    setStatus('OCR ' + S.stat.ocr.toFixed(0) + 'ms · 翻译 ' + S.stat.tr.toFixed(0) + 'ms · 新增 ' + newLines.length + ' 条', 'ok');
    return newLines.length;
  }

  /* 贴图 / 拖拽 / 剪贴板 / 选文件：不依赖屏幕共享，走同一套 OCR→翻译→悬浮窗 */
  async function ingestImage(src) {
    if (!S.ocrReady) { toast('OCR 模型还在加载，稍等', 'err'); return; }
    if (S.busy) { toast('正在处理上一张，稍等', 'dim'); return; }
    S.busy = true;
    try {
      let bmp = src;
      if (src instanceof Blob) bmp = await createImageBitmap(src);
      const w = bmp.width || bmp.naturalWidth;
      const h = bmp.height || bmp.naturalHeight;
      if (!w || !h) throw new Error('图片尺寸无效');
      const cv = document.createElement('canvas');
      cv.width = w;
      cv.height = h;
      cv.getContext('2d').drawImage(bmp, 0, 0);
      if (bmp.close) bmp.close();
      setStatus('已载入截图 ' + w + '×' + h + '，识别中…', 'dim');
      await processRegion(cv, { x: 0, y: 0, w: w, h: h }, { force: true });
    } catch (e) {
      setStatus('图片处理失败：' + e.message, 'err');
    } finally {
      S.busy = false;
      setStat();
    }
  }

  async function tick() {
    if (!S.running || S.busy) return;
    if (!S.region) return;
    S.busy = true;
    try {
      const g = grabRegion();
      if (!g) return;

      if (CFG.skip_static && staticCheck(g.canvas, g.region)) {
        setStatus('画面无变化，等待中…', 'dim');
        return;
      }

      await processRegion(g.canvas, g.region, {});
    } catch (e) {
      setStatus('识别异常：' + e.message, 'err');
    } finally {
      S.busy = false;
      setStat();
    }
  }

  function startScan() {
    if (S.running) return;
    if (!S.stream) { toast('先选择要捕获的画面', 'err'); return; }
    if (!S.region) { toast('先在画面上框选聊天区域', 'err'); return; }
    if (!S.ocrReady) { toast('OCR 模型还在加载，稍等', 'err'); return; }
    S.running = true;
    S.firstScan = true;
    S.lastThumb = null;
    $('#btn-start').classList.add('hidden');
    $('#btn-stop').classList.remove('hidden');
    document.body.classList.add('running');
    setStatus('运行中', 'ok');
    const loop = async () => {
      if (!S.running) return;
      await tick();
      S.timer = setTimeout(loop, Math.max(200, CFG.interval_ms));
    };
    loop();
    keepAwake(true);
  }

  function stopScan() {
    S.running = false;
    if (S.timer) { clearTimeout(S.timer); S.timer = null; }
    $('#btn-start').classList.remove('hidden');
    $('#btn-stop').classList.add('hidden');
    document.body.classList.remove('running');
    setStatus('已停止', 'dim');
    keepAwake(false);
  }

  /* ============================================================
   * 9. 后台保活（对抗浏览器标签页节流）
   * ============================================================ */
  async function keepAwake(on) {
    try {
      if (on && 'wakeLock' in navigator) {
        S.wakeLock = await navigator.wakeLock.request('screen');
      } else if (S.wakeLock) {
        await S.wakeLock.release();
        S.wakeLock = null;
      }
    } catch (e) {}
  }

  /* 播放极低音量音频，让浏览器认为本页在放音，避免隐藏标签页被节流到 1 次/分钟 */
  function startKeepAlive() {
    if (S.audioCtx) return;
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      const ctx = new AC();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      gain.gain.value = 0.0001;   // 几乎无声
      osc.frequency.value = 20000;
      osc.connect(gain).connect(ctx.destination);
      osc.start();
      S.audioCtx = ctx;
    } catch (e) {}
  }

  function resumeAudio() {
    if (S.audioCtx && S.audioCtx.state === 'suspended') S.audioCtx.resume().catch(() => {});
  }

  /* ============================================================
   * 10. 置顶悬浮窗（Document Picture-in-Picture）
   * ============================================================ */
  const PIP_CSS = `
:root{--bg:#0d0f14;--bg2:#141821;--fg:#eef1f6;--dim:#8b94a5;--acc:#00e0a0;--line:#232a36}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:rgba(13,15,20,var(--alpha,.95));color:var(--fg);font:400 14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;overflow:hidden}
.hd{display:flex;align-items:center;gap:8px;padding:8px 10px;border-bottom:1px solid var(--line);font-size:12px;color:var(--dim);user-select:none;cursor:default}
.hd .dot{width:7px;height:7px;border-radius:50%;background:var(--acc);box-shadow:0 0 8px var(--acc)}
.hd .t{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hd .x{cursor:pointer;padding:0 4px;border-radius:4px}
.hd .x:hover{background:#242c3a;color:#fff}
.plist{height:calc(100% - 34px);overflow-y:auto;padding:8px;scroll-behavior:smooth}
.line{padding:6px 8px;border-radius:8px;margin-bottom:6px;background:rgba(255,255,255,.03);cursor:pointer;transition:background .12s}
.line:hover{background:rgba(0,224,160,.08)}
.zh{color:#fff;font-weight:600;font-size:var(--fs,15px);word-break:break-word}
.en{color:var(--dim);font-size:calc(var(--fs,15px) - 3px);margin-top:2px;word-break:break-word}
.empty{color:var(--dim);text-align:center;padding:24px 8px;font-size:13px}
.plist::-webkit-scrollbar{width:6px}
.plist::-webkit-scrollbar-thumb{background:#2b3442;border-radius:3px}
`;

  async function openPip() {
    if (!('documentPictureInPicture' in window)) {
      toast('当前浏览器不支持置顶悬浮窗（需 Chrome / Edge 116+），译文请在本页面查看', 'err');
      return;
    }
    if (S.pipWin && !S.pipWin.closed) { S.pipWin.focus(); return; }
    try {
      const win = await documentPictureInPicture.requestWindow({ width: 400, height: 340 });
      S.pipWin = win;
      const style = win.document.createElement('style');
      style.textContent = PIP_CSS;
      win.document.head.appendChild(style);
      win.document.body.style.setProperty('--alpha', String(CFG.alpha));
      win.document.body.style.setProperty('--fs', CFG.font + 'px');

      const root = win.document.createElement('div');
      root.innerHTML = `<div class="hd"><span class="dot"></span><span class="t">MapleChat · 译文</span><span class="x" title="清空">清空</span></div><div class="plist"></div>`;
      win.document.body.appendChild(root);
      S.pipRoot = root;

      const list = root.querySelector('.plist');
      if (!S.lines.length) list.innerHTML = '<div class="empty">等待新消息…</div>';
      else S.lines.slice(-50).forEach(rec => {
        const row = win.document.createElement('div'); row.className = 'line';
        row.innerHTML = `<div class="zh">${esc(rec.dst || '—')}</div><div class="en">${esc(rec.src)}</div>`;
        row.addEventListener('click', () => navigator.clipboard.writeText(rec.dst || rec.src).then(() => toast('已复制')));
        list.appendChild(row);
      });

      root.querySelector('.x').addEventListener('click', () => { clearLines(); list.innerHTML = '<div class="empty">已清空</div>'; });

      // 滚轮调透明度，Ctrl+滚轮调字号
      win.document.body.addEventListener('wheel', e => {
        e.preventDefault();
        if (e.ctrlKey) {
          CFG.font = Math.min(28, Math.max(11, CFG.font + (e.deltaY < 0 ? 1 : -1)));
          win.document.body.style.setProperty('--fs', CFG.font + 'px');
        } else {
          CFG.alpha = Math.min(1, Math.max(0.35, +(CFG.alpha + (e.deltaY < 0 ? 0.05 : -0.05)).toFixed(2)));
          win.document.body.style.setProperty('--alpha', String(CFG.alpha));
        }
        saveCfg();
      }, { passive: false });

      // 双击复制全部
      win.document.body.addEventListener('dblclick', () => {
        const txt = S.lines.map(r => r.dst || r.src).join('\n');
        if (txt) navigator.clipboard.writeText(txt).then(() => toast('已复制全部译文'));
      });

      win.addEventListener('pagehide', () => { S.pipWin = null; S.pipRoot = null; $('#btn-pip').classList.remove('on'); });
      $('#btn-pip').classList.add('on');
      toast('悬浮窗已弹出，可拖到游戏画面上方');
    } catch (e) {
      toast('悬浮窗打开失败：' + e.message, 'err');
    }
  }

  /* ============================================================
   * 11. OCR 初始化
   * ============================================================ */
  async function initOcr() {
    if (S.ocrReady) return;
    setStatus('正在加载 OCR 模型（首次约 16MB）…', 'dim');
    $('#btn-start').disabled = true;
    try {
      const o = new PPOcr();
      const n = await o.init('./', msg => setStatus(msg, 'dim'));
      await o.warmup();
      S.ocr = o;
      S.ocrReady = true;
      setStatus('模型就绪 · 字符集 ' + n, 'ok');
      $('#btn-start').disabled = false;
      $('#model-badge').textContent = 'PP-OCRv4 · 就绪';
      $('#model-badge').classList.add('ok');
    } catch (e) {
      setStatus('模型加载失败：' + e.message, 'err');
      toast('OCR 模型加载失败：' + e.message, 'err');
      $('#model-badge').textContent = 'PP-OCRv4 · 失败';
    }
  }

  /* ============================================================
   * 12. 设置面板
   * ============================================================ */
  function bindSettings() {
    const open = () => { $('#drawer-settings').classList.add('open'); $('#mask').classList.add('open'); };
    const close = () => { $$('.drawer').forEach(d => d.classList.remove('open')); $('#mask').classList.remove('open'); };

    $('#btn-settings').addEventListener('click', open);
    $$('[data-close]').forEach(b => b.addEventListener('click', close));
    $('#mask').addEventListener('click', close);
    document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });

    // 回填
    $('#in-key').value = API_KEY;
    $('#in-base').value = CFG.api_base;
    $('#in-model').value = CFG.model;
    $('#in-interval').value = CFG.interval_ms;
    $('#in-scale').value = CFG.scale;
    $('#in-engine').value = CFG.engine;
    $('#in-skipfirst').checked = CFG.skip_first;
    $('#in-static').checked = CFG.skip_static;
    $('#in-terms').checked = CFG.terms_enabled;
    $('#in-bin').checked = CFG.binarize;

    $('#btn-save-key').addEventListener('click', () => {
      API_KEY = $('#in-key').value.trim();
      localStorage.setItem(LS.key, API_KEY);
      CFG.api_base = $('#in-base').value.trim() || DEFAULTS.api_base;
      CFG.model = $('#in-model').value.trim() || DEFAULTS.model;
      saveCfg();
      toast(API_KEY ? '已保存到本机浏览器' : '已清空 Key');
    });

    $('#in-interval').addEventListener('input', e => {
      CFG.interval_ms = Math.max(300, parseInt(e.target.value, 10) || 900);
      $('#interval-val').textContent = CFG.interval_ms + 'ms';
      saveCfg();
    });
    $('#in-scale').addEventListener('input', e => {
      CFG.scale = Math.max(1, parseFloat(e.target.value) || 2);
      $('#scale-val').textContent = CFG.scale.toFixed(1) + '×';
      saveCfg();
    });
    $('#in-engine').addEventListener('change', e => { CFG.engine = e.target.value; saveCfg(); });
    $('#in-skipfirst').addEventListener('change', e => { CFG.skip_first = e.target.checked; saveCfg(); });
    $('#in-static').addEventListener('change', e => { CFG.skip_static = e.target.checked; saveCfg(); });
    $('#in-bin').addEventListener('change', e => { CFG.binarize = e.target.checked; saveCfg(); });
    $('#in-terms').addEventListener('change', e => {
      CFG.terms_enabled = e.target.checked;
      Terms.setEnabled(e.target.checked);
      saveCfg();
      toast(e.target.checked ? '术语库已启用' : '术语库已关闭');
    });

    $('#btn-test').addEventListener('click', async () => {
      const out = $('#test-out');
      const key = ($('#in-key').value || '').trim();
      const base = ($('#in-base').value || '').trim() || DEFAULTS.api_base;
      const model = ($('#in-model').value || '').trim() || DEFAULTS.model;
      if (!key) { out.textContent = '请先填入 API Key 再测试'; return; }
      out.textContent = '测试中…';
      const t0 = performance.now();
      try {
        const r = await fetch(base.replace(/\/+$/, '') + '/chat/completions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key },
          body: JSON.stringify({
            model: model,
            messages: [{ role: 'user', content: '回复 ok 两个字母，不要其他内容' }],
            max_tokens: 8,
            temperature: 0
          })
        });
        const j = await r.json().catch(function () { return {}; });
        if (!r.ok) throw new Error((j.error && j.error.message) || ('HTTP ' + r.status));
        const txt = ((j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content) || '').trim();
        out.textContent = '连通成功 · ' + Math.round(performance.now() - t0) + 'ms · 回复「' + txt.slice(0, 40) + '」';
      } catch (e) {
        out.textContent = '失败：' + e.message + '（检查 Key、网络，或该网络是否拦截 API 域名）';
      }
    });

    $('#model-info').textContent = '';
    $('#btn-reset-region').addEventListener('click', () => {
      S.region = null;
      saveJSON(LS.region, null);
      $('#sel-box').style.display = 'none';
      $('#region-info').textContent = '未框选';
      $('#step3').classList.remove('active');
      toast('选区已清除，重新拖一次');
      close();
    });
  }

  /* ============================================================
   * 13. 组句面板
   * ============================================================ */
  function bindQuickGen() {
    const D = window.__CHATGEN__;
    if (!D) return;
    const rev = S.cg.buildRev(Terms.all || {});
    let mode = 'trade', action = 'buy';

    const box = $('#scenes');
    D.SCENES.forEach(([key, label]) => {
      const b = el('button', 'chip', label);
      b.dataset.mode = key;
      b.addEventListener('click', () => {
        mode = key;
        $$('.chip[data-mode]', box).forEach(x => x.classList.toggle('on', x.dataset.mode === key));
        renderActions();
      });
      box.appendChild(b);
    });
    $$('.chip[data-mode]', box)[0].classList.add('on');

    function renderActions() {
      const wrap = $('#actions');
      wrap.innerHTML = '';
      const acts = D.ACTIONS[mode] || [];
      action = acts.length ? acts[0][0] : '';
      acts.forEach(([key, label]) => {
        const b = el('button', 'chip sm', label);
        b.dataset.act = key;
        if (key === action) b.classList.add('on');
        b.addEventListener('click', () => {
          action = key;
          $$('.chip[data-act]', wrap).forEach(x => x.classList.toggle('on', x.dataset.act === key));
          renderFields(); update();
        });
        wrap.appendChild(b);
      });
      renderFields();
      update();
    }

    function renderFields() {
      const wrap = $('#fields');
      wrap.innerHTML = '';
      const fs = D.FIELDS[mode] || [];
      fs.forEach(([key, label, ph]) => {
        const row = el('label', 'field');
        row.innerHTML = `<span>${esc(label)}</span>`;
        const inp = el('input');
        inp.dataset.f = key;
        inp.placeholder = ph || '';
        inp.addEventListener('input', update);
        row.appendChild(inp);
        wrap.appendChild(row);
      });
      const chips = el('div', 'chiprow');
      D.CHIPS.forEach(c => {
        const b = el('button', 'chip xs', c);
        b.addEventListener('click', () => {
          const e = $('input[data-f="extra"]', wrap);
          if (e) { e.value = (e.value ? e.value + ' ' : '') + c; update(); }
        });
        chips.appendChild(b);
      });
      wrap.appendChild(chips);
    }

    function collect() {
      const o = {};
      $$('#fields input[data-f]').forEach(i => { o[i.dataset.f] = i.value; });
      return o;
    }

    function update() {
      const f = collect();
      // 目标里的中文自动转英文
      if (f.target && /[\u4e00-\u9fff]/.test(f.target)) {
        const a = S.cg.autoEn(f.target, rev);
        if (a.text !== f.target) {
          const inp = $('#fields input[data-f="target"]');
          if (inp) { inp.value = a.text; f.target = a.text; }
        }
      }
      const r = S.cg.build(mode, action, f);
      $('#qg-out').value = r.english || '';
      $('#qg-zh').textContent = S.cg.zhPreview(mode, action, f);
      $('#qg-warn').textContent = r.warning || '';
      $('#qg-warn').classList.toggle('show', !!r.warning);
      $('#btn-qg-copy').disabled = !r.english;
    }

    $('#btn-qg').addEventListener('click', () => {
      $('#drawer-qg').classList.add('open');
      $('#mask').classList.add('open');
      update();
    });
    $('#btn-qg-copy').addEventListener('click', () => {
      const v = $('#qg-out').value;
      if (!v) return;
      navigator.clipboard.writeText(v).then(() => toast('已复制，回游戏 Ctrl+V'));
    });
    $('#qg-out').addEventListener('click', () => $('#qg-out').select());

    renderActions();
  }

  /* ============================================================
   * 14. 反向翻译
   * ============================================================ */
  function bindReverse() {
    const inp = $('#rev-in'), out = $('#rev-out');
    let timer = null;
    async function go() {
      const v = inp.value.trim();
      if (!v) { out.value = ''; return; }
      try {
        const r = await TR.reverse(v);
        out.value = r || '（无结果）';
      } catch (e) {
        out.value = '失败：' + e.message;
      }
    }
    inp.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(go, 700);
    });
    $('#btn-rev-copy').addEventListener('click', () => {
      if (!out.value) return;
      navigator.clipboard.writeText(out.value).then(() => toast('已复制，回游戏 Ctrl+V'));
    });
  }

  /* ============================================================
   * 15. 启动
   * ============================================================ */
  async function boot() {
    S.video = $('#video');
    S.frameCanvas = document.createElement('canvas');

    // 组句数据
    try {
      window.__CHATGEN__ = await fetch('data/chatgen.json').then(r => r.json());
      S.cg = makeChatGen(window.__CHATGEN__);
    } catch (e) {
      toast('组句数据加载失败：' + e.message, 'err');
    }

    const meta = await Terms.load(CFG.terms_enabled);
    $('#terms-badge').textContent = meta.count ? `术语 ${meta.count} 条` : '术语库缺失';

    window.__MC__ = { S: S, CFG: CFG, tick: tick, startScan: startScan, stopScan: stopScan, setApiKey: function (k) { API_KEY = k; }, ingest: function (x) { return ingestImage(x); } };
    bindSettings();
    initSelector();
    bindReverse();
    if (S.cg) bindQuickGen();

    $('#btn-pick').addEventListener('click', pickSource);
    $('#btn-paste').addEventListener('click', async () => {
      try {
        const items = await navigator.clipboard.read();
        for (const it of items) {
          const t = (it.types || []).find(x => x.indexOf('image/') === 0);
          if (t) { ingestImage(await it.getType(t)); return; }
        }
        toast('剪贴板里没有图片：先 Win+Shift+S 截图，再点这里或直接 Ctrl+V', 'err');
      } catch (e) {
        toast('浏览器不允许读剪贴板，请直接按 Ctrl+V 粘贴截图', 'err');
      }
    });
    $('#file-image').addEventListener('change', e => {
      const f = e.target.files && e.target.files[0];
      if (f) ingestImage(f);
      e.target.value = '';
    });
    document.addEventListener('paste', e => {
      const items = (e.clipboardData && e.clipboardData.items) || [];
      for (const it of items) {
        if (it.type && it.type.indexOf('image/') === 0) {
          const f = it.getAsFile();
          if (f) { e.preventDefault(); ingestImage(f); return; }
        }
      }
    });
    document.addEventListener('dragover', e => e.preventDefault());
    document.addEventListener('drop', e => {
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f && /^image\//.test(f.type)) { e.preventDefault(); ingestImage(f); }
    });
    $('#btn-start').addEventListener('click', startScan);
    $('#btn-stop').addEventListener('click', stopScan);
    $('#btn-pip').addEventListener('click', openPip);
    $('#btn-clear').addEventListener('click', () => { clearLines(); toast('已清空'); });

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) resumeAudio();
    });
    window.addEventListener('click', resumeAudio, { once: true });

    $('#in-interval').dispatchEvent(new Event('input'));
    $('#in-scale').dispatchEvent(new Event('input'));
    renderRegionInfo();

    // 模型异步加载，不阻塞界面
    initOcr();
  }

  window.addEventListener('DOMContentLoaded', boot);
})();
