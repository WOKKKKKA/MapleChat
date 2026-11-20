/*!
 * ocr.js — 浏览器端 PP-OCRv4 推理引擎（det + rec）
 * 模型与 MapleChat 桌面版完全一致（ch_PP-OCRv4_det_infer.onnx / ch_PP-OCRv4_rec_infer.onnx）
 * 依赖：onnxruntime-web（vendor/ort.wasm.min.js，全局 ort）
 *
 * 后处理说明：
 *  - det 输出概率图 → 阈值二值化 → 水平膨胀（把同一行的单词连成一条）→ 连通域 → 行框
 *  - rec CTC 贪心解码，字符集来自模型 metadata（导出为 models/keys.txt）+ blank + 空格
 */
(function (global) {
  'use strict';

  const DET_LIMIT = 736;      // limit_side_len, limit_type=min
  const DET_MAX_SIDE = 1600;
  const DET_THRESH = 0.30;    // 概率图二值化阈值
  const DET_BOX_THRESH = 0.50;
  const UNCLIP = 1.6;
  const REC_H = 48;
  const REC_MAX_W = 800;   // 允许更宽输入减少压缩掉字（PP-OCRv4 rec 支持动态宽度）

  /* ---------------- 小工具：canvas ---------------- */
  function makeCanvas(w, h) {
    if (typeof OffscreenCanvas !== 'undefined') return new OffscreenCanvas(w, h);
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    return c;
  }

  function ctx2d(cv) {
    return cv.getContext('2d', { willReadFrequently: true });
  }

  /* ImageData → 缩放后的 ImageData（用 canvas 双线性） */
  function resizeImageData(src, w, h) {
    const sc = makeCanvas(src.width, src.height);
    ctx2d(sc).putImageData(src, 0, 0);
    const dc = makeCanvas(w, h);
    const dctx = ctx2d(dc);
    dctx.imageSmoothingEnabled = true;
    dctx.imageSmoothingQuality = 'high';
    dctx.drawImage(sc, 0, 0, w, h);
    return dctx.getImageData(0, 0, w, h);
  }

  /* RGB → 灰度（返回 Uint8ClampedArray） */
  function toGray(imgData) {
    const d = imgData.data, n = imgData.width * imgData.height;
    const g = new Uint8ClampedArray(n);
    for (let i = 0, j = 0; i < n; i++, j += 4) {
      g[i] = (d[j] * 0.299 + d[j + 1] * 0.587 + d[j + 2] * 0.114) | 0;
    }
    return g;
  }

  function meanOf(g) {
    let s = 0;
    for (let i = 0; i < g.length; i++) s += g[i];
    return s / g.length;
  }

  /* 灰度 → 归一化 NCHW Float32（mean/std = 0.5 对应 (x/255-0.5)/0.5）
     用于 rec；det 走下面 detTensor（ImageNet 均值方差） */
  function grayToTensor(g, w, h, out) {
    const t = out || new Float32Array(3 * w * h);
    const n = w * h;
    for (let i = 0; i < n; i++) {
      const v = (g[i] / 255 - 0.5) / 0.5;
      t[i] = v; t[n + i] = v; t[2 * n + i] = v;
    }
    return t;
  }

  /* det 专用：PaddleOCR 检测前处理 mean=[.485,.456,.406] std=[.229,.224,.225] */
  const DET_MEAN = [0.485, 0.456, 0.406];
  const DET_STD = [0.229, 0.224, 0.225];
  function detTensor(g, w, h) {
    const n = w * h;
    const t = new Float32Array(3 * n);
    for (let i = 0; i < n; i++) {
      const v = g[i] / 255;
      t[i] = (v - DET_MEAN[0]) / DET_STD[0];
      t[n + i] = (v - DET_MEAN[1]) / DET_STD[1];
      t[2 * n + i] = (v - DET_MEAN[2]) / DET_STD[2];
    }
    return t;
  }

  /* ---------------- Otsu 兜底二值化（投影模式用） ---------------- */
  function otsu(g) {
    const hist = new Uint32Array(256);
    for (let i = 0; i < g.length; i++) hist[g[i]]++;
    const total = g.length;
    let sum = 0;
    for (let i = 0; i < 256; i++) sum += i * hist[i];
    let sumB = 0, wB = 0, best = 0, thr = 128;
    for (let t = 0; t < 256; t++) {
      wB += hist[t];
      if (!wB) continue;
      const wF = total - wB;
      if (!wF) break;
      sumB += t * hist[t];
      const mB = sumB / wB, mF = (sum - sumB) / wF;
      const between = wB * wF * (mB - mF) * (mB - mF);
      if (between > best) { best = between; thr = t; }
    }
    return thr;
  }

  /* 用行投影估计典型文字行高（取有前景的连续行段高度中位数） */
  function estimateLineHeight(bin, w, h) {
    const rowSum = new Int32Array(h);
    for (let y = 0; y < h; y++) {
      const row = y * w;
      let s = 0;
      for (let x = 0; x < w; x++) s += bin[row + x];
      rowSum[y] = s;
    }
    const minPix = Math.max(2, Math.round(w * 0.002));
    const bands = [];
    let cur = -1;
    for (let y = 0; y < h; y++) {
      if (rowSum[y] >= minPix) { if (cur < 0) cur = y; }
      else if (cur >= 0) { bands.push(y - cur); cur = -1; }
    }
    if (cur >= 0) bands.push(h - cur);
    const ok = bands.filter(v => v >= 4 && v <= h / 2).sort((a, b) => a - b);
    if (!ok.length) return Math.max(8, Math.round(h / 12));
    return ok[(ok.length / 2) | 0];
  }

  /* ---------------- 连通域（在膨胀后的 mask 上找行框） ---------------- */
  /* mask = 水平膨胀后的掩码；bin = 未膨胀掩码。评分只在 bin 上前景像素取平均，
     否则膨胀补出来的背景像素（概率≈0）会把分数拉到阈值以下，导致漏框。 */
  function findBoxes(mask, bin, w, h, scoreMap) {
    const label = new Int32Array(w * h).fill(-1);
    const stack = new Int32Array(w * h);
    const boxes = [];
    let id = 0;
    for (let start = 0; start < w * h; start++) {
      if (mask[start] !== 1 || label[start] !== -1) continue;
      let sp = 0;
      stack[sp++] = start;
      label[start] = id;
      let minX = w, maxX = 0, minY = h, maxY = 0, cnt = 0, fg = 0, scoreSum = 0;
      while (sp > 0) {
        const p = stack[--sp];
        const y = (p / w) | 0, x = p - y * w;
        cnt++;
        if (bin[p]) { fg++; scoreSum += scoreMap[p]; }
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
        // 4 邻域：垂直方向不扩散，避免上下相邻两行被斜角串成一块
        if (x > 0) { const q = p - 1; if (mask[q] === 1 && label[q] === -1) { label[q] = id; stack[sp++] = q; } }
        if (x < w - 1) { const q = p + 1; if (mask[q] === 1 && label[q] === -1) { label[q] = id; stack[sp++] = q; } }
        if (y > 0) { const q = p - w; if (mask[q] === 1 && label[q] === -1) { label[q] = id; stack[sp++] = q; } }
        if (y < h - 1) { const q = p + w; if (mask[q] === 1 && label[q] === -1) { label[q] = id; stack[sp++] = q; } }
      }
      id++;
      if (fg < 6) continue;
      const bw = maxX - minX + 1, bh = maxY - minY + 1;
      if (bw < 4 || bh < 4) continue;
      const score = fg ? scoreSum / fg : 0;
      if (score < DET_BOX_THRESH) continue;
      boxes.push({ x0: minX, y0: minY, x1: maxX + 1, y1: maxY + 1, score });
    }
    return boxes;
  }

  /* 水平膨胀：把同一行的字连成一条（用行内滑动窗口 OR，O(N)） */
  function dilateH(bin, w, h, rad) {
    const out = new Uint8Array(w * h);
    for (let y = 0; y < h; y++) {
      const row = y * w;
      let last = -1e9;
      // 从左到右：记录最近的 1 的位置
      const near = new Int32Array(w).fill(-1e9);
      for (let x = 0; x < w; x++) {
        if (bin[row + x]) last = x;
        near[x] = last;
      }
      let next = 1e9;
      const near2 = new Int32Array(w).fill(1e9);
      for (let x = w - 1; x >= 0; x--) {
        if (bin[row + x]) next = x;
        near2[x] = next;
      }
      for (let x = 0; x < w; x++) {
        if (bin[row + x] || x - near[x] <= rad || near2[x] - x <= rad) out[row + x] = 1;
      }
    }
    return out;
  }

  /* ---------------- 主类 ---------------- */
  class PPOcr {
    constructor() {
      this.det = null;
      this.rec = null;
      this.keys = null;
      this.ready = false;
      this.stat = { detMs: 0, recMs: 0, n: 0 };
    }

    async init(base, onProgress) {
      if (typeof ort === 'undefined') throw new Error('onnxruntime-web 未加载');
      // 统一解析成绝对 URL：项目页部署在 /MapleChat/ 这类子路径下也不会错
      const abs = p => new URL(base + p, document.baseURI).href;
      const vendor = abs('vendor/');
      // GitHub Pages 无法设置 COOP/COEP，只能单线程
      ort.env.wasm.numThreads = 1;
      ort.env.wasm.simd = true;
      ort.env.wasm.proxy = false;
      // 用对象形式给完整文件名，避免各版本 wasmPaths 目录拼接差异
      ort.env.wasm.wasmPaths = {
        mjs: vendor + 'ort-wasm-simd-threaded.mjs',
        wasm: vendor + 'ort-wasm-simd-threaded.wasm',
      };

      const opt = {
        executionProviders: ['wasm'],
        graphOptimizationLevel: 'all',
        intraOpNumThreads: 1,
      };

      const step = async (msg, fn) => {
        if (onProgress) onProgress(msg);
        return fn();
      };

      this.det = await step('加载检测模型 (4.7MB)…', () =>
        ort.InferenceSession.create(base + 'models/ch_PP-OCRv4_det_infer.onnx', opt));
      this.rec = await step('加载识别模型 (10.9MB)…', () =>
        ort.InferenceSession.create(base + 'models/ch_PP-OCRv4_rec_infer.onnx', opt));

      const txt = await step('加载字符集…', () =>
        fetch(base + 'models/keys.txt').then(r => r.text()));
      // 顺序必须与训练一致：['blank'] + dict + [' ']
      this.keys = ['\u0000'].concat(txt.split('\n')).concat([' ']);
      this.ready = true;
      return this.keys.length;
    }

    /* 预热：首帧编译开销大。尺寸必须 32 对齐，否则 det 的 FPN 拼接会直接 abort */
    async warmup() {
      try {
        const t1 = detTensor(new Uint8ClampedArray(64 * 256).fill(255), 256, 64);
        await this.det.run({ x: new ort.Tensor('float32', t1, [1, 3, 64, 256]) });
      } catch (e) { /* 预热失败不影响主流程 */ }
      try {
        const t2 = grayToTensor(new Uint8ClampedArray(REC_H * 64).fill(255), 64, REC_H);
        await this.rec.run({ x: new ort.Tensor('float32', t2, [1, 3, REC_H, 64]) });
      } catch (e) { /* 同上 */ }
      return true;
    }

    /* ---------- 检测 ---------- */
    async detect(gray, w, h) {
      const t0 = performance.now();
      // 缩放：min 边放大到 DET_LIMIT，最大边不超 DET_MAX_SIDE，尺寸取 32 的倍数
      let ratio = 1;
      const minSide = Math.min(w, h), maxSide = Math.max(w, h);
      if (minSide < DET_LIMIT) ratio = DET_LIMIT / minSide;
      if (maxSide * ratio > DET_MAX_SIDE) ratio = DET_MAX_SIDE / maxSide;
      if (ratio < 1) ratio = Math.max(ratio, 0.2);
      let rw = Math.max(32, Math.ceil((w * ratio) / 32) * 32);
      let rh = Math.max(32, Math.ceil((h * ratio) / 32) * 32);
      rw = Math.min(rw, DET_MAX_SIDE + 32);
      rh = Math.min(rh, DET_MAX_SIDE + 32);

      // 先把灰度图包成 ImageData 再缩放
      const tmp = new ImageData(w, h);
      for (let i = 0, j = 0; i < w * h; i++, j += 4) {
        tmp.data[j] = tmp.data[j + 1] = tmp.data[j + 2] = gray[i];
        tmp.data[j + 3] = 255;
      }
      const rImg = resizeImageData(tmp, rw, rh);
      const rg = toGray(rImg);
      const tensor = detTensor(rg, rw, rh);
      const out = await this.det.run({ x: new ort.Tensor('float32', tensor, [1, 3, rh, rw]) });
      const prob = out[this.det.outputNames[0]];
      const ph = prob.dims[prob.dims.length - 2];
      const pw = prob.dims[prob.dims.length - 1];
      const data = prob.data;

      // 二值化
      const bin = new Uint8Array(pw * ph);
      for (let i = 0; i < pw * ph; i++) bin[i] = data[i] > DET_THRESH ? 1 : 0;

      // 膨胀半径按“实测行高”定：同一行字间空隙约半个字宽，
      // 用整图高度换算会到几十像素，把相邻多行黏成一整块（上一版就是这个坑）。
      const lh = estimateLineHeight(bin, pw, ph);
      const rad = Math.max(3, Math.min(24, Math.round(lh * 0.55)));
      const dil = dilateH(bin, pw, ph, rad);

      let boxes = findBoxes(dil, bin, pw, ph, data);
      // 按 DB 的 unclip 规则外扩：dist = area * ratio / perimeter。
      // 早前用 max(bw,bh)*0.3，会把矮长框上下各撑几十像素，把邻行内容一起裁进去。
      const sx = w / pw, sy = h / ph;
      boxes = boxes.map(b => {
        const bw = b.x1 - b.x0, bh = b.y1 - b.y0;
        const dist = (bw * bh * UNCLIP) / (2 * (bw + bh) + 1e-6);
        const dx = Math.min(dist, bw * 0.35), dy = Math.min(dist, bh * 0.45);
        return {
          x0: Math.max(0, (b.x0 - dx) * sx),
          y0: Math.max(0, (b.y0 - dy) * sy),
          x1: Math.min(w, (b.x1 + dx) * sx),
          y1: Math.min(h, (b.y1 + dy) * sy),
          score: b.score,
        };
      }).filter(b => (b.x1 - b.x0) > 8 && (b.y1 - b.y0) > 6);

      boxes.sort((a, b) => (a.y0 + a.y1) / 2 - (b.y0 + b.y1) / 2);
      this.stat.detMs = performance.now() - t0;
      let pmax = 0, psum = 0;
      for (let i = 0; i < data.length; i++) { const v = data[i]; if (v > pmax) pmax = v; psum += v; }
      this.lastDbg = {
        in: [rw, rh], prob: [pw, ph], lh, rad, nbox: boxes.length,
        pmin: Math.min.apply(null, Array.from(data.slice(0, 4000))),
        pmax: +pmax.toFixed(3), pmean: +(psum / data.length).toFixed(4),
        boxes: boxes.slice(0, 10),
      };
      return boxes;
    }

    /* ---------- 单条识别 ---------- */
    async recLine(gray, w, h, box) {
      const bw = Math.max(2, Math.round(box.x1 - box.x0));
      const bh = Math.max(2, Math.round(box.y1 - box.y0));
      // 裁切
      const crop = new ImageData(bw, bh);
      const x0 = Math.max(0, Math.round(box.x0)), y0 = Math.max(0, Math.round(box.y0));
      for (let y = 0; y < bh; y++) {
        const sy = Math.min(h - 1, y0 + y);
        for (let x = 0; x < bw; x++) {
          const sx = Math.min(w - 1, x0 + x);
          const v = gray[sy * w + sx];
          const j = (y * bw + x) * 4;
          crop.data[j] = crop.data[j + 1] = crop.data[j + 2] = v;
          crop.data[j + 3] = 255;
        }
      }
      // 等比缩放到高 48
      const rw = Math.min(REC_MAX_W, Math.max(16, Math.ceil((bw * REC_H) / bh)));
      const rImg = resizeImageData(crop, rw, REC_H);
      const rg = toGray(rImg);
      const tensor = grayToTensor(rg, rw, REC_H);
      const out = await this.rec.run({
        x: new ort.Tensor('float32', tensor, [1, 3, REC_H, rw])
      });
      const o = out[this.rec.outputNames[0]];
      const T = o.dims[1], C = o.dims[2];
      const d = o.data;

      let text = '', scoreSum = 0, scoreN = 0, prev = -1;
      for (let t = 0; t < T; t++) {
        const base = t * C;
        let best = 0, bestI = 0;
        // blank 是 0，不需要参与 argmax 比较之外的逻辑
        for (let c = 0; c < C; c++) {
          const v = d[base + c];
          if (v > best) { best = v; bestI = c; }
        }
        if (bestI !== 0 && bestI !== prev) {
          const ch = this.keys[bestI] || '';
          if (ch && ch !== '\u0000') text += ch;
          scoreSum += best; scoreN++;
        }
        prev = bestI;
      }
      return { text: text.trim(), score: scoreN ? scoreSum / scoreN : 0 };
    }

    /* ---------- 整图识别（det + rec） ---------- */
    async recognize(gray, w, h, opts) {
      opts = opts || {};
      const maxLines = opts.maxLines || 40;
      const boxes = await this.detect(gray, w, h);
      if (!boxes.length) return [];
      const pick = boxes.slice(0, maxLines);
      const t0 = performance.now();
      const items = [];
      for (const b of pick) {
        const r = await this.recLine(gray, w, h, b);
        if (r.text && r.score >= 0.35) {
          items.push({ text: r.text, score: r.score, y: (b.y0 + b.y1) / 2, x: b.x0, hgt: b.y1 - b.y0 });
        }
      }
      this.stat.recMs = performance.now() - t0;
      this.stat.n = items.length;
      return this.mergeRows(items);
    }

    /* ---------- 投影模式（不跑 det，按行切分，速度极快） ---------- */
    async recognizeProjection(gray, w, h) {
      const t0 = performance.now();
      const thr = otsu(gray);
      // 暗底亮字已由 preprocess 处理成亮底暗字，这里判断极性
      let fg = 0;
      for (let i = 0; i < gray.length; i++) if (gray[i] < thr) fg++;
      const darker = fg < gray.length * 0.5;
      const bin = new Uint8Array(w * h);
      for (let i = 0; i < w * h; i++) {
        const v = gray[i];
        bin[i] = (darker ? v < thr : v > thr) ? 1 : 0;
      }
      // 行投影
      const rowSum = new Int32Array(h);
      for (let y = 0; y < h; y++) {
        let s = 0;
        for (let x = 0; x < w; x++) s += bin[y * w + x];
        rowSum[y] = s;
      }
      const minPix = Math.max(2, Math.round(w * 0.004));
      const bands = [];
      let cur = null;
      for (let y = 0; y < h; y++) {
        if (rowSum[y] >= minPix) {
          if (!cur) cur = { y0: y, y1: y };
          else cur.y1 = y;
        } else if (cur) {
          // 允许 2px 空隙
          let gap = 0, yy = y;
          while (yy < h && rowSum[yy] < minPix && gap < 3) { gap++; yy++; }
          if (gap >= 3 || yy >= h) { bands.push(cur); cur = null; }
        }
      }
      if (cur) bands.push(cur);

      const items = [];
      for (const b of bands) {
        const bh = b.y1 - b.y0 + 1;
        if (bh < 6) continue;
        // 列投影取左右边界
        let x0 = w, x1 = 0;
        for (let x = 0; x < w; x++) {
          let hit = false;
          for (let y = b.y0; y <= b.y1; y++) if (bin[y * w + x]) { hit = true; break; }
          if (hit) { if (x < x0) x0 = x; if (x > x1) x1 = x; }
        }
        if (x1 - x0 < 8) continue;
        const r = await this.recLine(gray, w, h, { x0: x0 - 2, y0: b.y0 - 1, x1: x1 + 3, y1: b.y1 + 2 });
        if (r.text && r.score >= 0.35) {
          items.push({ text: r.text, score: r.score, y: (b.y0 + b.y1) / 2, x: x0, hgt: bh });
        }
      }
      this.stat.detMs = performance.now() - t0;
      this.stat.recMs = 0;
      this.stat.n = items.length;
      return this.mergeRows(items);
    }

    /* ---------- 按纵向位置聚合成行，行内按 x 拼接（与桌面版一致） ---------- */
    mergeRows(items) {
      if (!items.length) return [];
      items.sort((a, b) => a.y - b.y);
      const groups = [];
      let cur = [];
      for (const it of items) {
        if (!cur.length) { cur = [it]; continue; }
        const ref = cur[0];
        const tol = Math.max(ref.hgt, it.hgt) * 0.6;
        if (Math.abs(it.y - ref.y) <= tol) cur.push(it);
        else { groups.push(cur); cur = [it]; }
      }
      if (cur.length) groups.push(cur);
      const out = [];
      for (const g of groups) {
        g.sort((a, b) => a.x - b.x);
        const text = g.map(x => x.text).join(' ').replace(/\s+/g, ' ').trim();
        if (text) out.push(text);
      }
      return out;
    }
  }

  /* ---------------- 预处理（与桌面版 preprocess 一致） ---------------- */
  function preprocess(srcCanvas, region, scale, binarize) {
    const { x, y, w, h } = region;
    const sw = Math.max(1, Math.round(w * scale)), sh = Math.max(1, Math.round(h * scale));
    const cv = makeCanvas(sw, sh);
    const ctx = ctx2d(cv);
    ctx.imageSmoothingEnabled = scale > 1;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(srcCanvas, x, y, w, h, 0, 0, sw, sh);
    const img = ctx.getImageData(0, 0, sw, sh);
    const gray = toGray(img);
    // 暗底亮字 → 反色成亮底暗字
    const m = meanOf(gray);
    if (m < 118) {
      for (let i = 0; i < gray.length; i++) gray[i] = 255 - gray[i];
    }
    if (binarize) {
      const thr = otsu(gray);
      for (let i = 0; i < gray.length; i++) gray[i] = gray[i] > thr ? 255 : 0;
    }
    return { gray, w: sw, h: sh, mean: m };
  }

  global.PPOcr = PPOcr;
  global.OcrUtil = { preprocess, resizeImageData, toGray, otsu, makeCanvas, ctx2d };
})(window);
