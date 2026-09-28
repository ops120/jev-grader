'use strict';

/* 轨迹版本的真值在 spec（src/judge/specs/*.json 的 trace_version），经 /api/papers 下发。
   这里只是接口整体不可用时的兜底——用到它时界面会显式说明（见 state.traceVersionSource）。 */
const FALLBACK_TRACE_VERSION = 2;

const state = { exam: null, traces: {}, current: null, key: {}, busy: false,
                paper: null, papers: [], idPrefix: 'Q', onlyWrong: false,
                traceVersion: FALLBACK_TRACE_VERSION, traceVersionSource: 'fallback',
                letters: null, mathProbe: false, papersError: null };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/** GET 一个接口：非 2xx 时把服务端的 detail 原样抛出，避免"加载失败"背后藏着 404/503。 */
async function getJSON(url) {
  const r = await fetch(url);
  let data = null;
  try { data = await r.json(); } catch { /* 空响应体 */ }
  if (!r.ok) {
    const detail = data && (data.detail || data.message);
    const msg = typeof detail === 'string' ? detail
      : detail ? JSON.stringify(detail) : `HTTP ${r.status}`;
    throw Object.assign(new Error(`HTTP ${r.status}：${msg}`), { httpStatus: r.status });
  }
  return data;
}

/** 试卷标题（顶栏 / document.title）：只取题库自带字段，没有就用中性名。 */
function bankName(paper) {
  const m = (paper && paper.meta) || {};
  return m.title || m.subject
    || (state.papers.find((p) => p.id === state.paper) || {}).title
    || (state.papers.find((p) => p.id === state.paper) || {}).subject
    || 'JEV 智能改卷系统';
}

/* ---- LaTeX 信号：题库声明优先，$...$ 探测兜底 ---- */
const paperHasLatex = () => !!(state.papers.find((p) => p.id === state.paper) || {}).has_latex;
const hasInlineMath = (s) => /\$[^$\n]+\$/.test(String(s ?? ''));
/** 这一项是否需要走 KaTeX：题库级 has_latex 是主信号；
    未声明的旧版接口再按卷内是否出现行内公式兜底（兜底结果会在界面上标出来）。 */
function latexOn() { return paperHasLatex() || state.mathProbe; }

/** 本卷真实出现过的选项字母集合（来自题库数据，不写死 A–G）。 */
function bankLetters() {
  if (!state.letters) {
    const set = new Set();
    for (const it of (state.exam && state.exam.items) || []) {
      for (const L of Object.keys(it.options || {})) set.add(L);
    }
    state.letters = [...set].sort();
  }
  return state.letters;
}
const letterClass = () => `[${(bankLetters().join('')) || 'A-Z'}]`;
/** 归一化答案串：去重、排序，返回 {v, unknown} —— unknown 是本卷不存在的字母。 */
function normAnswer(v) {
  const letters = bankLetters();
  const raw = [...new Set(String(v || '').toUpperCase().replace(/[^A-Z]/g, ''))];
  const known = raw.filter((L) => letters.includes(L)).sort().join('');
  const unknown = raw.filter((L) => !letters.includes(L)).sort().join('');
  return { v: known, unknown };
}

/* ---- 轨迹形状：新字段优先，缺失时按步骤名推断并标注 ---- */
/** 'single' | 'set'；legacy ＝ 本轨迹没带可用的 answer_shape，结论是推出来的（legacyReason 说明靠什么推）。 */
function shapeOf(tr) {
  if (tr.answer_shape === 'single' || tr.answer_shape === 'set') {
    return { shape: tr.answer_shape, legacy: false, legacyReason: '' };
  }
  // answer_shape 存在但不是合法值：不能假装它是单选——那会把集合答案显示成一个字母。
  // 退回按 agreement 推断，并标注「按推断」。
  const reason = tr.answer_shape == null ? '按 agreement' : `按异常值 answer_shape=${JSON.stringify(tr.answer_shape)} 回退`;
  return { shape: tr.agreement == null ? 'set' : 'single', legacy: true, legacyReason: reason };
}
const isSetTrace = (tr) => shapeOf(tr).shape === 'set';
/** 证据步：声明了 evidence:true 的优先，否则退回 key==='evidence'。 */
const evidenceStepOf = (tr) => (tr.steps || []).find((s) => s.evidence === true)
  || (tr.steps || []).find((s) => s.key === 'evidence') || null;
/** 揭晓步：决定最终答案的那一步。reveal:true 优先；
    集合型轨迹由代码阈值/组合步揭晓（历史轨迹里叫 decide / count_hint）。 */
function revealStepOf(tr) {
  const steps = tr.steps || [];
  const flagged = steps.findIndex((s) => s.reveal === true);
  if (flagged >= 0) return flagged;
  if (isSetTrace(tr)) {
    // 集合答案的揭晓点＝代码把概率折成集合的那一步（历史轨迹里叫 decide）。
    // 不能取"第一个非 support 的 group"——真实轨迹里 support(逐选项)在前、组合步在后，
    // 取错会让中栏骨架一直挂到播放结束才由兜底放出。找不到组合步时退回最后一步：
    // 最后一步一定在播放范围内，至少不会"永不揭晓"。
    const comp = steps.findIndex((s) => s.type === 'group' && s.key !== 'support');
    if (comp >= 0 && comp >= steps.length - 1) return comp;   // 组合步就在末尾：正是要找的揭晓点
    if (comp >= 0) return steps.length - 1;                   // 组合步后还有参考步 → 在最后一步揭晓
    return steps.length ? steps.length - 1 : -1;              // 没有可识别的组合步 → 兜底到最后一步
  }
  const i = steps.findIndex((s) => s.key === 'best');
  return i >= 0 ? i : steps.length - 1;
}
/** 「按步骤名推断」提示：只在联动结论真的靠猜时才给读者一句实话。 */
function linkageNote(tr) {
  const steps = tr.steps || [];
  const bits = [];
  if (shapeOf(tr).legacy) bits.push('答案形态（缺 answer_shape，按 agreement 推定）');
  if (!steps.some((s) => s.reveal === true)) {
    // 集合型轨迹：只有真的存在"代码组合步"可指认时才算推断成功；
    // 否则揭晓点是按位置兜底猜出来的，必须说出来。
    if (shapeOf(tr).shape !== 'set') {
      bits.push('揭晓步骤（缺 reveal，按步骤名 best/最后一步推定）');
    } else if (!steps.some((s) => s.type === 'group' && s.key !== 'support')) {
      bits.push('揭晓步骤（缺 reveal，且无可指认的代码组合步，按最后一步位置推定）');
    } else {
      bits.push('揭晓步骤（缺 reveal，按代码组合步 decide 推定）');
    }
  }
  if (!steps.some((s) => s.evidence === true) && !evidenceStepOf(tr)?.sentence) {
    bits.push('证据步骤（无 evidence 标记，也没有可用的 sentence，左栏不会高亮）');
  }
  if (!bits.length) return '';
  return `<div class="linkage-note">本题跨栏联动未完全由轨迹声明，已按步骤名推断：${esc(bits.join('；'))}。</div>`;
}

/** 渲染 $...$ 内的 LaTeX（数学题）；无 $ 的纯文本原样转义输出。 */
function tex(s) {
  const str = String(s ?? '');
  if (!str.includes('$') || typeof katex === 'undefined') return esc(str);
  return str.split(/(\$[^$]*\$)/g).map((seg) => {
    if (seg.length > 2 && seg.startsWith('$') && seg.endsWith('$')) {
      try {
        return katex.renderToString(seg.slice(1, -1), { throwOnError: false, display: false });
      } catch (e) { return esc(seg); }
    }
    return esc(seg);
  }).join('');
}

const pct = (v) => `${Math.round((v ?? 0) * 100)}%`;
/** 耗时展示：不足 1 分钟用秒，超过用 分+秒 */
function fmtMs(ms) {
  if (!ms) return '—';
  const sec = ms / 1000;
  if (sec < 60) return `${sec.toFixed(1)} 秒`;
  const m = Math.floor(sec / 60);
  return `${m} 分 ${(sec - m * 60).toFixed(0)} 秒`;
}
const item = (no) => ((state.exam && state.exam.items) || []).find((i) => i.no === no);
const passageOf = (it) => (it && it.passage_id
  ? ((state.exam && state.exam.passages) || []).find((p) => p.id === it.passage_id) : null);
/** 轨迹里的模型名（跳过 _run 之类的辅助键） */
const modelOf = () => {
  const t = Object.entries(state.traces).find(([k, v]) => !k.startsWith('_') && v?.model);
  return t ? t[1].model : 'jev';
};
const optText = (it, L) => (it && it.options ? it.options[L] : '') || '';

/** 轨迹查找：真实键是文件名前缀＋题号，但 idPrefix 取自第一道题——一旦某条轨迹来自
    别的前缀（或题库被重新编号），按前缀拼键会查成 undefined 并让切题直接崩。
    这里按轨迹自带的 item_id 建索引，查不到再退回旧拼法。 */
function buildTraceIndex() {
  state.traceIndex = {};
  for (const [k, tr] of Object.entries(state.traces || {})) {
    if (k.startsWith('_') || !tr) continue;
    if (tr.item_id != null) state.traceIndex[String(tr.item_id)] = tr;
    state.traceIndex[k] = tr;
  }
}
const traceKeyOf = (it) => (it && it.id != null ? String(it.id) : state.idPrefix + (it && it.no));
const traceOf = (no) => {
  const it = item(no);
  const idx = state.traceIndex || {};
  return (it && idx[String(it.id)]) || state.traces[state.idPrefix + no] || null;
};

/* ---------- 顶栏统计 ---------- */
function renderStats() {
  if (!state.exam) { $('stats').innerHTML = ''; return; }
  const items = state.exam.items;
  const answerable = items.filter((i) => i.answerable);
  const solved = answerable.filter((i) => traceOf(i.no));
  const by = { ok: 0, bad: 0, warn: 0, stale: 0, low: 0, na: 0 };
  items.forEach((i) => { by[statusOf(i).k] += 1; });
  const confs = solved.map((i) => traceOf(i.no).confidence).filter((c) => typeof c === 'number');
  const avg = confs.length ? confs.reduce((a, b) => a + b, 0) / confs.length : 0;

  // 全卷累计耗时（各题 wall_ms 之和，相当于逐题串行）
  const totalMs = solved.reduce((a, i) => a + (traceOf(i.no).wall_ms || 0), 0);
  // 正确率：仅统计「已录入答案且有判定结果」的题，未求解的不计为答错
  const cmp = items.filter((i) => keyOf(i) && traceOf(i.no));
  const hitN = cmp.filter((i) => normCompare(traceOf(i.no).answer) === normCompare(keyOf(i))).length;

  // 题库核验状态：verified 只能由外部核验流程置位。本项目不做提取，
  // 上传题库的「题面是否与原件一致」没有物证可查，界面必须如实亮出「未核验」。
  const bm = state.exam.meta || {};
  const vblockers = (bm.verified_blockers || []).filter((b) => !/核验/.test(b));
  const cells = [
    bm.verified === true
      ? { n: '已核验', l: '题库独立核验', cls: 'is-ok', t: '该题库带有独立核验记录' }
      : { n: '未核验', l: '题库独立核验', cls: 'is-action',
          t: vblockers.length ? `尚未通过独立核验：${vblockers.join('；')}` : '题面内容未经独立核验（上传题库的常态，不影响改卷）' },
    { n: answerable.length, l: '可作答题目' },
    { n: `${solved.length}/${answerable.length}`, l: '已求解', cls: solved.length === answerable.length ? 'is-ok' : '' },
    { n: by.warn, l: '依据薄弱', cls: by.warn ? 'is-bad' : 'is-ok' },
    { n: by.bad, l: '两路判断分歧', cls: by.bad ? 'is-bad' : 'is-ok' },
    { n: by.low, l: '低置信度(<50%)', cls: by.low ? 'is-bad' : 'is-ok' },
    { n: pct(avg), l: '平均置信度' },
    { n: totalMs ? fmtMs(totalMs) : '—', l: '全卷累计耗时', cls: 'is-time' },
    cmp.length
      ? { n: pct(hitN / cmp.length), l: `正确率 ${hitN}/${cmp.length}`,
          cls: hitN === cmp.length ? 'is-ok' : 'is-bad' }
      : { n: '—', l: '正确率（无标准答案）' },
  ];
  $('stats').innerHTML = cells.map((c) => `
    <div class="stat ${c.cls || ''}" ${c.t ? `title="${esc(c.t)}"` : ''}><b>${c.n}</b><span>${c.l}</span></div>`).join('');
  const vstat = $('stats').querySelector('.is-action');
  if (vstat) vstat.addEventListener('click', () => { if (state.exam) openSummary(); });
}

const KIND_CN = {
  mcq: '四选一阅读理解', seven: '七选五', cloze: '完形填空',
  gapfill: '语法填空', listening: '听力', writing: '写作',
  math_single: '单选（四选一）', math_multi: '多选',
  math_gapfill: '填空题', math_solution: '解答题',
};
const NA_REASON = {
  listening: '试卷无听力原文',
  gapfill: '需生成词形（v2）',
  writing: '需生成文本（v2）',
  math_gapfill: '需生成数值/表达式（v2）',
  math_solution: '需生成完整解题过程（v2）',
};
const COLLAPSE_KEY = 'jev_answer_collapsed';
/** 轨迹版本是否来自接口；false 时界面会标注用的是前端兜底常量。 */
const traceVersionIsFallback = () => state.traceVersionSource !== 'api';

/* 一道题的汇总状态：ok 一致 / bad 分歧 / warn 依据薄弱 / stale 轨迹过旧 / na 未求解或不可作答 */
function statusOf(it) {
  if (!it.answerable) return { k: 'na', label: '不可作答' };
  const t = traceOf(it.no);
  if (!t) return { k: 'na', label: '未求解' };
  // 旧代码生成的轨迹缺少 weak/support_max，不能当作"一致"来读
  if ((t.trace_version || 0) < state.traceVersion) return { k: 'stale', label: '轨迹过旧' };
  // 无任何选项获得材料支持时，一致性结论本身没有意义，依据薄弱优先
  if (t.weak) return { k: 'warn', label: '依据薄弱' };
  if (t.agreement === false) return { k: 'bad', label: '存在分歧' };
  if (typeof t.confidence === 'number' && t.confidence < 0.5) return { k: 'low', label: '低置信度' };
  return { k: 'ok', label: '一致' };
}

/* ---------- 答案栏折叠 ---------- */
function setCollapsed(v) {
  document.getElementById('workbench').classList.toggle('answer-collapsed', v);
  document.getElementById('answer-section').classList.toggle('is-collapsed', v);
  localStorage.setItem(COLLAPSE_KEY, v ? '1' : '0');
  updateStrip();
}

function updateStrip() {
  const it = item(state.current);
  const el = $('strip-answer');
  if (!it) return;
  const t = traceOf(it.no);
  const st = statusOf(it);
  el.textContent = t && t.answer ? t.answer : '—';
  el.className = 'strip-answer' + (
    !t || !t.answer ? ' is-none' : st.k === 'bad' ? ' is-bad' : st.k === 'warn' ? ' is-weak' : '');
}

/* ---------- 左栏导航 ---------- */
function renderRail() {
  if (!state.exam) { $('rail-body').innerHTML = ''; return; }
  const groups = state.exam.sections.map((s) => ({
    sec: s,
    items: state.exam.items.filter((i) => i.section_id === s.id),
  })).filter((g) => g.items.length);

  $('rail-body').innerHTML = groups.map(({ sec, items }) => `
    <div class="sec">
      <div class="sec-name">${esc(sec.name)}</div>
      <div class="sec-meta">第 ${sec.no_from}–${sec.no_to} 题 · ${sec.per
        ? `每题 ${sec.per} 分`
        : (sec.full ? `共 ${sec.full} 分` : `共 ${sec.count || (sec.no_to - sec.no_from + 1)} 题`)}</div>
      <div class="nums">
        ${items.map((i) => {
          const st = statusOf(i);
          const t = traceOf(i.no);
          const cls = [
            i.no === state.current ? 'active' : '',
            t && st.k === 'ok' ? 'solved' : '',
            st.k === 'bad' ? 'disagree' : '',
            st.k === 'stale' ? 'stale' : '',
            st.k === 'low' ? 'low' : '',
            st.k === 'na' ? 'na' : '',
          ].filter(Boolean).join(' ');
          const title = !i.answerable ? (i.notice || '不可作答')
            : (t ? `JEV: ${t.answer ?? '—'} · ${st.label}` : '未求解');
          const warn = ['warn', 'bad', 'stale', 'low'].includes(st.k)
            ? '<span class="dot"></span>' : '';
          return `<button class="num ${cls}" data-no="${i.no}" title="${esc(title)}">${i.no}${warn}</button>`;
        }).join('')}
      </div>
    </div>`).join('');

  $('rail-body').querySelectorAll('.num').forEach((b) =>
    b.addEventListener('click', () => select(Number(b.dataset.no))));
}

function select(no) {
  state.current = no;
  renderRail(); renderQuestion(); renderAnswer(); renderTrace();
}

/* ---------- 左栏：题面 ---------- */
const fmtBlank = (t) => esc(t).replace(/\[(\d{1,2})\]/g, '<span class="blank">$1</span>');

function renderQuestion() {
  const it = item(state.current);
  if (!it) return;
  const p = passageOf(it);
  const kindCn = KIND_CN[it.kind] || it.kind;
  $('q-meta').textContent = kindCn && kindCn !== it.section_name
    ? `${it.section_name} · ${kindCn}` : it.section_name;
  const parts = [];

  if (it.notice) {
    const icon = it.answerable ? '!' : '×';
    parts.push(`<div class="notice"><span class="ico">${icon}</span><div>${esc(it.notice)}</div></div>`);
  }

  if (p) {
    const blankInfo = it.kind === 'cloze' || it.kind === 'seven' || it.kind === 'gapfill'
      ? `<span class="chip chip-ghost">本材料含第 ${it.no} 空</span>` : '';
    parts.push(`<div class="block">
      <div class="block-title">阅读材料 ${blankInfo}</div>
      <div class="card card-pad">
        ${p.title ? `<h3 class="passage-title">${esc(p.title)}</h3>` : ''}
        <div class="passage" id="passage">
          ${p.sentences.map((s) => `<span class="sent" data-sno="${s.no}">
            <span class="sno">${s.no}</span>${fmtBlank(s.text)}</span>`).join('')}
        </div>
      </div></div>`);
  } else if (it.kind === 'listening') {
    parts.push(`<div class="block"><div class="block-title">听力材料</div>
      <div class="card card-pad"><p style="margin:0;font-size:13px;color:var(--ink-2)">
      本试卷不含听力原文，${esc(it.material || '该段材料')}的录音内容不在 PDF 内。</p></div></div>`);
  }

  // 题目本体：是否走 KaTeX 由题库声明的 has_latex 决定（不再按题型名前缀猜），
  // tex() 只渲染 $...$ 片段，因此对无公式的纯文本与 esc() 结果一致。
  const latex = latexOn();
  // 数学排版样式按"本题是否真的含公式/表格结构"选择，不再按题型名前缀猜科目
  const richMath = latex && (hasInlineMath(it.stem)
    || Object.values(it.options || {}).some(hasInlineMath)
    || (it.subs || []).some(hasInlineMath));
  const isMath = richMath || !!(it.table || it.crit_table);
  const render = (t) => (latex ? tex(t) : esc(t));
  const stemHtml = render(it.stem);
  const opts = Object.entries(it.options || {});
  const tableHtml = [it.table, it.crit_table].filter(Boolean).map((t) => `
    <table class="data-table">
      <thead><tr>${(t.header || []).map((h) => `<th>${render(h)}</th>`).join('')}</tr></thead>
      <tbody>${(t.rows || []).map((r) => `<tr>${r.map((c) => `<td>${render(c)}</td>`).join('')}</tr>`).join('')}</tbody>
    </table>`).join('');
  const subsHtml = (it.subs || []).length
    ? `<div style="margin-top:8px;font-size:14px;line-height:1.95">${
        it.subs.map((s) => `<div>${render(s)}</div>`).join('')}</div>` : '';
  const appendHtml = (it.appendices || []).length
    ? `<div style="margin-top:8px;font-size:13.5px;color:var(--ink-2)">${
        it.appendices.map((s) => `<div>${render(s)}</div>`).join('')}</div>` : '';
  const figNote = (it.figures || []).length
    ? `<div class="notice" style="margin-top:10px"><span class="ico">!</span><div>
        本题含图形：${esc(it.figures.join('；'))}。图形未作为文本提供，模型看不到图。</div></div>` : '';
  const origHtml = (it.images || []).length
    ? `<details class="math-orig"><summary>查看试卷原始排版（公式保真）</summary>
        ${it.images.map((src) => `<img src="/${esc(src)}" alt="第 ${it.no} 题原图" loading="lazy">`).join('')}
       </details>` : '';

  parts.push(`<div class="block">
    <div class="block-title">第 ${it.no} 题 ${it.score != null ? `<span class="chip chip-ghost">${esc(it.score)} 分</span>` : ''}
      ${it.seq_note ? `<span class="chip chip-ghost">${esc(it.seq_note)}</span>` : ''}</div>
    <div class="${isMath ? 'math-stem' : 'stem'}">${stemHtml}</div>
    ${subsHtml}
    ${tableHtml}
    ${opts.length ? `<ul class="opts">${opts.map(([L, T]) => `
      <li class="opt" data-opt="${L}"><span class="ltr">${L}</span>
        <span class="${isMath ? 'math-opt' : ''}">${render(T)}</span></li>`).join('')}</ul>`
      : ''}
    ${appendHtml}
    ${figNote}
    ${origHtml}
    ${!paperHasLatex() && state.mathProbe ? `<div class="notice" style="margin-top:10px">
      <span class="ico">!</span><div>题库清单未声明 <code>has_latex</code>，
      公式渲染是按卷内出现行内 <code>$…$</code> 探测出来的（兜底判断，非题库声明）。</div></div>` : ''}
  </div>`);

  $('pane-question').innerHTML = parts.join('');

  const tr = traceOf(it.no);
  if (tr) markOptions(it, tr);

  $('pane-question').querySelectorAll('.sent').forEach((el) =>
    el.addEventListener('click', () => el.classList.toggle('hl')));
  // 回放未走到「证据定位」前不预先高亮，让证据句由播放过程带出
  if (tr && !player.skeletonFor(it.no)) {
    highlightEvidence(evidenceStepOf(tr)?.sentence, false);
  } else {
    highlightEvidence(null, false);
  }
}

function markOptions(it, tr) {
  const set = isSetTrace(tr);
  // 揭晓步的 choice（单选）或答案串里的字母（多选集合）都算 JEV 选中的项
  const revealIdx = revealStepOf(tr);
  const chosen = new Set(set
    ? String(tr.answer ?? '').split('')
    : [revealIdx >= 0 ? (tr.steps || [])[revealIdx].choice : tr.answer].filter(Boolean));
  const dis = ((tr.steps || []).find((s) => s.key === 'distractor') || {}).choice;
  const k = keyOf(it);
  const keys = set ? new Set(String(k ?? '').split('')) : new Set(k ? [k] : []);
  $('pane-question').querySelectorAll('.opt').forEach((el) => {
    const L = el.dataset.opt;
    if (chosen.has(L)) el.classList.add('is-answer');
    if (dis && L === dis && !chosen.has(L)) el.classList.add('is-distractor');
    if (keys.has(L) && !chosen.has(L)) el.classList.add('is-wrong');
  });
}

function highlightEvidence(sno, scroll = true) {
  document.querySelectorAll('#passage .sent').forEach((el) => {
    el.classList.toggle('hl-primary', sno != null && el.dataset.sno === sno);
  });
  if (sno && scroll) {
    const el = document.querySelector(`#passage .sent[data-sno="${sno}"]`);
    if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }
}

/* ---------- 中栏：答案 ---------- */
function renderAnswer() {
  const it = item(state.current);
  if (!it) return;
  updateStrip();
  const tr = traceOf(it.no);
  $('a-meta').textContent = tr ? `模型 ${tr.model || '—'}` : '未求解';
  const stale = tr ? (tr.trace_version || 0) < state.traceVersion : false;

  // 回放中且答案尚未揭晓：显示「判定中」骨架，等揭晓步出现再放出答案
  if (tr && player.skeletonFor(it.no)) {
    const total = (tr.steps || []).length;
    const cur = Math.max(0, player.phase + 1);
    const rv = revealStepOf(tr);
    const hint = rv >= 0
      ? `答案将在右栏走到「${esc(tr.steps[rv].label || tr.steps[rv].key)}」（第 ${rv + 1} 步）时揭晓。`
      : '本轨迹没有任何一步声明或可推断为「揭晓步」（缺 <code>reveal</code> 字段，且无同名步骤），'
        + '答案只会在播放结束时由代码组合结果给出。';
    $('pane-answer').innerHTML = `
      <div class="skeleton">
        <div class="sk-note">JEV 判定中
          <span class="sk-dots"><i></i><i></i><i></i></span>
          <span style="margin-left:auto">${cur} / ${total} 步</span></div>
        <div class="verdict">
          <span class="sk-letter"></span>
          <div style="flex:1;min-width:0">
            <div class="sk-line" style="width:38%"></div>
            <div class="sk-line" style="width:76%;margin-bottom:0"></div>
          </div>
        </div>
        <div class="hr"></div>
        <div class="sk-line" style="width:100%"></div>
        <div class="sk-line" style="width:88%"></div>
        <div class="sk-line" style="width:64%"></div>
      </div>
      <div class="empty" style="padding:16px 4px 4px">
        <p style="margin:0">${hint}</p>
      </div>`;
    return;
  }

  if (!it.answerable) {
    $('pane-answer').innerHTML = `<div class="empty"><div class="big">—</div>
      <p><span class="tag-na">暂不可自动作答</span></p>
      <p>${esc(it.notice || '')}</p></div>`;
    return;
  }
  if (!tr) {
    $('pane-answer').innerHTML = `<div class="empty"><div class="big">◌</div>
      <p>本题尚未求解</p>
      <button class="btn btn-primary" id="empty-solve">求解第 ${it.no} 题</button></div>`;
    $('empty-solve').addEventListener('click', () => solve([it.no]));
    return;
  }

  const conf = tr.confidence ?? 0;
  const confCls = conf >= 0.85 ? 'var(--ok)' : conf >= 0.6 ? 'var(--warn)' : 'var(--bad)';
  const key = state.key[it.no];
  const ok = !!key && normCompare(key) === normCompare(tr.answer ?? '');
  const shape = shapeOf(tr);
  const isSet = shape.shape === 'set';            // 集合答案（多选）
  const rv = revealStepOf(tr);
  const revealStep = rv >= 0 ? (tr.steps || [])[rv] : null;
  const supportStep = (tr.steps || []).find((s) => s.key === 'support');
  const compStep = (tr.steps || []).find((s) => s.type === 'group' && s.key !== 'support');
  const trapStep = (tr.steps || []).find((s) => s.key === 'trap');
  // 多选题没有单选揭晓步，展示逐选项判定分布
  const distStep = revealStep || supportStep;
  const latex = latexOn();
  const render = (t) => (latex ? tex(t) : esc(t));

  $('pane-answer').innerHTML = `
    ${stale ? `<div class="notice"><span class="ico">!</span><div>
      这份轨迹由<b>旧版本代码</b>生成（trace_version=${esc(tr.trace_version ?? '—')} &lt;
      当前 ${state.traceVersion}${traceVersionIsFallback() ? '，前端兜底值' : ''}），缺少「依据强弱」等判定字段，
      下面的答案与一致性结论不可采信。请点「求解本题」重算。</div></div>` : ''}
    <div class="block">
      <div class="block-title">JEV 判定答案</div>
      <div class="verdict">
        <div class="letter">${esc(tr.answer ?? '—')}</div>
        <div class="vtext">
          <div class="vlabel">${isSet ? '入选选项集合' : '最高概率选项'}</div>
          <div class="vopt">${render(isSet
          ? String(tr.answer ?? '').split('').filter((L) => optText(it, L))
              .map((L) => `${L}. ${optText(it, L)}`).join('　') || '—'
          : (optText(it, tr.answer) || '—'))}</div>
        </div>
        <div class="ring" style="--p:${Math.round(conf * 100)};--c:${confCls}"
             title="置信度 ${pct(conf)}"><i>${pct(conf)}</i></div>
      </div>
      ${linkageNote(tr)}
    </div>

    <div class="block">
      <div class="block-title">${revealStep ? `${esc(revealStep.label || '最终判定')}概率分布` : '逐选项判定概率（决定入选）'}</div>
      ${distStep
        ? probBars(distStep.probabilities
            || Object.fromEntries((distStep.items || []).map((x) => [x.key, x.value ?? 0])),
          it, tr.answer)
        : '<div class="conf">无数据</div>'}
      ${!revealStep && it.options ? `<div class="conf" style="margin-top:8px">
        多选题的答案为选项集合：概率 ≥ 0.5 的选项入选，由代码判定，不经过选择判定。</div>` : ''}
    </div>

    <div class="block">
      <div class="block-title">一致性检查</div>
      <div class="card card-pad">
        <div class="chip-row">
          ${isSet
            ? `<span class="chip chip-ghost">集合判定（无需两路交叉）${
                shape.legacy ? `<em class="chip-inferred">${esc(shape.legacyReason)}推断</em>` : ''}</span>`
            : stale
            ? `<span class="chip chip-warn">轨迹过旧</span>`
            : tr.weak
            ? `<span class="chip chip-warn">依据薄弱</span>`
            : tr.agreement ? `<span class="chip chip-ok">两路判断一致</span>`
                           : `<span class="chip chip-bad">存在分歧</span>`}
          ${isSet ? '' : `<span class="conf">直接作答 ${esc(tr.answer ?? '—')} · 支持度最高 ${esc(tr.top_support ?? '—')}
            ${tr.support_max != null ? `（${pct(tr.support_max)}）` : ''}</span>`}
        </div>
        <div style="font-size:12.5px;color:var(--ink-2);line-height:1.6">
          ${isSet
            ? '本题为多选题，答案是选项集合，不存在"两路结论是否相同"的比较。'
              + (compStep
                  ? '右栏「阈值判定」展示了代码如何按概率阈值把判断折成答案集合。'
                  : '但本轨迹缺少代码组合步（阈值判定），右栏没有可展示的折集过程——答案只是轨迹记录值，请谨慎采信。')
            : stale
            ? '该轨迹缺少依据强弱判定字段，无法给出一致性结论，请重新求解本题。'
            : tr.weak
            ? '所有选项的逐选项支持度都很低，说明没有一个选项真正得到材料支持，通常意味着该题依赖的材料不在文本内（如图表、图形、听力音频）。此处的一致不代表可信，请勿采信该答案。'
            : tr.agreement
              ? '「最终判定」与「逐选项支持度」指向同一选项，两路判断自洽。注意：这只说明模型内部自洽，不等于答案正确。'
              : '两路独立判断结论不同，建议人工复核该题。'}
        </div>
      </div>
    </div>

    ${trapStep ? `<div class="block">
      <div class="block-title">干扰项</div>
      <div class="card card-pad" style="font-size:13px">
        <b style="font-family:var(--mono)">${esc(trapStep.choice ?? '—')}</b>
        · ${render(trapStep.desc ?? '')}
      </div>
    </div>` : ''}

    <div class="block">
      <div class="block-title">标准答案对照（可选）</div>
      <div class="card card-pad">
        <div class="key-in">
          <input id="key-input" placeholder="录入答案，如 B 或 ABC"
                 value="${esc(key || '')}" style="text-transform:uppercase">
          <button class="btn" id="key-save">保存</button>
        </div>
        ${key ? `<div class="cmp">
          <span class="big ${ok ? 'ok' : 'bad'}">${ok ? '✓ 一致' : '✗ 不一致'}</span>
          <span class="conf">标准答案 ${esc(key)} · JEV ${esc(tr.answer ?? '—')}</span>
        </div>` : `<div class="cmp conf" style="color:var(--ink-3)">
          试卷 PDF 内不含答案表，可手动录入用于比对。</div>`}
        <div id="key-hint" class="conf" style="margin-top:6px"></div>
        <details class="payload" style="margin-top:10px">
          <summary>批量粘贴答案表</summary>
          <div style="padding:10px 11px">
            <textarea id="key-bulk" rows="3" placeholder="如：21 B 22 C 23 B，或 21:B 22:C"
              style="width:100%;padding:7px 9px;border:1px solid var(--line-2);border-radius:7px;font-family:var(--mono);font-size:12px"></textarea>
            <div style="margin-top:7px;display:flex;gap:7px">
              <button class="btn btn-primary" id="key-parse">解析并保存</button>
              <button class="btn" id="key-clear">清空全部</button>
            </div>
          </div>
        </details>
      </div>
    </div>

    <div class="block">
      <div class="block-title">运行信息</div>
      <div class="card card-pad">
        <dl class="kv">
          <dt>模型</dt><dd>${esc(tr.model || '—')}</dd>
          <dt>单次判断数</dt><dd>${tr.n_questions ?? '—'}</dd>
          <dt>推理耗时</dt><dd>${tr.latency_ms ?? '—'} ms</dd>
          <dt>端到端耗时</dt><dd>${tr.wall_ms ?? '—'} ms</dd>
          <dt>Token 用量</dt><dd>${tr.usage ? `${tr.usage.input_tokens} / ${tr.usage.output_tokens}` : '—'}</dd>
          <dt>答案形态</dt><dd>${esc(shape.shape)}${shape.legacy
            ? `<span class="chip-inferred">${esc(shape.legacyReason)}推断</span>` : '<span class="chip-ok-tag">轨迹已声明</span>'}</dd>
          <dt>揭晓步声明</dt><dd>${rv >= 0 && (tr.steps || [])[rv] && (tr.steps)[rv].reveal === true
            ? `<span class="chip-ok-tag">由第 ${rv + 1} 步「${esc((tr.steps[rv].label || tr.steps[rv].key))}」的 reveal 声明</span>`
            : `<span class="chip-inferred">无 reveal 字段，${rv >= 0
                ? `按位置推定在第 ${rv + 1} 步「${esc((tr.steps[rv].label || tr.steps[rv].key))}」揭晓`
                : '找不到揭晓点（空轨迹），只在播放结束时给出'}</span>`}</dd>
          <dt>轨迹版本门槛</dt><dd>${state.traceVersion}${traceVersionIsFallback()
            ? '<span class="chip-inferred">前端兜底常量，接口未提供 trace_version</span>'
            : '<span class="chip-ok-tag">来自 /api/papers</span>'}</dd>
        </dl>
      </div>
    </div>`;

  const keyHint = (msg) => { const el = $('key-hint'); if (el) el.innerHTML = msg || ''; };

  $('key-save').addEventListener('click', () => {
    const raw = ($('key-input').value || '').trim().toUpperCase();
    const { v, unknown } = normAnswer(raw);
    if (raw && !v) {
      return toast(`第 ${it.no} 题：${esc(unknown)} 不在本卷选项中（本卷选项：${bankLetters().join('、') || '无'}）`);
    }
    if (unknown) toast(`已忽略本卷不存在的选项字母：${unknown}`);
    if (v) state.key[it.no] = v; else delete state.key[it.no];
    saveKey();
    renderAnswer(); renderQuestion(); renderRail(); renderStats();
    toast(v ? `已保存第 ${it.no} 题标准答案${unknown ? `（忽略 ${unknown}）` : ''}` : '已清除');
  });
  $('key-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('key-save').click(); });
  $('key-parse').addEventListener('click', () => {
    const res = parseKey($('key-bulk').value);
    applyKeyParse(res, keyHint);
  });
  $('key-clear').addEventListener('click', () => {
    state.key = {}; localStorage.removeItem(keyStore());
    renderAnswer(); renderQuestion(); renderRail(); renderStats(); toast('已清空标准答案');
  });
}

/** 批量解析结果统一落库 + 把被丢弃的部分显式说出来（不静默丢数据）。 */
function applyKeyParse(res, hintEl) {
  const n = Object.keys(res.map).length;
  if (!n) { toast('未识别到「题号 + 字母」对'); return { n: 0, html: '' }; }
  Object.assign(state.key, res.map);
  saveKey();
  const maxNo = ((state.exam && state.exam.items) || []).reduce((a, i) => Math.max(a, i.no), 0);
  const warn = [];
  if (res.outOfRange.length) {
    warn.push(`题号超出本卷范围（本卷题号至 ${maxNo || '?'}）：${
      res.outOfRange.slice(0, 8).map((x) => `${x.no}→${x.letters}`).join('、')}${
      res.outOfRange.length > 8 ? ` 等 ${res.outOfRange.length} 条` : ''}`);
  }
  if (res.unknown.length) {
    warn.push(`本卷不存在这些选项字母（可选：${bankLetters().join('、') || '无'}）：${
      res.unknown.slice(0, 8).map((x) => `${x.no}→${x.letters.join('')}`).join('、')}${
      res.unknown.length > 8 ? ` 等 ${res.unknown.length} 条` : ''}`);
  }
  if (res.stray.length) {
    warn.push(`未与题号配对、因此未录入的字母：${res.stray.join(' ')}`);
  }
  const html = warn.length
    ? `<span class="key-warn">已录入 ${n} 题，但以下内容未生效：<br>${warn.map(esc).join('<br>')}</span>`
    : `已录入 ${n} 题`;
  if (hintEl) hintEl.innerHTML = html;
  renderAnswer(); renderQuestion(); renderRail(); renderStats();
  toastKey(n, warn);
  return { n, html, warn };
}

/** 提示条：需要换行的告警用 innerHTML 走 <br>，内容已在调用处 esc 过。 */
function toastKey(n, warn) {
  const t = $('toast');
  t.innerHTML = `已录入 ${n} 题标准答案${warn.length
    ? `<br><span class="toast-warn">${warn.map(esc).join('<br>')}</span>` : ''}`;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), warn.length ? 6500 : 2600);
}

/** 比对用归一化：字母串按集合比较（'AC' == 'CA'），其余写法原样比较。 */
const normCompare = (v) => {
  const s = String(v ?? '').trim().toUpperCase();
  return /^[A-Z]+$/.test(s) ? [...new Set(s)].sort().join('') : s;
};

function parseKey(text) {
  const str = String(text ?? '');
  const letters = bankLetters();
  if (!letters.length) return { map: {}, outOfRange: [], unknown: [], stray: [] };
  const cls = letterClass();
  const consumed = [];
  const out = {};
  const outOfRange = [];
  // 只认「独立数字 + 可选分隔 + 连续选项字母」的成对写法（'21 B' / '21:B' / '21、B' / '9 ABC'）。
  // 字母集来自本卷真实选项，题号不设位数上限——行测卷有 100+ 题，写死 99 会静默丢掉后半张答案表。
  // 数字必须两侧不挨着字母/数字：避免把年份等长数字串误配成题号。
  const re = new RegExp(`(?<![0-9A-Za-z])(\\d+)[.:：、-]?\\s*(${cls}+)(?![A-Za-z])`, 'g');
  let m;
  while ((m = re.exec(str))) {
    const no = Number(m[1]);
    // 捕获串里可能混进非字母（如全角空格）：先剥掉，否则会把"空白"报成未知选项
    const clean = String(m[2]).replace(/[^A-Z]/g, '');
    if (!clean) continue;
    consumed.push([m.index, m.index + m[0].length]);
    if (no > 999) { outOfRange.push({ no: m[1], letters: clean }); continue; }   // 年份/长数字串：说清楚没录入
    if (no >= 1) out[no] = [...new Set(clean)].sort().join('');
  }
  const inPair = (i) => consumed.some(([a, b]) => i >= a && i < b);
  // 落单的字母也要说清楚：以前它会被整条丢弃，用户以为录进去了
  const stray = new Set();
  for (const mm of str.matchAll(new RegExp(`(?<![A-Za-z])(${cls})(?![A-Za-z])`, 'g'))) {
    if (!inPair(mm.index)) stray.add(mm[0]);
  }
  const maxNo = ((state.exam && state.exam.items) || []).reduce((a, i) => Math.max(a, i.no), 0);
  const map = {}, unknown = [];
  for (const [k, v] of Object.entries(out)) {
    const no = Number(k);
    const miss = [...v].filter((L) => !letters.includes(L));
    const kept = v.replace(new RegExp(`[${miss.join('')}]`, 'g'), '');
    if (maxNo && no > maxNo) { outOfRange.push({ no, letters: v }); continue; }
    if (miss.length) unknown.push({ no, letters: miss });
    if (kept) map[k] = kept;
  }
  return { map, outOfRange, unknown, stray: [...stray].sort() };
}

/* 概率条：同时写出最终宽度与 data-w，回放时可从 0 生长到 data-w */
function probBars(probs, it, top) {
  const entries = Object.entries(probs || {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return '<div class="conf">无数据</div>';
  // top 可能是单字母（单选）或字母集合（多选，如 "ABC"），两种情况都标出
  const tops = new Set(String(top ?? '').split(''));
  const wide = Math.max(...entries.map(([L]) => L.length)) > 2;
  return `<div class="bars">${entries.map(([L, v]) => {
    const w = Math.max(v * 100, 1.5).toFixed(1);
    return `<div class="bar-row ${wide ? 'wide' : ''} ${tops.has(L) ? 'is-top' : ''} ${v < 0.05 ? 'is-low' : ''}">
      <span class="bl">${esc(L)}</span>
      <span class="track"><span class="fill" data-w="${w}" style="width:${w}%"></span></span>
      <span class="bv">${pct(v)}</span>
    </div>`;
  }).join('')}</div>`;
}

/* 折叠态标题行右侧的结论摘要：让收起的步骤仍然可读 */
function summaryOf(s) {
  if (s.summary) return s.summary;
  if (s.type === 'group') {
    const items = Array.isArray(s.items) ? s.items : [];
    if (!items.length) return NO_DATA_LABEL;
    const vals = items.map((x) => [x.value ?? 0, x.key]).sort((a, b) => b[0] - a[0]);
    return vals.length ? `最高 ${vals[0][1]} ${pct(vals[0][0])}` : '';
  }
  if (s.evidence === true || s.key === 'evidence') return s.sentence || '';
  if (s.key === 'strategy') {
    const t = (s.legend || {})[s.choice];
    return t ? `${s.choice} ${t.split('：')[0]}` : (s.choice || '');
  }
  return s.choice ? `${s.choice} ${s.reveal === true || s.key === 'best' ? pct(s.confidence) : ''}`.trim() : '';
}

/* ---------- 右栏：运算过程（可折叠 + 回放） ---------- */
const NO_DATA_LABEL = '无数据';
/** 读取 group 步的条目；缺字段/空数组时返回 null，由调用处渲染成显式的「无数据」。 */
const groupItems = (s) => (Array.isArray(s.items) && s.items.length ? s.items : null);

function renderTrace() {
  player.stop();
  const it = item(state.current);
  if (!it) return;
  const tr = traceOf(it.no);
  $('t-meta').textContent = tr ? `${(tr.steps || []).length} 个判断步骤` : '等待求解';
  $('tb-metrics').hidden = true;

  if (!tr) {
    $('pane-trace').innerHTML = `<div class="empty"><div class="big">◌</div>
      <p>JEV 会在这道题上提出若干相互独立的原子判断，<br>再用代码组合出答案。求解后此处可回放完整判定过程。</p></div>`;
    player.sync();
    return;
  }

  const latex = latexOn();
  const render = (t) => (latex ? tex(t) : esc(t));
  const evStep = evidenceStepOf(tr);
  const rvIdx = revealStepOf(tr);
  const stepsHtml = (tr.steps || []).map((s, i) => {
    const n = `<span class="n">${String(i + 1).padStart(2, '0')}</span>`;
    const head = (ty) => `<div class="step-head">
      ${n}<span class="t">${esc(s.label ?? s.key ?? `步骤 ${i + 1}`)}</span>
      ${ty ? `<span class="ty">${ty}</span>` : ''}
      <span class="sum">${esc(summaryOf(s))}</span>
      <span class="chev">▼</span></div>`;

    if (s.type === 'group') {
      const items = groupItems(s);
      if (!items) {
        return `<div class="step" data-idx="${i}">
          ${head('Noul × 0')}
          <div class="step-wrap"><div>
            <div class="step-body">
              <div class="no-data">${NO_DATA_LABEL}：该步骤声明为批量判断（type: group），
                但轨迹里没有 <code>items</code> 数据。这里不是"没有选项支持度"，
                而是这份轨迹缺字段——请重新求解本题。</div>
            </div>
          </div></div></div>`;
      }
      const max = Math.max(...items.map((x) => x.value ?? 0));
      const rows = items.map((x, k) => {
        const v = x.value ?? 0;
        const w = Math.max(v * 100, 1.5).toFixed(1);
        return `<div class="bar-row ${v === max ? 'is-top' : ''} ${v < 0.05 ? 'is-low' : ''}"
                     style="--d:${k * 90}ms">
          <span class="bl">${esc(x.key)}</span>
          <span class="track"><span class="fill" data-w="${w}" style="width:${w}%"></span></span>
          <span class="bv">${pct(v)}</span>
          <span class="note">${render(x.text || '')}</span>
        </div>`;
      }).join('');
      return `<div class="step" data-idx="${i}">
        ${head(`Noul × ${items.length}`)}
        <div class="step-wrap"><div>
          <div class="step-body">
            <div class="bars rise">${rows}</div>
            <div class="conf reveal" style="margin-top:7px">每个选项一次独立判断，形成比单一选择更细腻的排除依据。</div>
          </div>
        </div></div></div>`;
    }

    const isEv = !!evStep && evStep === s;
    return `<div class="step" data-idx="${i}">
      ${head('Choice')}
      <div class="step-wrap"><div>
        <div class="step-body ${isEv ? 'evidence' : ''}" ${isEv ? 'data-sno="' + esc(s.sentence) + '"' : ''}>
          <div class="chip-row reveal">
            <span class="pick">${esc(s.choice ?? '—')}</span>
            <span class="conf">置信度 ${pct(s.confidence)}</span>
            ${isEv ? '<span class="conf" style="margin-left:auto">点击定位到左栏</span>' : ''}
            ${i === rvIdx ? '<span class="chip chip-ghost" style="margin-left:auto">揭晓答案</span>' : ''}
          </div>
          ${s.desc ? `<div class="reveal ${isEv ? 'quote' : ''}" style="font-size:13px;line-height:1.6">${render(s.desc)}</div>` : ''}
          ${!isEv ? `<div class="rise">${probBars(s.probabilities, it, s.choice)}</div>` : ''}
        </div>
      </div></div></div>`;
  }).join('');

  $('pane-trace').innerHTML = `
    <div class="steps">${stepsHtml}</div>
    <div class="block" style="margin-top:14px">
      <div class="block-title">组合规则（由代码决定，非模型输出）</div>
      <div class="rule">${esc(tr.composition_rule || '')}</div>
    </div>
    <details class="payload">
      <summary>查看发给 JEV 的完整请求载荷</summary>
      <pre>${esc(JSON.stringify(tr.request || {}, null, 2))}</pre>
    </details>`;

  bindSteps();
  $('tb-toggle').textContent = '全部展开';
  player.sync();
}

function bindSteps() {
  document.querySelectorAll('#pane-trace .step-head').forEach((h) => {
    h.addEventListener('click', () => h.parentElement.classList.toggle('open'));
  });
  document.querySelectorAll('#pane-trace .step-body.evidence').forEach((el) =>
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      highlightEvidence(el.dataset.sno, true);
    }));
}

function setBarsAnimated(body) {
  const bars = [...body.querySelectorAll('[data-w]')];
  bars.forEach((el) => { el.style.transition = 'none'; el.style.width = '0%'; });
  // 用强制回流代替 requestAnimationFrame：后台标签页里 rAF 不触发，会卡在 0%
  void body.offsetWidth;
  bars.forEach((el, k) => setTimeout(() => {
    el.style.transition = '';
    el.style.width = el.dataset.w + '%';
  }, k * 85));
}

/** 兜底：把某步（或全部）的条状图直接置为最终宽度，避免动画被跳过时留空 */
function setBarsFinal(body) {
  (body ? [body] : document.querySelectorAll('#pane-trace .step-body')).forEach((b) =>
    b.querySelectorAll('[data-w]').forEach((el) => {
      el.style.transition = 'none';
      el.style.width = el.dataset.w + '%';
    }));
}

/* ---------- 回放播放器 ---------- */
const PHASE_MS = [700, 1000, 380, 1000, 720];
const reduceMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const player = {
  gen: 0, qno: null, phase: -1, playing: false, revealed: false, speed: 1,
  pending: 0, dueAt: 0, remain: 0, timers: [],

  clear() {
    this.gen += 1;
    this.timers.forEach(clearTimeout);
    this.timers = [];
  },

  /** 切换题目或重新渲染前调用，确保不残留定时器 */
  stop() {
    this.clear();
    this.qno = null; this.phase = -1; this.playing = false; this.revealed = false;
    this.pending = 0; this.remain = 0;
  },

  nodes() { return [...document.querySelectorAll('#pane-trace .steps .step')]; },
  stepsEl() { return document.querySelector('#pane-trace .steps'); },
  tr() { const it = item(state.current); return it ? traceOf(it.no) : null; },

  durationFor(i) {
    const s = this.steps()[i];
    const base = PHASE_MS[i] ?? 600;
    return s && s.type === 'group' ? base + (groupItems(s) || []).length * 110 : base;
  },

  /** 播放中且尚未揭晓答案时，中栏显示骨架 */
  skeletonFor(no) { return this.qno === no && !this.revealed; },

  /** 当前轨迹的步骤数组（缺字段时按空处理，避免 undefined.length 崩掉回放） */
  steps() { const tr = this.tr(); return (tr && tr.steps) || []; },

  start(fromPhase = 0) {
    const tr = this.tr();
    if (!tr || !this.steps().length) return;
    this.clear();
    this.qno = item(state.current).no;
    // 集合答案没有单选揭晓步：它由代码在阈值/组合步之后折出来。
    // 找不到揭晓点时不能挂着骨架等一个永远不会来的事件——那正是"永不揭晓"的静默失败。
    this.revealed = revealStepOf(tr) < 0;
    this.playing = true;
    this.nodes().forEach((n) => n.classList.remove('open', 'playing', 'done', 'revealed', 'risen'));
    this.stepsEl()?.classList.add('playing-mode');
    highlightEvidence(null, false);   // 证据句留到证据定位步再带出
    this.phase = fromPhase - 1;
    this.pending = fromPhase;
    this.remain = 0;
    renderAnswer();
    this.sync();
    this.tick();
  },

  tick() {
    const tr = this.tr();
    if (!tr) return this.finish();
    const i = this.pending;
    if (i >= this.steps().length) return this.finish();
    const wait = this.remain || this.durationFor(i);
    this.remain = 0;
    this.dueAt = Date.now() + wait;
    const g = this.gen;
    this.timers.push(setTimeout(() => {
      if (g !== this.gen || !this.playing) return;
      this.apply(i);
      this.pending = i + 1;
      this.tick();
    }, wait / this.speed));
  },

  apply(i) {
    const tr = this.tr();
    if (!tr) return;
    const nodes = this.nodes();
    nodes.forEach((n, k) => {
      n.classList.toggle('playing', k === i);
      n.classList.toggle('done', k < i);
      if (k !== i) n.classList.remove('open');      // 聚焦当前步
    });
    const node = nodes[i];
    if (!node) return;
    node.classList.add('open');
    this.phase = i;

    const body = node.querySelector('.step-body');
    if (body) {
      if (reduceMotion()) { body.classList.add('revealed', 'risen'); }
      else {
        setTimeout(() => body.classList.add('revealed'), 120);
        setTimeout(() => { body.classList.add('risen'); setBarsAnimated(body); }, 300);
      }
    }

    // 跨栏联动：证据步 → 左栏高亮；揭晓步 → 中栏放出答案。
    // 优先读轨迹声明的 evidence / reveal 标记，缺失时才按步骤名推断（并在中栏标注）。
    const s = tr.steps[i];
    if (s === evidenceStepOf(tr) && s.sentence) highlightEvidence(s.sentence, true);
    if (i === revealStepOf(tr)) { this.revealed = true; renderAnswer(); }
    else if (!this.revealed) renderAnswer();
    this.sync();
  },

  pause() {
    if (!this.playing) return;
    this.playing = false;
    this.remain = Math.max(0, this.dueAt - Date.now());
    this.clear();
    this.sync();
  },

  resume() {
    if (this.playing) return;
    if (this.phase < 0) return this.start(0);
    this.playing = true;
    this.sync();
    this.tick();
  },

  toggle() { this.playing ? this.pause() : this.resume(); },

  /** 单步步进：立即推进一个相位并暂停 */
  step() {
    if (!this.tr()) return;
    if (this.qno === null) {
      this.start(0);
      this.playing = false;
      this.clear();
      this.sync();
      return;
    }
    this.playing = false;
    this.clear();
    const next = this.phase + 1;
    if (next >= this.steps().length) return this.finish();
    this.apply(next);
    this.pending = next + 1;
    this.remain = 0;
  },

  finish() {
    this.playing = false;
    this.revealed = true;
    this.clear();
    const stepsEl = this.stepsEl();
    if (stepsEl) {
      stepsEl.classList.remove('playing-mode');
      stepsEl.querySelectorAll('.step').forEach((n) => {
        n.classList.remove('open', 'playing');
        n.classList.add('done', 'revealed', 'risen');
      });
    }
    this.phase = this.steps().length ? this.steps().length - 1 : -1;
    setBarsFinal();
    showMetrics();
    renderAnswer();
    this.sync();
  },

  expandAll(open) {
    this.nodes().forEach((n) => n.classList.toggle('open', open));
    const btn = $('tb-toggle');
    if (btn) btn.textContent = open ? '全部折叠' : '全部展开';
  },

  sync() {
    const tr = this.tr();
    const total = this.steps().length;
    const has = !!tr && total > 0;
    const done = Math.max(0, Math.min(has ? total : 0, this.phase + 1));
    $('tb-play-icon').textContent = this.playing ? '❚❚' : '▶';
    $('tb-play').title = this.playing ? '暂停（P）' : '播放（P）';
    $('tb-play').disabled = !has;
    $('tb-replay').disabled = !has;
    $('tb-step').disabled = !has;
    $('tb-toggle').disabled = !has;
    $('tb-phase').textContent = has ? `${done} / ${total}` : '— / —';
    $('tb-fill').style.width = `${has ? (done / total) * 100 : 0}%`;
  },
};

function showMetrics() {
  const tr = player.tr();
  const el = $('tb-metrics');
  if (!tr) { el.hidden = true; return; }
  const u = tr.usage || {};
  const bits = [`同一次请求并行完成 <b>${tr.n_questions ?? '—'}</b> 个判断`,
                `实际 <b>${tr.latency_ms ?? '—'}ms</b>`];
  if (u.input_tokens) bits.push(`<b>${u.input_tokens}/${u.output_tokens}</b> tokens`);
  let html = bits.join(' · ');
  const cur = item(tr.no);
  if (cur) {
    const st = statusOf(cur);
    if (st.k === 'warn') html += ' · <span class="warnline">该题依据薄弱，答案不可信</span>';
    else if (st.k === 'bad') html += ' · <span class="warnline">两路判断存在分歧，建议复核</span>';
    else if (st.k === 'stale') html += ' · <span class="warnline">轨迹过旧，请重新求解</span>';
  }
  el.innerHTML = html;
  el.hidden = false;
}

/* ---------- 答案汇总 ---------- */
function summaryStats() {
  const items = state.exam.items;
  const answerable = items.filter((i) => i.answerable);
  const solved = answerable.filter((i) => traceOf(i.no));
  const confs = solved.map((i) => traceOf(i.no).confidence).filter((c) => typeof c === 'number');
  const withKey = items.filter((i) => keyOf(i));
  // 只在「已录入答案且已求解」的题上算正确率；未求解的不计为答错
  const keyed = withKey.filter((i) => traceOf(i.no));
  const hit = keyed.filter((i) => normCompare(traceOf(i.no).answer) === normCompare(keyOf(i)));
  const byStatus = { ok: 0, bad: 0, warn: 0, stale: 0, low: 0, na: 0 };
  items.forEach((i) => { byStatus[statusOf(i).k] += 1; });
  const msList = solved.map((i) => traceOf(i.no).wall_ms ?? traceOf(i.no).latency_ms ?? 0);
  const totalMs = msList.reduce((a, b) => a + b, 0);
  const tokens = solved.reduce((a, i) => {
    const u = traceOf(i.no).usage || {};
    return a + (u.input_tokens || 0) + (u.output_tokens || 0);
  }, 0);
  // 改卷计分：只统计「已录入答案 + 已求解 + 有分值」的题。
  // 分值优先取题目自带 score，缺了退回小节的 per（每题 X 分）；
  // 未求解/无分值的题不进总分，避免把没改的题当成零分。
  const secPer = {};
  (state.exam.sections || []).forEach((sec) => { secPer[sec.id] = sec.per; });
  const scoreOf = (i) => (i.score != null ? i.score : secPer[i.section_id]);
  const scored = keyed.filter((i) => scoreOf(i) != null);
  const earned = scored
    .filter((i) => normCompare(traceOf(i.no).answer) === normCompare(keyOf(i)))
    .reduce((a, i) => a + (scoreOf(i) || 0), 0);
  const full = scored.reduce((a, i) => a + (scoreOf(i) || 0), 0);
  return {
    totalMs,
    avgMs: solved.length ? totalMs / solved.length : 0,
    tokens,
    run: state.run || null,
    answerable: answerable.length,
    solved: solved.length,
    disagree: byStatus.bad,
    weak: byStatus.warn,
    stale: byStatus.stale,
    avg: confs.length ? confs.reduce((a, b) => a + b, 0) / confs.length : 0,
    keyed: keyed.length,
    keyedOnly: withKey.length - keyed.length,
    hit: hit.length,
    scoredN: scored.length,
    earned,
    full,
  };
}

function renderSummary() {
  const s = summaryStats();
  const cards = [
    { n: s.answerable, l: '可作答题目' },
    { n: `${s.solved}/${s.answerable}`, l: '已求解', c: s.solved === s.answerable ? 'ok' : '' },
    { n: s.disagree, l: '两路判断分歧', c: s.disagree ? 'bad' : 'ok' },
    { n: s.weak, l: '依据薄弱', c: s.weak ? 'warn' : 'ok' },
    ...(s.stale ? [{ n: s.stale, l: '轨迹过旧需重算', c: 'warn' }] : []),
    { n: pct(s.avg), l: '平均置信度' },
    { n: fmtMs(s.totalMs), l: '全卷累计耗时', c: 'time' },
  ];
  if (s.run && s.run.elapsed_s) {
    // 如实标注：记录的是**最近一次**求解批次，可能不是全卷（全卷看左侧「累计耗时」）
    cards.push({ n: `${s.run.elapsed_s} 秒`,
                 l: `最近一次求解 ${s.run.n} 题（并发 ${s.run.concurrency}）`, c: 'time' });
  }
  if (s.tokens) cards.push({ n: s.tokens.toLocaleString(), l: '累计 token', c: 'time' });
  if (s.keyed) {
    cards.push({ n: `${s.hit}/${s.keyed}`, l: '与标准答案一致', c: s.hit === s.keyed ? 'ok' : 'warn' });
    cards.push({ n: s.keyed ? pct(s.hit / s.keyed) : '—', l: '对比正确率', c: s.hit === s.keyed ? 'ok' : 'bad' });
    cards.push({ n: s.keyed - s.hit, l: '答错', c: s.keyed - s.hit ? 'bad' : 'ok' });
  }
  if (s.full > 0) {
    cards.push({ n: `${s.earned}/${s.full}`,
                 l: `总分 · 得分率 ${pct(s.earned / s.full)}（${s.scoredN} 题）`,
                 c: s.earned === s.full ? 'ok' : '' });
  }

  const sectionsHtml = state.exam.sections.map((sec) => {
    const items = state.exam.items.filter((i) => i.section_id === sec.id);
    if (!items.length) return '';
    const rows = items.map((it) => {
      const st = statusOf(it);
      const t = traceOf(it.no);
      const conf = t && typeof t.confidence === 'number' ? t.confidence : null;
      const k = keyOf(it);
      const right = !!(k && t && normCompare(t.answer) === normCompare(k));
      const cmp = !k ? '<span style="color:var(--ink-3)">未录入</span>'
        : !t ? `<span class="mini na">${esc(k)}</span>`
        : right ? `<span class="mini ok">${esc(t.answer)} = ${esc(k)} ✓</span>`
                : `<span class="mini bad">${esc(t.answer ?? '—')} ≠ ${esc(k)} ✗</span>`;
      const preview = it.kind === 'seven' ? `第 ${it.no} 空` : String(it.stem ?? '').slice(0, 34);
      const note = !it.answerable ? (NA_REASON[it.kind] || it.notice || '不可作答')
        : st.k === 'stale' ? '轨迹由旧版代码生成，需重新求解'
          : st.k === 'bad' ? '两路判断结论不同，建议复核'
            : st.k === 'warn' ? '逐选项支持度普遍很低，答案不可信'
              : t ? (latexOn() ? tex(preview) : esc(preview)) : '尚未求解';
      if (state.onlyWrong && (right || !k || !t)) return '';   // 只看错题时，跳过非错题
      return `<tr class="q-row ${it.no === state.current ? 'is-current' : ''} ${right ? '' : (k && t ? 'is-wrong' : '')} ${it.answerable ? '' : 'na-row'}" data-no="${it.no}">
        <td class="qno">${it.no}</td>
        <td class="ans ${k && t ? (right ? 'ok' : 'bad') : ''}">${t && t.answer ? esc(t.answer) : '—'}</td>
        <td class="cbar">${conf === null ? '' : `<div style="display:flex;align-items:center;gap:7px">
          <span class="track" style="flex:1"><span class="fill ${conf < 0.6 ? 'bad' : conf < 0.85 ? 'low' : ''}"
            style="width:${Math.max(conf * 100, 2)}%"></span></span>
          <span class="cnum">${pct(conf)}</span></div>`}</td>
        <td><span class="mini ${st.k}">${st.label}</span></td>
        <td>${cmp}</td>
        <td class="note">${note}</td>
      </tr>`;
    }).join('');
    return `<div class="sum-sec">
      <div class="sum-sec-head">${esc(sec.name)}
        <em>第 ${sec.no_from}–${sec.no_to} 题 · ${sec.per ? `每题 ${sec.per} 分`
          : (sec.full ? `共 ${sec.full} 分` : `共 ${sec.count || (sec.no_to - sec.no_from + 1)} 题`)}</em></div>
      <table class="sum">
        <thead><tr>
          <th>题号</th><th>JEV 答案</th><th>置信度</th><th>状态</th><th>标准答案</th><th>说明</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  }).join('');

  const keyed = s.keyed;
  // 题库体检：题库自带的校验记录与损失账目。界面之前完全不展示这两样，
  // 于是「这份题库可信吗」在产品里没有任何答案。
  const bm = state.exam.meta || {};
  const vals = state.exam.validations || [];
  const sevN = { error: 0, warn: 0, info: 0 };
  vals.forEach((v) => { sevN[v.severity] = (sevN[v.severity] || 0) + 1; });
  const loss = bm.loss || {};
  const lossBits = [
    `卷面见题号 ${loss.seen_printed_nos ?? '?'} 个`,
    `产出条目 ${loss.kept ?? '?'} 条`,
    `自报丢弃 ${(loss.dropped || []).length} 条`,
    `静默丢失 ${(loss.never_parsed || []).length} 条`,
  ];
  $('modal-body').innerHTML = `
    <div class="key-panel">
      <div class="key-panel-head">
        <b>标准答案对照</b>
        <span class="conf">本卷选项字母：${bankLetters().join('、') || '（题库未提供选项）'}
          ｜ 题号上限 ${((state.exam && state.exam.items) || []).reduce((a, i) => Math.max(a, i.no), 0) || '?'}
          ｜ 试卷 PDF 内不含答案表，粘贴官方答案后才能比对</span>
        <label class="key-toggle"><input type="checkbox" id="only-wrong"
          ${state.onlyWrong ? 'checked' : ''}> 只看错题</label>
      </div>
      <textarea id="sum-key-input" rows="2"
        placeholder="粘贴答案表，如：21 B 22 C 23 B；也支持 21:B、21.B、21、B、9 ABC 等写法（题号不限位数）"
      ></textarea>
      <div class="key-panel-actions">
        <button class="btn btn-primary" id="sum-key-parse">解析并比对</button>
        <button class="btn" id="sum-key-clear">清空全部答案</button>
        <span class="conf">已录入 ${keyed} 题${keyed ? `，其中答对 ${s.hit} 题` : ''}</span>
      </div>
      <div id="sum-key-hint" class="key-hint"></div>
    </div>
    <div class="sum-cards">${cards.map((c) => `
      <div class="sum-card ${c.c || ''}"><b>${c.n}</b><span>${c.l}</span></div>`).join('')}</div>
    <div class="key-panel">
      <div class="key-panel-head">
        <b>题库体检</b>
        <span class="conf">题库核验：${bm.verified === true ? '已通过独立核验' : '尚未通过独立核验'}${(bm.verified_blockers || []).filter((b) => !/独立核验/.test(b)).length
          ? `（${esc((bm.verified_blockers || []).filter((b) => !/独立核验/.test(b)).join('；'))}）` : ''}</span>
      </div>
      ${vals.length ? `<ul class="val-list">${vals.map((v) => `
        <li class="val-row val-${esc(v.severity || 'info')}">
          <span class="val-sev">${esc(v.severity || 'info')}</span>
          <code>${esc(v.code)}</code>
          <span class="val-detail">${esc(v.detail || '')}</span>
        </li>`).join('')}</ul>`
        : '<div class="conf">本题库没有任何校验记录。</div>'}
      <div class="conf" style="margin-top:7px">损失账目：${esc(lossBits.join(' ｜ '))}。丢题不一定等于缺陷，但每一条都必须能在上面的记录里找到解释。</div>
      <details style="margin-top:8px">
        <summary class="bank-report-toggle">完整体检报告（解析器生成，含逐条依据）</summary>
        <pre class="bank-report">${esc((state.exam.report || []).join('\n'))}</pre>
      </details>
    </div>
    ${sectionsHtml}
    <div class="note" style="color:var(--ink-3);font-size:11.5px;line-height:1.7">
      状态说明：<b>一致</b> ＝ 直接作答与逐选项支持度结论相同；<b>存在分歧</b> ＝ 两者结论不同，建议人工复核；
      <b>依据薄弱</b> ＝ 所有选项的逐选项支持度都低于 50%，通常意味着该题依赖的材料（图表、听力音频）不在文本内，答案不可信；
      <b>轨迹过旧</b> ＝ 该轨迹由旧版代码生成，缺失判定字段，需重新求解；
      <b>低置信度</b> ＝ 模型自报置信度低于 50%，实测中错题多落在此区间；
      <b>注意</b>：以上告警都**不能**保证答案正确——实测中存在高置信度、两路自洽但仍答错的题；
      <b>不可作答</b> ＝ 该题型需音频或需生成文本，JEV 不具备相应能力。<br>
      试卷 PDF 内不含答案表，标准答案需在中栏手动录入后方可比对。
    </div>`;
}

function summaryText() {
  const s = summaryStats();
  const head = [
    `${state.exam.meta.title || state.exam.meta.subject} · JEV 判定答案`,
    `模型 ${modelOf()} ｜ 可作答 ${s.answerable} 题，已求解 ${s.solved} 题`
      + ` ｜ 分歧 ${s.disagree} 题，依据薄弱 ${s.weak} 题`,
    `全卷累计耗时 ${fmtMs(s.totalMs)}（平均每题 ${fmtMs(s.avgMs)}）`
      + (s.run?.elapsed_s ? ` ｜ 实际并发求解 ${s.run.elapsed_s} 秒` : '')
      + (s.tokens ? ` ｜ 累计 ${s.tokens} token` : ''),
    '',
  ];
  const body = state.exam.sections.map((sec) => {
    const items = state.exam.items.filter((i) => i.section_id === sec.id);
    if (!items.length) return '';
    const line = items.map((it) => {
      const t = traceOf(it.no);
      return `${it.no} ${t && t.answer ? t.answer : '—'}${keyOf(it) ? `(标准 ${keyOf(it)})` : ''}`;
    }).join('  ');
    return `${sec.name}\n${line}`;
  }).filter(Boolean).join('\n\n');
  const foot = [];
  if (s.keyed) {
    foot.push('', `与标准答案对比：可对比 ${s.keyed} 题，答对 ${s.hit} 题，答错 ${s.keyed - s.hit} 题，正确率 ${(s.hit / s.keyed * 100).toFixed(0)}%`);
    if (s.full > 0) {
      foot.push(`总分：${s.earned} / ${s.full}（得分率 ${(s.earned / s.full * 100).toFixed(0)}%，`
        + `计分范围：已录入答案且有分值的 ${s.scoredN} 题）`);
    }
    const wrong = state.exam.items.filter((x) => {
      const t = traceOf(x.no);
      return keyOf(x) && t && normCompare(t.answer) !== normCompare(keyOf(x));
    });
    if (wrong.length) foot.push('答错题目：' + wrong.map((x) => `${x.no}(JEV ${traceOf(x.no).answer} / 标准 ${keyOf(x)})`).join('  '));
  }
  // 不可作答的原因来自本卷数据（notice / NA_REASON），不写死某个科目的说明
  const naKinds = [...new Set(state.exam.items
    .filter((x) => !x.answerable).map((x) => x.notice || NA_REASON[x.kind] || x.kind))];
  foot.push('', '注：标 — 者为未求解或不可自动作答。'
    + (naKinds.length ? `本卷不可作答原因：${naKinds.join('；')}。` : ''));
  return head.concat([body]).concat(foot).join('\n');
}

function openSummary() {
  bindSummary.hint = '';
  renderSummary();
  $('modal').hidden = false;
  bindSummary();
}

function bindSummary() {
  const body = $('modal-body');
  const rerender = () => { renderSummary(); bindSummary(); };
  const hintEl = $('sum-key-hint');
  if (hintEl && bindSummary.hint) hintEl.innerHTML = bindSummary.hint;
  const parse = $('sum-key-parse');
  if (parse) parse.addEventListener('click', () => {
    const res = parseKey($('sum-key-input').value);
    const out = applyKeyParse(res, null);
    if (!out.n) return;
    // applyKeyParse 已经重渲染过三栏；这里只重建弹窗内容并把告警塞回去
    bindSummary.hint = out.html;
    rerender();
  });
  const clr = $('sum-key-clear');
  if (clr) clr.addEventListener('click', () => {
    state.key = {}; localStorage.removeItem(keyStore());
    renderStats(); renderRail(); renderQuestion(); renderAnswer();
    rerender();
    toast('已清空标准答案');
  });
  const ow = $('only-wrong');
  if (ow) ow.addEventListener('change', () => { state.onlyWrong = ow.checked; rerender(); });
  body.querySelectorAll('.q-row').forEach((tr) =>
    tr.addEventListener('click', () => { select(Number(tr.dataset.no)); closeSummary(); }));
}

function closeSummary() { $('modal').hidden = true; }

/* ---------- 清空解答 ---------- */
function openClearConfirm() {
  if (!state.exam) return;
  const solvedAll = state.exam.items.filter((i) => i.answerable && traceOf(i.no)).length;
  const cur = item(state.current);
  const hasCur = !!traceOf(state.current) && cur && cur.answerable;
  // 重算成本按本卷实测均值估，不写死题数与秒数
  const n = state.exam.items.length;
  const msList = state.exam.items.map((i) => (traceOf(i.no) || {}).wall_ms).filter((x) => typeof x === 'number');
  const avgSec = msList.length ? msList.reduce((a, b) => a + b, 0) / msList.length / 1000 : 0;
  $('confirm-body').innerHTML = `
    将删除<b>已求解的 JEV 判定轨迹</b>（保存在 <code>data/traces_${esc(state.paper)}/</code>），
    对应题目会回到「未求解」，右栏的运算过程一并消失。
    <div class="warn-box">
      <b>注意：</b>轨迹是调用 JEV 接口算出来的，删除后重新求解需<b>再次消耗 API 配额</b>与时间
      （本卷共 ${n} 题${avgSec ? `，已求解题平均单题端到端 ${avgSec.toFixed(1)} 秒`
        : '；尚无耗时样本，无法估算重算时间'}）。
    </div>
    <ul>
      <li>已录入的<b>标准答案是浏览器本地数据，不受影响</b>（需清空请用中栏的「清空全部」）</li>
      <li>试卷题面与解析结果不受影响</li>
    </ul>`;
  $('confirm-foot').innerHTML = `
    <button class="btn" data-act="cancel">取消</button>
    <button class="btn" data-act="one" ${hasCur ? '' : 'disabled'}>
      清空本题（第 ${state.current} 题）</button>
    <button class="btn btn-danger" data-act="all" ${solvedAll ? '' : 'disabled'}>
      清空全卷（${solvedAll} 份轨迹）</button>`;
  $('confirm').hidden = false;
  $('confirm-foot').querySelectorAll('button').forEach((b) =>
    b.addEventListener('click', () => {
      const act = b.dataset.act;
      if (act === 'cancel') return closeConfirm();
      clearTraces(act === 'one' ? [state.current] : null);
    }));
}

function closeConfirm() { $('confirm').hidden = true; }

async function clearTraces(nos) {
  try {
    const r = await fetch('/api/clear', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(nos ? { paper: state.paper, nos } : { paper: state.paper }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || '清空失败');
    (data.removed || []).forEach((id) => delete state.traces[id]);
    buildTraceIndex();
    closeConfirm();
    player.stop();
    renderStats(); renderRail(); renderQuestion(); renderAnswer(); renderTrace();
    toast(data.removed.length ? `已清空 ${data.removed.length} 份判定轨迹` : '没有可清空的轨迹');
  } catch (e) {
    toast(`清空失败：${e.message}`);
  }
}

/* ---------- 上传题库 ---------- */
async function uploadBank(name, bankObj) {
  const btn = $('btn-upload');
  if (btn.disabled) return;
  btn.disabled = true;
  const orig = btn.textContent;
  btn.innerHTML = '<span class="spin"></span>校验中…';
  try {
    const r = await fetch('/api/upload', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, bank: bankObj }),
    });
    const data = await r.json().catch(() => null);
    if (!r.ok) {
      if (data && Array.isArray(data.errors) && data.errors.length) {
        return showUploadErrors(data.errors);
      }
      throw new Error((data && data.detail) || `HTTP ${r.status}`);
    }
    state.papers = await fetchPapers();
    renderPaperSwitch();
    toast(`已导入「${data.title}」：${data.total_items} 题，可作答 ${data.answerable} 题`
      + (data.replaced ? '（替换同名题库）' : '')
      + (data.warnings && data.warnings.length
        ? `；${data.warnings.length} 条告警见「答案汇总 → 题库体检」` : ''));
    await loadPaper(data.paper);
  } catch (e) {
    toast(`上传失败：${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

/** 上传被拒：error 逐条展示（复用确认弹窗），绝不静默成一句「失败」。 */
function showUploadErrors(errors) {
  $('confirm-title').textContent = '题库未通过结构验收';
  $('confirm-body').innerHTML = `
    <p>这份题库存在下列错误，<b>整体未入库</b>。修正后重新上传即可：</p>
    <ul class="upload-errors">${errors.map((e) =>
      `<li><code>${esc(e.code)}</code>　${esc(e.message || e.detail || '')}</li>`).join('')}</ul>`;
  $('confirm-foot').innerHTML = `<button class="btn" data-act="cancel">关闭</button>`;
  $('confirm').hidden = false;
  $('confirm-foot').querySelectorAll('button').forEach((b) =>
    b.addEventListener('click', closeConfirm));
}

/* ---------- 求解 ---------- */
async function solve(nos) {
  if (state.busy || !state.exam) return;
  state.busy = true;
  const btnAll = $('btn-solve-all'), btnOne = $('btn-solve-one');
  const origAll = btnAll.dataset.orig || '求解全卷';
  btnAll.disabled = btnOne.disabled = true;
  const all = !nos;
  btnAll.innerHTML = all ? '<span class="spin"></span>求解中…' : origAll;
  if (!all) btnOne.innerHTML = '<span class="spin"></span>求解中…';

  try {
    const r = await fetch('/api/solve', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(nos ? { paper: state.paper, nos } : { paper: state.paper, force: false }),
    });
    const data = await r.json().catch(() => null);
    if (!r.ok) throw new Error((data && data.detail) || `HTTP ${r.status}`);
    Object.assign(state.traces, data.traces || {});
    buildTraceIndex();
    const errs = (data.results || []).filter((x) => x.status === 'error');
    const dropped = (data.results || []).filter((x) => x.discarded);
    let msg = errs.length ? `${errs.length} 题失败：${errs.map((e) => e.id).join(', ')}` : '求解完成';
    if (dropped.length) msg += `；${dropped.length} 题旧缓存与当前题目内容不符，已重算（各花一次调用）`;
    toast(msg);
  } catch (e) {
    toast(`求解失败：${e.message}`);
  } finally {
    state.busy = false;
    btnAll.disabled = btnOne.disabled = false;
    btnAll.textContent = origAll;
    btnOne.textContent = '求解本题';
    renderStats(); renderRail(); renderQuestion(); renderAnswer(); renderTrace();
    // 单题求解后自动回放一次，便于演示；整卷求解不自动播放，避免连续播放
    if (nos && nos.length === 1 && traceOf(nos[0])) player.start(0);
  }
}

let toastTimer;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2600);
}

/* 标准答案：优先用用户录入的；没有则用题库自带的官方答案 */
const keyOf = (it) => (it && (state.key[it.no] || it.standard_answer)) || null;

/* ---------- 标准答案存储（按试卷分开，避免不同卷的题号互相覆盖） ---------- */
const keyStore = () => `jev_answer_key_${state.paper}`;
const LEGACY_KEY_STORE = 'jev_answer_key';   // 早期版本的全局键（不区分试卷）

const readStoredKey = (k) => {
  const raw = localStorage.getItem(k);
  if (!raw) return null;
  try {
    const v = JSON.parse(raw);
    return v && typeof v === 'object' ? v : null;
  } catch (e) {
    toast(`本地标准答案数据已损坏（${k}），已忽略；请重新录入`);
    return null;
  }
};

function loadKey() {
  const own = readStoredKey(keyStore());
  if (own && Object.keys(own).length) { state.key = own; return; }
  state.key = own || {};
  // 旧版本把所有试卷的答案存在一个全局键下。迁移到"当前这份试卷"名下而不是写死某个科目，
  // 否则题库一改 id，用户粘过的答案就永久读不回来了。
  const legacy = readStoredKey(LEGACY_KEY_STORE);
  if (legacy && !Object.keys(state.key).length) {
    state.key = legacy;
    saveKey();
    toast(`已将旧版全局标准答案（${Object.keys(legacy).length} 题）迁移到当前试卷 ${state.paper}`);
  }
}

function saveKey() { localStorage.setItem(keyStore(), JSON.stringify(state.key)); }

/* ---------- 试卷切换 ---------- */
function renderPaperSwitch() {
  const sw = $('paper-switch');
  if (!state.papers.length) { sw.innerHTML = ''; return; }
  sw.innerHTML = state.papers.map((p) => `
    <button data-paper="${p.id}" class="${p.id === state.paper ? 'on' : ''}"
            title="${esc(p.title || p.id)}${p.exam_ready === false ? `（${esc(p.error || '题库不可用')}）` : ''}">${
              esc(p.subject || p.id)}<em>${p.solved}${p.exam_ready === false ? ' ·异常' : ''}</em></button>`).join('');
  sw.querySelectorAll('button').forEach((b) =>
    b.addEventListener('click', () => {
      if (b.dataset.paper !== state.paper) loadPaper(b.dataset.paper);
    }));
}

/** 题库清单里这一卷的条目（可能不存在，如直接用一个未注册的 id 打开）。 */
const paperEntry = (id) => state.papers.find((p) => p.id === id) || null;

/** 三栏清空：加载失败时不能把上一份试卷的内容留在屏幕上。 */
function clearChrome() {
  $('q-meta').textContent = ''; $('a-meta').textContent = ''; $('t-meta').textContent = '';
  $('pane-question').innerHTML = ''; $('pane-answer').innerHTML = ''; $('pane-trace').innerHTML = '';
  $('tb-metrics').hidden = true;
}

const NEXT_STEPS = `
  <ol class="guide-steps">
    <li><a href="/api/template" download="bank_template.json">下载题库模板</a>
      （或直接取仓库里的 <code>templates/bank_template.json</code>）。
      怎么填见 <code>templates/GUIDE.md</code>（手把手教程）与
      <code>templates/README.md</code>（字段速查）。</li>
    <li>把题目按模板填成 JSON：题目（items）必须给 <code>stem</code> 与 <code>options</code>；
      阅读材料放进 <code>passages</code>；判定方式由 <code>meta.specs</code> 声明
      （english / xingce / math+math_multi）。</li>
    <li>顶栏「上传题库」选择该文件，或把它放进 <code>data/banks/</code>（文件名即试卷 id）。
      结构验收不过会逐条给出原因；通过后刷新即出现在顶栏。</li>
    <li>人工核对题面内容与原卷一致——验收器只查结构，查不出「抄错题面」；
      题面错一行，改卷就错一题。核对完再点「求解全卷」。</li>
  </ol>`;

/** 分不出种类的加载错误也不许含糊：说清楚是哪一步、哪个接口、返回了什么。 */
function renderEmptyState(paperId, err) {
  const entry = paperEntry(paperId);
  const status = Number(err && err.httpStatus);
  let kind = (err && err.kind) || 'unknown';
  if (kind === 'unknown' && !state.papers.length) kind = 'nobank';
  else if (kind === 'unknown' && !entry) kind = 'unregistered';
  else if (kind === 'unknown' && entry && entry.exam_ready === false) kind = 'missing';
  else if (kind === 'unknown' && status === 503) kind = 'missing';

  const head = {
    nobank: ['仓库里还没有任何题库', '本仓库不做试卷提取：按 templates/ 模板制作题库 JSON 并上传，工作台才有卷可改。'],
    unregistered: [`没有 id 为 ${esc(paperId)} 的题库`, '地址栏指定的题库不在注册表里，因此这里没有可显示的内容——不会回退到别的题库。'],
    missing: [`题库 ${esc(paperId)} 已注册，但读不出来`,
      entry && entry.error ? `原因：${esc(entry.error)}`
        : '注册表指向的题库 JSON 不存在——请重新上传，或把文件放回 data/banks/。'],
    malformed: [`题库 ${esc(paperId)} 内容不合法`, '文件在，但读不出题目——问题出在题库文件本身，不是加载器。'],
    unknown: ['无法读取题库', '接口没有给出可辨识的原因，按下面的顺序自查。'],
  }[kind];

  $('pane-question').innerHTML = `
    <div class="block"><div class="block-title">引导 · 从零到一份可判定的题库</div>
      <div class="card card-pad guide">
        <h3>${esc(head[0])}</h3>
        <p>${head[1]}</p>
        ${NEXT_STEPS}
        <p class="conf">服务未启动时请先运行 <code>python src/server.py</code>，再打开 http://127.0.0.1:8848</p>
        ${state.papersError ? `<p class="key-warn">读取 /api/papers 也失败了：${esc(state.papersError)}</p>` : ''}
      </div></div>`;
  $('pane-answer').innerHTML = `<div class="empty"><div class="big">—</div>
    <p><span class="tag-na">无题库数据</span></p>
    <p>左栏列出补齐题库所需的四个步骤。</p></div>`;
  $('pane-trace').innerHTML = `<div class="empty"><div class="big">—</div>
    <p>没有判定轨迹可回放：本题库尚未产出任何 <code>data/traces_*/</code> 文件。</p></div>`;
  $('rail-body').innerHTML = `<div class="sec"><div class="sec-name">未注册题库</div>
    <div class="sec-meta">${state.papers.length
      ? `清单里有 ${state.papers.length} 项，但没有 ${esc(paperId)}`
      : '/api/papers 返回空列表'}</div></div>`;
  $('stats').innerHTML = '';
  $('btn-solve-all').disabled = true;
  $('btn-solve-one').disabled = true;
}

async function loadPaper(paperId) {
  const lock = $('paper-switch');
  lock.style.pointerEvents = 'none';
  try {
    const [exam, traces, run] = await Promise.all([
      getJSON(`/api/exam?paper=${encodeURIComponent(paperId)}`),
      getJSON(`/api/traces?paper=${encodeURIComponent(paperId)}`).then((t) => t ?? {}),
      fetch(`/api/run?paper=${encodeURIComponent(paperId)}`)
        .then((r) => (r.ok ? r.json() : {})).catch(() => ({})),
    ]);
    // 切卷即重置该卷派生数据：字母集、LaTeX 探测结果与本地标准答案都属于"这份卷"
    state.paper = paperId;
    state.letters = null;
    state.mathProbe = false;
    state.exam = exam;
    state.traces = traces || {};
    buildTraceIndex();
    state.run = run || null;
    const items = Array.isArray(exam.items) ? exam.items : [];
    if (!items.length) {
      throw Object.assign(new Error(
        `题库已注册但内容为空：${paperId} 的 items 为 0 条。`
        + '这不是加载器的问题——请检查题库文件的题目数组。'), { kind: 'malformed' });
    }
    // 兜底信号：题库清单没声明 has_latex 时，按卷内是否真的出现 $...$ 判断
    if (!paperHasLatex()) {
      state.mathProbe = items.some((it) => hasInlineMath(it.stem)
        || Object.values(it.options || {}).some(hasInlineMath)
        || (it.subs || []).some(hasInlineMath) || (it.appendices || []).some(hasInlineMath));
    }
    loadKey();
    state.idPrefix = (items[0]?.id || 'Q').replace(/\d+$/, '') || 'Q';
    const first = items.find((i) => i.answerable) || items[0];
    if (!first || first.no == null) {
      throw Object.assign(new Error(
        `题库 ${paperId} 的题目缺少 no 字段，无法定位题号。`), { kind: 'malformed' });
    }
    state.current = first.no;
    player.stop();
    const name = bankName(exam);
    document.title = `${name} · JEV 智能改卷系统`;
    $('app-title').textContent = name;
    const n = items.filter((i) => i.answerable).length;
    const btnAll = $('btn-solve-all');
    btnAll.textContent = `求解全卷 ${n} 题`;
    btnAll.dataset.orig = btnAll.textContent;
    btnAll.disabled = !n;
    $('btn-solve-one').disabled = false;
    clearChrome();
    renderPaperSwitch(); renderStats(); renderRail();
    renderQuestion(); renderAnswer(); renderTrace();
    setCollapsed(localStorage.getItem(COLLAPSE_KEY) === '1');
    return true;
  } catch (e) {
    state.exam = null;
    state.traces = {};
    state.current = null;
    state.paper = null;          // 别让失败的试卷切换把界面留在上一份卷的身份上
    player.stop();
    $('app-title').textContent = 'JEV 智能改卷系统';
    $('btn-solve-all').textContent = '求解全卷';
    $('btn-solve-all').dataset.orig = $('btn-solve-all').textContent;
    clearChrome();
    renderPaperSwitch(); renderStats(); renderRail();
    renderEmptyState(paperId, e);
    toast(`加载试卷失败：${e.message}`);
    return false;
  } finally {
    lock.style.pointerEvents = '';
  }
}

/* ---------- 轨迹版本：接口优先，兜底常量必须说出来 ---------- */
function applyTraceVersion(papersPayload) {
  const p = papersPayload || {};
  const raw = Array.isArray(p) ? undefined : p.trace_version;
  if (Number.isFinite(raw)) {
    state.traceVersion = raw;
    state.traceVersionSource = 'api';
    return;
  }
  state.traceVersion = FALLBACK_TRACE_VERSION;
  state.traceVersionSource = 'fallback';
}

/** 取 /api/papers：兼容"数组"与"{papers, trace_version}"两种返回形态。 */
async function fetchPapers() {
  let data;
  try {
    data = await getJSON('/api/papers');
  } catch (e) {
    // 后端整体不可用（服务没起 / 路由变更）也要能起来，但要说明版本值是兜底的
    data = null;
    state.papersError = e.message;
  }
  applyTraceVersion(data);
  return Array.isArray(data) ? data : ((data && data.papers) || []);
}

/* ---------- 启动 ---------- */
async function boot() {
  const urlPaper = new URLSearchParams(location.search).get('paper');
  try {
    state.papers = await fetchPapers();
  } catch (e) {
    document.body.insertAdjacentHTML('beforeend',
      `<div class="notice" style="margin:14px">无法读取题库清单：${esc(e.message)}　请确认已运行 python src/server.py</div>`);
    return;
  }
  bindChrome();

  // 指定了却没注册的 id：直接说明，不去碰 /api/exam（它会用后端默认卷兜底，界面就变成另一份试卷）
  if (urlPaper && !paperEntry(urlPaper)) {
    state.paper = null;
    clearChrome(); renderPaperSwitch(); renderStats(); renderRail();
    renderEmptyState(urlPaper, Object.assign(new Error('未注册'), { kind: 'unregistered' }));
    return;
  }
  await loadPaper(urlPaper || state.papers[0]?.id || '__none__');
}

/** 与题库无关的外壳交互：即使一份题库都没加载成功也必须可用。 */
function bindChrome() {
  $('btn-collapse').addEventListener('click', () => setCollapsed(true));
  $('answer-strip').addEventListener('click', () => setCollapsed(false));
  $('btn-upload').addEventListener('click', () => $('file-upload').click());
  $('file-upload').addEventListener('change', async (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!f) return;
    let bankObj;
    try { bankObj = JSON.parse(await f.text()); }
    catch (err) { return toast(`文件不是合法 JSON：${err.message}`); }
    if (!bankObj || typeof bankObj !== 'object' || Array.isArray(bankObj)) {
      return toast('题库顶层必须是 JSON 对象（含 meta 与 items）');
    }
    uploadBank(f.name, bankObj);
  });
  $('btn-summary').addEventListener('click', () => { if (state.exam) openSummary(); });
  $('btn-close').addEventListener('click', closeSummary);
  $('modal-mask').addEventListener('click', closeSummary);
  $('btn-clear').addEventListener('click', () => { if (state.exam) openClearConfirm(); });
  $('confirm-mask').addEventListener('click', closeConfirm);
  $('btn-copy').addEventListener('click', async () => {
    if (!state.exam) return toast('没有题库可复制');
    try {
      await navigator.clipboard.writeText(summaryText());
      toast('答案汇总已复制到剪贴板');
    } catch {
      toast('复制失败，请手动选择文本');
    }
  });

  $('btn-solve-one').addEventListener('click', () => solve([state.current]));
  $('btn-solve-all').addEventListener('click', () => {
    const nos = (state.exam ? state.exam.items : []).filter((i) => i.answerable).map((i) => i.no);
    if (!nos.length) return toast('本卷没有可求解的题目');
    solve(nos);
  });

  // 回放控制条
  $('tb-play').addEventListener('click', () => {
    if (player.phase < 0) return player.start(0);
    player.toggle();
  });
  $('tb-replay').addEventListener('click', () => player.start(0));
  $('tb-step').addEventListener('click', () => player.step());
  $('tb-speed').addEventListener('change', (e) => { player.speed = Number(e.target.value) || 1; });
  $('tb-toggle').addEventListener('click', () => {
    const allOpen = player.nodes().length > 0 &&
      player.nodes().every((n) => n.classList.contains('open'));
    player.expandAll(!allOpen);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (!$('confirm').hidden) return closeConfirm();
      if (!$('modal').hidden) return closeSummary();
    }
    if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
    if (!state.exam) return;
    if (e.key === 'c' || e.key === 'C') {
      setCollapsed(!document.getElementById('answer-section').classList.contains('is-collapsed'));
      return;
    }
    if (e.key === 's' || e.key === 'S') return openSummary();
    if (e.key === 'p' || e.key === 'P') {
      if (player.phase < 0) player.start(0); else player.toggle();
      return;
    }
    if (e.key === 'n' || e.key === 'N') return player.step();
    if (e.key === 'r' || e.key === 'R') return player.start(0);
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const all = state.exam.items.map((i) => i.no);
    const i = all.indexOf(state.current);
    const next = all[Math.min(all.length - 1, Math.max(0, i + (e.key === 'ArrowDown' ? 1 : -1)))];
    if (next !== state.current) select(next);
  });
}

boot();
