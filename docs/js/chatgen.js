/*!
 * chatgen.js — 快捷组句引擎（chatgen.py 的浏览器移植）
 * 8 大场景 × 51 个动作，本地模板拼句，零 API 消耗
 */
(function (global) {
  'use strict';

  const CN_RE = /[\u4e00-\u9fff]/;

  function k(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); }

  function q(parts) {
    const s = parts.map(k).filter(Boolean).join(' ');
    return s.replace(/\s+([?!,.])/g, '$1').trim();
  }

  const EXACT_ABBR = /^[a-z]{2,5}$/;

  function makeChatGen(D) {
    const NO_TARGET = new Set((D.NO_TARGET || []).map(p => p[0] + '|' + (p[1] === null ? '' : p[1])));

    /* ---------- 本地组句 ---------- */
    function build(mode, action, f) {
      f = f || {};
      const t = k(f.target), r = k(f.role), ch = k(f.channel), sl = k(f.slots), x = k(f.extra);
      let en = '', warn = null;

      if (mode === 'trade') {
        const pre = { buy: 'B>', sell: 'S>', trade: 'T>', lf: 'LF>', pc: 'PC>' }[action];
        if (action === 'offer') en = q(['offer', x || t]);
        else if (action === 'pc') en = q([pre, t]);
        else en = q([pre, t, x]);
      } else if (mode === 'party') {
        if (action === 'lfp') en = q(['LFP', t, x]);
        else if (action === 'recruit') en = q(['R>', t, 'party', x]);
        else if (action === 'join') en = q(['J>', t, x]);
        else if (action === 'need') en = q(['Need', t, x]);
        else if (action === 'full') en = 'Full';
        else if (action === 'ready') en = 'Ready';
      } else if (mode === 'leech') {
        const m = {
          buyingLeech: 'Buying leech', sellingLeech: 'Selling leech',
          leechSeller: 'LF> leech', leechBuyer: 'R> leech buyer',
        }[action];
        if (m) en = q([m, t, sl, x]);
      } else if (mode === 'pq') {
        if (action === 'lfp') en = q(['LFP', t, x]);
        else if (action === 'recruit') en = q(['R>', t, r, x]);
        else if (action === 'need') en = q(['Need', r || t, (t && r) ? 'for ' + t : '', x]);
        else if (action === 'hosting') en = q(['Hosting', t, x]);
        else if (action === 'join') en = t ? `Can I join ${t}?` : 'Can I join?';
        else if (action === 'ready') en = t ? `Ready for ${t}` : 'Ready';
      } else if (mode === 'location') {
        if (action === 'askChannel') en = 'What cc are you in?';
        else if (action === 'tellChannel') en = q(['I am in', ch]);
        else if (action === 'askLocation') en = 'Where are you?';
        else if (action === 'tellLocation') en = q(['I am at', t, ch, x]);
        else if (action === 'comeHere') en = q(['Come to', t, ch, x]);
        else if (action === 'arrived') en = q(['I am here', t, ch, x]);
      } else if (mode === 'skill') {
        if (action === 'need') en = q(['Need', t, x]);
        else if (action === 'canUse') en = q(['Can you use', t, x]).replace(/\s+\?/g, '') + '?';
        else if (action === 'have') en = q(['I have', t]);
        else if (action === 'noSkill') en = q(['No', t]);
        else if (action === 'cooldown') en = q([t, 'cooldown']);
      } else if (mode === 'server') {
        const base = {
          dc: 'DC', lag: 'Lagging', maintenance: 'Server check', backUp: 'Server is back up',
          down: 'Server is down', noEta: 'No ETA', reset: 'Server reset', stuck: 'Stuck',
        }[action] || '';
        en = q([base, x]);
      } else if (mode === 'custom') {
        const pre = action === 'free' ? '' : k(action);
        en = q([pre, t, x]);
      }

      en = k(en);

      const needTarget = !NO_TARGET.has(mode + '|' + action) && !NO_TARGET.has(mode + '|');
      const filled = !!(t || r || x || ch || sl);
      if (!filled && !en) warn = '先点动作、填个目标或补充内容。';
      else if (needTarget && !(t || r) && mode !== 'server' && mode !== 'custom') {
        warn = '这个动作用来问/说具体对象，请填目标（中文会自动转英文）。';
      }
      const valid = !!en && !warn;
      return { english: en, warning: warn, valid };
    }

    function zhPreview(mode, action, f) {
      f = f || {};
      const parts = [(D.ACTION_CN || {})[action] || action];
      const pairs = [['目标', f.target], ['职业/技能', f.role], ['人数/位置', f.slots], ['频道', f.channel], ['补充', f.extra]];
      for (const [label, v] of pairs) if (k(v)) parts.push(label + '：' + k(v));
      return parts.join('，');
    }

    /* ---------- 中文 → 英文（术语反向索引 + EXTRA_REV） ---------- */
    function buildRev(flat) {
      const rev = {};
      for (const en in flat) {
        const zh = flat[en];
        for (const piece of String(zh).split(/[、,，/;；]/)) {
          const p = piece.trim();
          if (!p || !CN_RE.test(p)) continue;
          (rev[p] = rev[p] || []).push(en);
        }
      }
      for (const cn in (D.EXTRA_REV || {})) {
        const en = D.EXTRA_REV[cn];
        const lst = rev[cn] = rev[cn] || [];
        if (lst.indexOf(en) < 0) lst.unshift(en);
      }
      for (const p in rev) {
        rev[p].sort((a, b) => {
          const ra = EXACT_ABBR.test(a) ? 0 : 1, rb = EXACT_ABBR.test(b) ? 0 : 1;
          return ra - rb || a.length - b.length;
        });
      }
      return rev;
    }

    function autoEn(text, rev, maxReplace) {
      maxReplace = maxReplace || 3;
      const s = k(text);
      if (!s || !CN_RE.test(s)) return { text: s, left: [] };
      if (rev[s] && rev[s].length) return { text: rev[s][0], left: [] };
      let out = s;
      const used = [];
      const keys = Object.keys(rev).sort((a, b) => b.length - a.length);
      for (const key of keys) {
        if (used.length >= maxReplace) break;
        if (key && out.indexOf(key) >= 0) {
          out = out.replace(key, rev[key][0]);
          used.push(key);
        }
      }
      const left = out.match(/[\u4e00-\u9fff]/g) || [];
      return { text: k(out), left };
    }

    return { build, zhPreview, buildRev, autoEn, SCENES: D.SCENES, ACTIONS: D.ACTIONS, ACTION_CN: D.ACTION_CN, FIELDS: D.FIELDS, CHIPS: D.CHIPS };
  }

  global.makeChatGen = makeChatGen;
})(window);
