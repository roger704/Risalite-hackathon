/* SpotWise dashboard. Run data is always loaded through the research API. */
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const safeUrl = u => { try { const p = new URL(u); return p.protocol === 'http:' || p.protocol === 'https:' ? p.href : null; } catch (_) { return null; } };
const link = (u, text) => { const h = safeUrl(u); return h ? `<a href="${esc(h)}" target="_blank" rel="noopener">${esc(text)}</a>` : `<span class="mut">${esc(text)}</span>`; };
const params = new URLSearchParams(location.search);
const API = (params.get('api') || '').replace(/\/$/, '');
const STAGES = ['plan', 'approve', 'data', 'research', 'skeptic', 'fact-check', 'write', 'done'];

let RUN = null;      // ResearchResponse (plan)
let APPROVAL = null; // ApproveRequest sent
let R = null;        // RunResult (merged)
let TOKEN = null;
let state = { on: {}, w: {}, mode: 'present' };
let CITE = {};       // evidence_id -> n
let map = null, timer = null, doneT = null, STREAM_CTRL = null, EXPORT_CTRL = null, EXPORT_WINDOW = null, GEN = 0;

$('#mode-badge').textContent = 'LIVE';
$('#mode-badge').classList.add('live');

function show(id) { ['s-ask', 's-plan', 's-live', 's-dash'].forEach(s => $('#' + s).classList.toggle('hidden', s !== id)); window.scrollTo(0, 0); }
function err(sel, msg) { const e = $(sel); e.textContent = msg; e.classList.toggle('hidden', !msg); }
const apiUrl = path => API + path;
const headers = extra => ({ ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}), ...extra });
async function getJSON(path, opt = {}) {
  const response = await fetch(apiUrl(path), { ...opt, headers: headers(opt.headers || {}) });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(`${opt.method || 'GET'} ${path} → HTTP ${response.status}: ${payload.detail || payload.error?.message || 'request failed'}`);
  return payload;
}
const post = (path, body) => getJSON(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body)
});
$$('[data-export]').forEach(button => { button.onclick = () => exportRun(button.dataset.export); });

async function exportRun(fmt) {
  if (!RUN || !R || !TOKEN) return;
  const gen = GEN, isHtml = fmt === 'report.html';
  const reportWindow = isHtml ? window.open('about:blank', '_blank') : null;
  if (isHtml && !reportWindow) return err('#export-err', 'Allow pop-ups to open the HTML report.');
  EXPORT_WINDOW = reportWindow;
  const controller = new AbortController();
  EXPORT_CTRL = controller;
  $$('[data-export]').forEach(button => { button.disabled = true; });
  err('#export-err', '');
  const query = new URLSearchParams();
  if (!R.candidates) {
    query.set('layers', Object.keys(state.on).filter(id => state.on[id]).join(','));
    query.set('mode', state.mode);
  }
  const queryString = query.toString();
  const path = `/runs/${encodeURIComponent(RUN.run_id)}/export/${encodeURIComponent(fmt)}${queryString ? `?${queryString}` : ''}`;
  let objectUrl = null;
  try {
    const response = await fetch(apiUrl(path), { headers: headers(), signal: controller.signal });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const detail = payload.detail;
      const message = typeof detail === 'string' ? detail : detail?.message || detail?.error || payload.error?.message || 'request failed';
      throw new Error(`HTTP ${response.status}: ${message}`);
    }
    if (gen !== GEN) return;
    const blob = await response.blob();
    if (gen !== GEN) return;
    objectUrl = URL.createObjectURL(blob);
    if (isHtml) {
      reportWindow.addEventListener('load', () => URL.revokeObjectURL(objectUrl), { once: true });
      reportWindow.location.href = objectUrl;
      setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
      EXPORT_WINDOW = null;
    } else {
      const anchor = document.createElement('a');
      anchor.href = objectUrl;
      anchor.download = `spotwise-${RUN.run_id}-${fmt}`;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    }
  } catch (error) {
    if (gen === GEN && error.name !== 'AbortError') err('#export-err', 'Export failed: ' + error.message);
    if (reportWindow && !reportWindow.closed) reportWindow.close();
    if (objectUrl) URL.revokeObjectURL(objectUrl);
  } finally {
    if (gen === GEN) {
      EXPORT_CTRL = null;
      EXPORT_WINDOW = null;
      $$('[data-export]').forEach(button => { button.disabled = false; });
    }
  }
}

/* ---------- 1 · ask ---------- */
$$('.tpl .t').forEach(t => t.onclick = () => { $('#q').value = t.dataset.q; $('#op').value = t.dataset.op || ''; $('#clar').classList.add('hidden'); });
$('#restart').onclick = () => {
  stopLive(); RUN = R = APPROVAL = TOKEN = null;
  $('#clar').classList.add('hidden');
  show('s-ask');
};
$('#back').onclick = () => show('s-ask');
$('#go').onclick = () => ask($('#q').value.trim());
$('#clar-go').onclick = async () => {
  const answer = $('#clar-in').value.trim();
  if (!answer || !RUN) return;
  const gen = GEN;
  try {
    const response = await post(`/runs/${encodeURIComponent(RUN.run_id)}/clarify`, { answer });
    if (gen !== GEN) return;
    RUN = response;
    acceptRun(RUN);
    if (RUN.status === 'needs_clarification') {
      $('#clar-text').textContent = RUN.clarify_question || 'Which district or neighbourhood?';
    } else renderPlan();
  } catch (error) { if (gen === GEN) err('#ask-err', 'Clarification failed: ' + error.message); }
};

async function ask(q) {
  if (!q) { $('#q').focus(); return; }
  stopLive(); TOKEN = null; RUN = R = APPROVAL = null;
  const gen = GEN;
  err('#ask-err', ''); $('#clar').classList.add('hidden');
  const body = { question: q, role: $('#role').value || null, profile: $('#op').value || null };
  try {
    RUN = await post('/research', body);
  } catch (e) {
    if (gen === GEN) err('#ask-err', 'Could not start research: ' + e.message + ' — is the API running?');
    return;
  }
  if (gen !== GEN) return;
  acceptRun(RUN);
  if (RUN.status === 'needs_clarification') {
    $('#clar-text').textContent = RUN.clarify_question || 'Which district or neighbourhood?';
    $('#clar').classList.remove('hidden'); return;
  }
  renderPlan();
}
function acceptRun(response) {
  TOKEN = response.run_token || TOKEN;
  RUN = { ...response };
  delete RUN.run_token;
  $('#mode-badge').textContent = response.mode === 'fixtures' ? 'FIXTURE RUNS (synthetic data)' : 'LIVE';
  $('#mode-badge').classList.toggle('mock', response.mode === 'fixtures');
  $('#mode-badge').classList.toggle('live', response.mode !== 'fixtures');
}

/* ---------- 2 · plan ---------- */
function renderPlan() {
  const P = RUN.plan || { sub_questions: [], layers: [], paid_sources: [] };
  const C = RUN.classification || { shape: RUN.shape };
  $('#p-q').textContent = RUN.question;
  $('#p-class').innerHTML = `<span class="badge b-${esc(C.shape)}">${esc(C.shape)}</span> <span class="why">${esc(C.why)}</span>`;
  const selected = P.area?.area_id || '';
  if (C.shape === 'point') {
    $('#p-area').innerHTML = `<b>${esc(C.address || RUN.question)}</b> → parcel + ~100 m neighbourhood<div class="small mut" style="margin-top:4px">Operator profile: ${esc(RUN.profile || 'none given (operator fit scored neutral)')}</div>`;
  } else {
    const options = (P.area_candidates || []).map(a => `<option value="${esc(a.area_id)}" ${a.area_id === selected ? 'selected' : ''}>${esc(a.name)} · ${esc(a.level)}</option>`).join('');
    $('#p-area').innerHTML = `<div class="row"><select id="area-picker">${options}</select><input id="area-search" type="text" placeholder="Search any Berlin area"><select id="area-search-results" class="hidden"></select></div><div class="small mut" style="margin-top:4px">${esc(P.area?.plr_count || '')} PLR cells · Granularity: PLR</div>`;
    $('#area-search').oninput = search => {
      const query = search.target.value.trim();
      if (!query) return $('#area-search-results').classList.add('hidden');
      const gen = GEN;
      getJSON(`/runs/${encodeURIComponent(RUN.run_id)}/areas?q=${encodeURIComponent(query)}`).then(areas => {
        if (gen !== GEN) return;
        const result = $('#area-search-results');
        result.innerHTML = areas.map(a => `<option value="${esc(a.area_id)}">${esc(a.name)} · ${esc(a.level)}</option>`).join('');
        result.classList.toggle('hidden', !areas.length);
      }).catch(error => { if (gen === GEN) err('#plan-err', error.message); });
    };
    $('#area-search-results').onchange = event => {
      const picker = $('#area-picker');
      if (![...picker.options].some(option => option.value === event.target.value)) {
        const option = event.target.selectedOptions[0].cloneNode(true);
        picker.add(option);
      }
      picker.value = event.target.value;
    };
  }
  $('#p-sub tbody').innerHTML = P.sub_questions.map(s => `<tr class="planrow"><td><input type="checkbox" checked data-s="${esc(s.id)}"></td><td>${esc(s.text)}</td><td><span class="badge">${esc(s.source_class)}</span></td><td class="small mut">${esc(s.sources || (s.layer_ids || []).join(', '))}</td></tr>`).join('');
  if (C.shape === 'point') {
    $('#p-layers-wrap').innerHTML = `<h3>Business hypotheses (planner)</h3><table><tbody>${(P.hypotheses || []).map(h => `<tr><td><input type="checkbox" checked data-h="${esc(h.id)}"></td><td><b>${esc(h.name)}</b></td><td class="why">${esc(h.why || '')}</td></tr>`).join('')}</tbody></table>`;
  } else {
    $('#p-layers-wrap').innerHTML = `<h3>Scoring layers &amp; default weights</h3><table id="p-layers"><thead><tr><th></th><th>Layer</th><th>Dir.</th><th>Weight</th><th>Why</th></tr></thead><tbody>${P.layers.map(l => `<tr><td><input type="checkbox" ${l.enabled === false ? '' : 'checked'} data-l="${esc(l.id)}"></td><td>${esc(l.label)}</td><td>${esc(l.direction)}</td><td><input type="number" step="0.05" min="0" max="1" value="${l.weight_default}" data-w="${esc(l.id)}" style="width:64px"></td><td class="why">${esc(l.why || '')}</td></tr>`).join('')}</tbody></table>`;
  }
  $('#p-paid tbody').innerHTML = (P.paid_sources || []).length ? P.paid_sources.map(p => `<tr><td><b>${esc(p.name)}</b></td><td>${esc(p.used_for)}</td><td><span class="badge b-skip">${esc(p.status)}</span></td><td>—</td></tr>`).join('') : '<tr><td colspan=4 class="mut">none for this template</td></tr>';
  $$('#p-sub input').forEach(i => i.onchange = () => {
    const s = P.sub_questions.find(x => x.id === i.dataset.s);
    (s.layer_ids || []).forEach(l => { const x = $(`#p-layers input[data-l="${CSS.escape(l)}"]`); if (x) x.checked = i.checked; });
  });
  $('#area-change').classList.toggle('hidden', !RUN.areaChanged);
  if (P.note) $('#p-note').textContent = P.note;
  $('#p-note').classList.toggle('hidden', !P.note);
  err('#plan-err', '');
  show('s-plan');
}

$('#approve').onclick = async () => {
  const shape = RUN.classification?.shape || RUN.shape;
  const subs = $$('#p-sub input').filter(i => i.checked).map(i => i.dataset.s);
  APPROVAL = { subquestions: subs };
  if (shape === 'point') {
    APPROVAL.hypothesis_ids = $$('#p-layers-wrap input[data-h]').filter(i => i.checked).map(i => i.dataset.h);
    if (!APPROVAL.hypothesis_ids.length) return err('#plan-err', 'Keep at least one business hypothesis.');
  } else {
    APPROVAL.area_id = $('#area-picker')?.value || RUN.plan.area?.area_id;
    APPROVAL.layers = $$('#p-layers input[data-l]').map(input => ({
      id: input.dataset.l,
      enabled: input.checked,
      weight: Number($(`#p-layers input[data-w="${CSS.escape(input.dataset.l)}"]`).value)
    }));
    if (!APPROVAL.layers.some(layer => layer.enabled)) return err('#plan-err', 'Keep at least one scoring layer.');
  }
  const gen = GEN;
  try {
    const response = await post(`/runs/${encodeURIComponent(RUN.run_id)}/approve`, APPROVAL);
    if (gen !== GEN) return;
    if (response.status === 'awaiting_approval') {
      RUN.plan = response.plan;
      RUN.areaChanged = true;
      renderPlan();
      return;
    }
  } catch (e) { if (gen === GEN) return err('#plan-err', 'Approve failed: ' + e.message); return; }
  live();
};

/* ---------- 3 · live ---------- */
function stopLive() {
  GEN++;
  clearTimeout(timer); timer = null;
  clearTimeout(doneT); doneT = null;
  M3.pending = null;
  if (STREAM_CTRL) { STREAM_CTRL.abort(); STREAM_CTRL = null; }
  if (EXPORT_CTRL) { EXPORT_CTRL.abort(); EXPORT_CTRL = null; }
  if (EXPORT_WINDOW && !EXPORT_WINDOW.closed) EXPORT_WINDOW.close();
  EXPORT_WINDOW = null;
  $$('[data-export]').forEach(button => { button.disabled = false; });
  err('#export-err', '');
}
function eventStage(event) {
  if (['classify', 'clarify', 'area', 'plan'].includes(event.type)) return 'plan';
  if (event.type === 'approved') return 'approve';
  if (['layer', 'score'].includes(event.type)) return 'data';
  if (['search', 'extract', 'finding_accepted', 'finding_rejected', 'model_call', 'tool_error', 'budget_exhausted'].includes(event.type)) return 'research';
  if (['merge', 'skeptic_risk', 'review', 'rescore'].includes(event.type)) return 'skeptic';
  if (event.type === 'fact_check') return 'fact-check';
  if (event.type === 'write') return 'write';
  if (event.type === 'metrics' || (event.type === 'status' && event.detail?.status === 'done')) return 'done';
  return event.type === 'error' ? 'research' : 'plan';
}
function live() {
  show('s-live'); stopLive(); const gen = GEN;
  $('#l-trace').innerHTML = ''; err('#live-err', '');
  $('#stages').innerHTML = STAGES.map(s => `<span data-s="${s}">${s}</span>`).join('');
  const seen = new Set(); let tok = 0, sec = 0;
  const add = ev => {
    const k = ev.seq ?? JSON.stringify(ev); if (seen.has(k)) return; seen.add(k);
    tok += (ev.tokens_in || 0) + (ev.tokens_out || 0); sec += +ev.seconds || 0;
    $('#l-trace').insertAdjacentHTML('beforeend', evRow(ev, `+${sec.toFixed(1)}s`));
    $('#l-trace').scrollTop = $('#l-trace').scrollHeight;
    const stage = eventStage(ev), idx = STAGES.indexOf(stage);
    $$('#stages span').forEach((s, i) => s.className = i < idx ? 'done' : i === idx ? 'on' : '');
    $('#l-stat').textContent = `${seen.size} events · ${tok.toLocaleString()} tokens · ${sec.toFixed(1)} s`;
    $('#bar').style.width = `${Math.min(95, seen.size * 5)}%`;
    if (ev.type === 'error') err('#live-err', JSON.stringify(ev.detail));
  };
  let finished = false;
  const finish = () => {
    if (finished || gen !== GEN) return;
    finished = true;
    if (STREAM_CTRL) { STREAM_CTRL.abort(); STREAM_CTRL = null; }
    clearTimeout(timer);
    $('#bar').style.width = '100%';
    doneT = setTimeout(() => { if (gen === GEN) loadResult(gen); }, 200);
  };
  $('#skip').onclick = () => { if (gen === GEN) loadResult(gen); };
  const runId = encodeURIComponent(RUN.run_id);
  let lastSeq = -1;
  const poll = () => {
    if (gen !== GEN) return;
    $('#l-src').textContent = '· polling /result';
    getJSON(`/runs/${runId}/result`).then(result => {
      if (gen !== GEN) return;
      if (result.status === 'done') return finish();
      timer = setTimeout(poll, 1200);
    }).catch(error => { if (gen === GEN) err('#live-err', error.message); });
  };
  const reconnect = async attempt => {
    if (gen !== GEN || finished) return;
    const controller = new AbortController(); STREAM_CTRL = controller;
    try {
      $('#l-src').textContent = '· streaming /events';
      const response = await fetch(apiUrl(`/runs/${runId}/events`), {
        headers: headers(lastSeq >= 0 ? { 'Last-Event-ID': String(lastSeq) } : {}),
        signal: controller.signal
      });
      if (!response.ok) throw new Error(`SSE HTTP ${response.status}`);
      const reader = response.body.getReader(), decoder = new TextDecoder();
      let buffer = '';
      while (gen === GEN) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let boundary;
        while ((boundary = buffer.indexOf('\n\n')) >= 0) {
          const block = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
          let id = null, data = '';
          block.split(/\r?\n/).forEach(line => {
            if (line.startsWith('id:')) id = line.slice(3).trim();
            if (line.startsWith('data:')) data += line.slice(5).trim();
          });
          if (data) {
            const event = JSON.parse(data);
            if (id !== null) lastSeq = Math.max(lastSeq, Number(id));
            add(event);
          }
        }
      }
      if (gen === GEN && !finished) finish();
    } catch (error) {
      if (gen !== GEN || error.name === 'AbortError') return;
      if (attempt < 3) {
        timer = setTimeout(() => reconnect(attempt + 1), 250);
      } else {
        poll();
      }
    }
  };
  reconnect(1);
}
function evRow(ev, t) {
  const cls = /reject|error/.test(ev.type) ? 'rej' : /rescore/.test(ev.type) ? 're' : '';
  const detail = typeof ev.detail === 'string' ? ev.detail : JSON.stringify(ev.detail || {});
  const tokens = (ev.tokens_in || 0) + (ev.tokens_out || 0);
  return `<div class="ev ${cls}"><span class="w">${esc(t)}</span><span class="role">${esc(ev.agent)}</span><span>${esc(ev.type)} · ${esc(detail)}${ev.why ? `<br><span class="w">why: ${esc(ev.why)}</span>` : ''}</span><span class="meta">${esc(ev.model || '')}${tokens ? ' · ' + tokens + ' tok' : ''} · ${(+ev.seconds || 0).toFixed(1)}s</span></div>`;
}

/* ---------- 4 · result ---------- */
async function loadResult(g = GEN) {
  let res;
  try { res = await getJSON(`/runs/${encodeURIComponent(RUN.run_id)}/result`); }
  catch (e) { if (g === GEN) err('#live-err', 'Could not load result: ' + e.message); return; }
  if (g !== GEN) return;
  if (res.status === 'failed') return err('#live-err', 'The run failed' + (res.error || res.report_md ? ': ' + (res.error || res.report_md) : '.'));
  if (res.status === 'running' || res.status === 'approved' || res.status === 'researching') {
    doneT = setTimeout(() => { if (g === GEN) loadResult(g); }, 1200);
    return err('#live-err', 'Research is still in progress…');
  }
  R = res; dash();
}

function dash() {
  show('s-dash');
  CITE = {}; (R.evidence || []).forEach((e, index) => { CITE[e.evidence_id] = index + 1; });
  $('#fixture-notice').textContent = R.fixture_notice || '';
  $('#fixture-notice').classList.toggle('hidden', !R.fixture_notice);
  $('#d-q').textContent = R.question || RUN.question;
  const shape = R.shape || RUN.classification?.shape;
  $('#d-shape').innerHTML = `<span class="badge b-${esc(shape)}">${esc(shape)}</span>`;
  $('#d-role').textContent = R.role ? 'Role: ' + R.role : '';
  const subs = (R.plan || RUN.plan || {}).sub_questions || [], ok = new Set(APPROVAL?.subquestions || subs.map(s => s.id));
  $('#d-plan').innerHTML = `<table><tbody>${subs.map(s => `<tr style="opacity:${ok.has(s.id) ? 1 : .45}"><td>${ok.has(s.id) ? '✓' : '✕ not approved'}</td><td>${esc(s.text)}</td><td><span class="badge">${esc(s.source_class)}</span></td></tr>`).join('')}</tbody></table>`;
  $('#d-note').textContent = R.plan?.note || ''; $('#d-note').classList.toggle('hidden', !R.plan?.note);
  $('#d-sum').innerHTML = renderMarkdown(R.report_md || '');
  renderSources(); renderTrace(); renderGrade();
  if (R.candidates) { $('#layer-card').classList.add('hidden'); $('#d-mix').textContent = 'point · LocalLens'; renderPoint(); }
  else if (R.cells) {
    $('#layer-card').classList.remove('hidden'); $('#d-mix').textContent = 'written for the default layer mix';
    R.defaultOn = {}; R.defaultW = {};
    R.cells.layers.forEach(l => { R.defaultOn[l.id] = l.enabled !== false; R.defaultW[l.id] = l.weight_default; });
    state = { on: { ...R.defaultOn }, w: { ...R.defaultW }, mode: R.cells.mode_default || 'present' };
    R.defaultRank = SpotWiseScoring.scoreCells(R.cells, R.defaultOn, R.defaultW, 'present');
    renderLayers(); renderArea();
  } else if ((R.findings || []).length) {
    $('#layer-card').classList.add('hidden'); $('#battle-card').classList.add('hidden');
    $('#d-ftitle').textContent = 'Research findings'; $('#d-mix').textContent = 'Live research';
    const liveFindings = R.findings || [];
    const verifiedFindings = liveFindings.filter(f => f.status === 'accepted' && ['verified', 'partial'].includes(f.verification));
    const droppedFindings = liveFindings.filter(f => f.status !== 'accepted' || !['verified', 'partial'].includes(f.verification));
    const verifiedHtml = verifiedFindings.length ? `<h3>Research findings</h3>${verifiedFindings.map(f => `<div class="src finding" id="finding-${esc(f.finding_id)}"><div><span class="finding-id">${esc(f.finding_id)}</span><div><b>${esc(f.claim || f.statement || '')}</b><div class="quote small">“${esc(f.quote || '')}”</div><span class="badge">${esc(f.status)} · ${esc(f.verification)}</span> ${(f.evidence_ids || []).map(cite).join(' ')}</div></div></div>`).join('')}` : '';
    const droppedHtml = droppedFindings.length ? `<h3 class="mut">Dropped / unverified</h3><ul class="small mut">${droppedFindings.map(f => `<li>${esc(f.claim || f.statement || '')} <span class="badge">${esc(f.status || 'unknown')} · ${esc(f.verification || 'unverified')}</span></li>`).join('')}</ul>` : '';
    $('#d-find').innerHTML = verifiedHtml + droppedHtml;
  } else { $('#d-find').innerHTML = '<p class="mut">The run returned no cells or candidates.</p>'; }
  bindCites();
}

const cite = id => id && CITE[id] ? `<a class="cite" data-e="${esc(id)}">[${CITE[id]}]</a>` : (id ? `<span class="small mut" title="not in ledger">[?]</span>` : '');
function renderInline(text) {
  return esc(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\[(\d+)\]/g, (match, n) => {
      const evidence = (R.evidence || [])[Number(n) - 1];
      return evidence ? cite(evidence.evidence_id) : match;
    })
    .replace(/\[(e[\w.-]*)\]/g, (match, id) => CITE[id] ? cite(id) : '');
}
function renderMarkdown(markdown) {
  const blocks = [];
  let paragraph = [], list = [];
  const flushParagraph = () => {
    if (paragraph.length) blocks.push(`<p>${paragraph.map(renderInline).join(' ')}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (list.length) blocks.push(`<ul>${list.map(line => `<li>${renderInline(line)}</li>`).join('')}</ul>`);
    list = [];
  };
  String(markdown).split(/\r?\n/).forEach(line => {
    if (/^\s*[-*]\s+/.test(line)) {
      flushParagraph(); list.push(line.replace(/^\s*[-*]\s+/, ''));
    } else if (!line.trim()) {
      flushParagraph(); flushList();
    } else {
      flushList(); paragraph.push(line.trim());
    }
  });
  flushParagraph(); flushList();
  return blocks.join('');
}
const fmt = v => v === null || v === undefined ? '<span class="null">missing</span>' : (+v).toFixed(2);
const evOf = id => (R.evidence || []).find(e => e.evidence_id === id);
function color(t) { const st = [[255, 247, 214], [180, 226, 193], [65, 174, 160], [18, 104, 121], [12, 52, 86]]; t = Math.max(0, Math.min(.9999, t)) * 4; const i = Math.floor(t), f = t - i, a = st[i], b = st[i + 1]; return `rgb(${a.map((v, k) => Math.round(v + (b[k] - v) * f)).join(',')})`; }
function bindCites() { $$('.cite').forEach(a => a.onclick = ev => { ev.preventDefault(); const el = document.getElementById('src-' + a.dataset.e); if (!el) return; el.scrollIntoView({ behavior: 'smooth', block: 'center' }); el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); }); }

/* ---------- area (T2/T3) ---------- */
function renderLayers() {
  const L = R.cells.layers, hasFut = R.cells.cells.some(c => (c.modifiers || []).length);
  $('#layers').innerHTML = L.map(l => `<div class="layer"><input type="checkbox" id="ly-${esc(l.id)}" ${state.on[l.id] ? 'checked' : ''}><label for="ly-${esc(l.id)}">${esc(l.label)} ${cite(l.evidence_id)}</label><span class="small mut">w ${state.w[l.id].toFixed(2)}</span><span class="small mut">${l.direction === '+' ? '↑ good' : '↓ good'}</span></div>`).join('');
  L.forEach(l => $('#ly-' + CSS.escape(l.id)).onchange = e => { state.on[l.id] = e.target.checked; renderArea(); });
  $$('#mode button').forEach(b => {
    b.classList.toggle('on', b.dataset.m === state.mode);
    b.disabled = b.dataset.m === 'future' && !hasFut; b.title = b.disabled ? 'No future developments found' : '';
    b.onclick = () => { state.mode = b.dataset.m; $$('#mode button').forEach(x => x.classList.toggle('on', x === b)); renderArea(); };
  });
  $('#reset').onclick = () => { state = { on: { ...R.defaultOn }, w: { ...R.defaultW }, mode: 'present' }; renderLayers(); renderArea(); };
  bindCites();
}
function layerVal(l, c) {
  if (c.values[l.id] === null || c.values[l.id] === undefined) return '<span class="small mut">no data</span>';
  const raw = c.raw_values?.[l.id];
  return `${fmt(c.values[l.id])}${raw !== undefined ? ` <span class="small mut">(${raw === null ? 'missing' : esc(raw) + (l.unit ? ' ' + esc(l.unit) : '')})</span>` : ''}`;
}
function renderArea() {
  const L = R.cells.layers;
  if (!L.some(l => state.on[l.id])) {
    $('#d-find').innerHTML = '<p class="mut">Tick at least one layer.</p>';
    $('#battle-card').classList.add('hidden'); $('#legend').innerHTML = '';
    if (map) { const v = map._loaded ? [map.getCenter(), map.getZoom()] : null; resetMap(); if (v) map.setView(...v); }
    clear3D();
    return;
  }
  const ranked = SpotWiseScoring.scoreCells(R.cells, state.on, state.w, state.mode), byId = {};
  ranked.forEach((r, i) => byId[r.cell.id] = { ...r, rank: i + 1 });
  const defRank = {}; R.defaultRank.forEach((r, i) => defRank[r.cell.id] = i + 1);
  $('#d-ftitle').textContent = R.shape === 'gap' ? 'Top-3 cells by gap score' : 'Top-3 ranked cells';
  $('#d-find').innerHTML = ranked.slice(0, 3).map((r, i) => {
    const c = r.cell, mv = defRank[c.id] - (i + 1);
    return `<div class="top" data-id="${esc(c.id)}"><div class="rk">#${i + 1}</div><div class="row" style="gap:8px"><b style="font-size:15px">${esc(c.name)}</b><span class="small mut">PLR ${esc(c.id)}${c.parent_name ? ' · ' + esc(c.parent_name) : ''}</span>${mv ? `<span class="mv ${mv > 0 ? 'up' : 'dn'}">${mv > 0 ? '▲' : '▼'} ${Math.abs(mv)} vs default</span>` : ''}</div>
      <div><span class="sc">${r.score.toFixed(2)}</span> <span class="small mut">score (${state.mode === 'future' ? 'present + future' : 'present'})</span></div>
      <div class="vals">${L.map(l => `<div style="opacity:${state.on[l.id] ? 1 : .35}">${esc(l.label)}: <b>${layerVal(l, c)}</b> ${cite(l.evidence_id)}<div class="mini"><div style="width:${(c.values[l.id] ?? 0) * 100}%"></div></div></div>`).join('')}</div>
      ${(c.modifiers || []).map(m => `<div class="mod"><b>Future:</b> ${esc(m.label)} ${cite(m.evidence_id)} <span class="badge">${state.mode === 'future' ? `+${(m.bonus ?? .1).toFixed(2)} applied` : `+${(m.bonus ?? .1).toFixed(2)} in future mode`}</span><br><span class="quote">“${esc(m.quote)}”</span>${m.magnitude ? ` <span class="small mut">· magnitude stated by source: ${esc(Object.entries(m.magnitude).map(([k, v]) => v + ' ' + k).join(', '))}</span>` : ''}</div>`).join('')}
      ${(c.risks || []).map(k => `<div class="risk"><b>Risk (${esc(k.severity)}):</b> ${esc(k.text)} ${cite(k.evidence_id)}${(c.demoted_by || []).map(id => `<a class="badge risk-link" href="#finding-${esc(id)}">risk · ${esc(id)}</a>`).join('')}${k.quote ? `<br><span class="quote">“${esc(k.quote)}”</span>` : ''}</div>`).join('')}
      ${(c.unknowns || []).map(u => `<div class="unk"><b>Unknown:</b> ${esc(u)}</div>`).join('')}</div>`;
  }).join('') + (() => {
    const demoted = ranked.filter(item => (item.cell.demoted_by || []).length && !ranked.slice(0, 3).includes(item));
    return demoted.length ? `<h3>Risk-adjusted cells</h3>${demoted.map(item => `<div class="src"><div class="num">#${byId[item.cell.id].rank}</div><div><b>${esc(item.cell.name)}</b> · score ${item.score.toFixed(2)} ${item.cell.demoted_by.map(id => `<a class="badge risk-link" href="#finding-${esc(id)}">risk · ${esc(id)}</a>`).join(' ')}<div>${(item.cell.risks || []).map(risk => esc(risk.text)).join(' · ')}</div></div></div>`).join('')}` : '';
  })() + (() => {
    const findingRows = (R.findings || []).map(f => `<div class="src finding" id="finding-${esc(f.finding_id)}"><div><span class="finding-id">${esc(f.finding_id)}</span><div><b>${esc(f.claim)}</b><div class="quote small">“${esc(f.quote)}”</div><span class="badge">${esc(f.status)} · ${esc(f.verification || 'unverified')}</span> ${(f.evidence_ids || []).map(cite).join(' ')}</div></div></div>`).join('');
    return findingRows ? `<h3>Research findings</h3>${findingRows}` : '';
  })() + (() => { const w = ranked.find(r => (r.cell.risks || []).length); return w && !ranked.slice(0, 3).includes(w) ? `<p class="small mut">Also considered: <b>${esc(w.cell.name)}</b> (#${byId[w.cell.id].rank}): ${esc(w.cell.risks[0].text)} ${cite(w.cell.risks[0].evidence_id)}.</p>` : ''; })();
  const [a, b] = ranked;
  if (a && b) {
    $('#battle-card').classList.remove('hidden');
    $('#battle-title').innerHTML = `Location Battle: <span style="color:var(--acc)">${esc(a.cell.name)}</span> vs ${esc(b.cell.name)}`;
    const adj = (l, c) => { const v = c.values[l.id]; return v === null || v === undefined ? null : l.direction === '+' ? v : 1 - v; };
    $('#d-battle').innerHTML = `<div class="battle">${L.filter(l => state.on[l.id]).map(l => {
      const va = adj(l, a.cell), vb = adj(l, b.cell), bar = (v, win) => v === null ? '<span class="null">missing</span>' : `<span class="bar" style="width:${v * 100}px;background:${win ? '#0f766e' : '#94a3b8'}"></span>`;
      const aw = va !== null && (vb === null || va > vb), bw = vb !== null && (va === null || vb > va);
      return `<div class="l">${aw ? '<b>✓</b> ' : ''}${bar(va, va !== null && (vb === null || va >= vb))}</div><div class="lab">${esc(l.label)}</div><div>${bar(vb, bw)}${bw ? ' <b>✓</b>' : ''}</div>`;
    }).join('')}<div class="l"><b>${a.score.toFixed(2)}</b></div><div class="lab"><b>score</b></div><div><b>${b.score.toFixed(2)}</b></div></div><p class="small mut" style="margin:8px 0 0">Bars show each checked layer direction-adjusted (longer = better).</p>`;
  }
  drawAreaMap(ranked, byId); bindCites();
}
function resetMap() { if (map) { map.remove(); map = null; } map = L.map('map', { scrollWheelZoom: false }); L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap contributors' }).addTo(map); return map; }
function drawAreaMap(ranked, byId) {
  const gj = R.cells_geojson;
  $('#d-maparea').textContent = `${R.cells.area.name} · ${R.cells.cells.length} PLR cells · heatmap of the ${R.shape === 'gap' ? 'gap' : 'composite'} score`;
  if (!gj) { show3D(false); $('#map').innerHTML = '<p class="mut" style="padding:12px">No cell geometry (cells.geojson) in this result.</p>'; return; }
  $('#map-ctl').classList.remove('hidden');
  if (use3D()) { show3D(true); paint3D(ranked, byId); return; }
  show3D(false);
  const prevView = map && map._loaded ? [map.getCenter(), map.getZoom()] : null;
  resetMap();
  const top3 = ranked.slice(0, 3).map(r => r.cell.id);
  const fid = f => String(f.properties?.id ?? f.id);
  const layer = L.geoJSON(gj, {
    filter: f => byId[fid(f)],
    style: f => ({ fillColor: color(byId[fid(f)].score), fillOpacity: .78, color: top3.includes(fid(f)) ? '#0b1720' : '#fff', weight: top3.includes(fid(f)) ? 2.5 : 1 }),
    onEachFeature: (f, lyr) => {
      const r = byId[fid(f)], c = r.cell;
      lyr.bindTooltip(tipHTML(r), { sticky: true, className: 'rtip' });
      lyr.bindPopup(() => srcHTML(c));
      lyr.on('click', () => flashTop(c.id));
      const center = lyr.getBounds().getCenter();
      const i = top3.indexOf(c.id), star = (c.modifiers || []).length ? (state.mode === 'future' ? '★' : '☆') : '';
      if (i >= 0 || star) L.marker(center, { interactive: false, icon: L.divIcon({ className: 'lbl', html: `${i >= 0 ? '#' + (i + 1) : ''}${star}`, iconSize: [40, 16], iconAnchor: [20, 8] }) }).addTo(map);
    }
  }).addTo(map);
  if (prevView) map.setView(...prevView); else map.fitBounds(layer.getBounds(), { padding: [10, 10] });
  $$('.top').forEach(t => {
    t.onmouseenter = () => layer.eachLayer(l => { if (fid(l.feature) === t.dataset.id) l.setStyle({ weight: 4, color: '#dc2626' }); });
    t.onmouseleave = () => layer.resetStyle();
  });
  $('#legend').innerHTML = legendHTML(false);
}

const tipHTML = r => {
  const c = r.cell;
  return `<b>${esc(c.name)}</b> <span style="color:#8fa5b5">#${r.rank} · ${esc(c.id)}</span><br>score <b>${r.score.toFixed(2)}</b><table>${R.cells.layers.map(l => `<tr style="opacity:${state.on[l.id] ? 1 : .45}"><td>${esc(l.label)}</td><td><b>${layerVal(l, c)}</b></td><td>[${CITE[l.evidence_id] || '?'}]</td></tr>`).join('')}</table>${(c.modifiers || []).map(m => `★ ${esc(m.label)} [${CITE[m.evidence_id] || '?'}]`).join('<br>')}${(c.risks || []).map(k => `<br>⚠ ${esc(k.text)} [${CITE[k.evidence_id] || '?'}]`).join('')}<div style="color:#8fa5b5;margin-top:4px">click for sources</div>`;
};
const srcHTML = c => `<b>${esc(c.name)}</b><br>${[...new Set([...R.cells.layers.map(l => l.evidence_id), ...(c.modifiers || []).map(m => m.evidence_id), ...(c.risks || []).map(k => k.evidence_id)])].map(id => { const e = evOf(id); return e ? `<div class="small">[${CITE[id]}] ${link(e.url, e.title || e.url)}</div>` : ''; }).join('')}`;
function flashTop(id) { const el = document.querySelector(`.top[data-id="${CSS.escape(id)}"]`); if (el) { el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); } }
const legendHTML = is3D => `<span>low</span><span class="bar" style="background:linear-gradient(90deg,${[0, .25, .5, .75, 1].map(color).join(',')})"></span><span>high</span><span style="margin-left:10px">☆/★ future development (★ applied)</span>${is3D ? `<span style="margin-left:10px" id="lg-z"></span>` : ''}<span style="margin-left:auto" class="small">Boundaries: LOR 2021 PLR, Amt für Statistik Berlin-Brandenburg, CC BY 3.0</span>`;

/* ---------- 3D map: MapLibre GL + OpenFreeMap vector tiles + Berlin TrueDOP 2024 aerial; Leaflet 2D is the fallback ---------- */
const M3 = { map: null, loaded: false, failed: false, gj: null, cells: [], bCell: new Map(), mk: [], hover: null, hl: null, pending: null, bb: null, tip: null };
let MAP_VIEW = '3d';
const STYLE3D = 'https://tiles.openfreemap.org/styles/liberty', NEUT3D = 'hsl(35,8%,88%)';
const ORTHO = 'https://gdi.berlin.de/services/wms/truedop_2024?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=truedop_2024&STYLES=&CRS=EPSG:3857&BBOX={bbox-epsg-3857}&WIDTH=256&HEIGHT=256&FORMAT=image/jpeg';
const use3D = () => MAP_VIEW === '3d' && !!window.maplibregl && !M3.failed;
function show3D(on) {
  $('#map').classList.toggle('hidden', on); $('#map3d').classList.toggle('hidden', !on);
  $('#map-base').classList.toggle('hidden', !on); $('#map-recenter').classList.toggle('hidden', !on);
  $('#map-view').classList.toggle('hidden', !window.maplibregl || M3.failed);
  $$('#map-view button').forEach(b => b.classList.toggle('on', b.dataset.v === (on ? '3d' : '2d')));
}
$$('#map-view button').forEach(b => b.onclick = () => { MAP_VIEW = b.dataset.v; if (R?.cells) renderArea(); });
$$('#map-base button').forEach(b => b.onclick = () => {
  $$('#map-base button').forEach(x => x.classList.toggle('on', x === b));
  if (M3.loaded) baseStyle3D();
});
$('#map-recenter').onclick = () => fit3D(900);
function fail3D(msg) {
  if (M3.failed) return;
  M3.failed = true; M3.loaded = false;
  try { M3.map && M3.map.remove(); } catch (_) { /* already gone */ }
  M3.map = null;
  $('#map-note').textContent = `3D view unavailable (${msg}); showing the 2D map.`;
  if (R?.cells && !$('#s-dash').classList.contains('hidden')) renderArea();
}
function init3D() {
  try {
    M3.map = new maplibregl.Map({ container: 'map3d', style: STYLE3D, center: [13.41, 52.495], zoom: 13, pitch: 55, bearing: -12, maxPitch: 70, cooperativeGestures: true, attributionControl: { compact: true } });
  } catch (_) { fail3D('WebGL not available'); return; }
  const map = M3.map, t = setTimeout(() => { if (!M3.loaded) fail3D('map tiles did not load'); }, 15000);
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'bottom-right');
  map.on('error', () => { if (!M3.loaded) { clearTimeout(t); fail3D('map style did not load'); } });
  map.on('load', () => {
    clearTimeout(t);
    M3.loaded = true;
    setup3D();
    if (M3.pending && M3.pending[0] === GEN && R?.cells && !R.candidates) {
      paint3D(M3.pending[1], M3.pending[2]);
    }
  });
  new ResizeObserver(() => M3.map && M3.map.resize()).observe($('#map3d'));
}
function setup3D() {
  const map = M3.map, layers = map.getStyle().layers, bl = layers.find(l => l.type === 'fill-extrusion');
  M3.blayer = bl?.id; M3.bsrc = bl?.source;
  const below = (layers.find(l => l.id === 'building') || bl || {}).id;
  layers.filter(l => /^poi/.test(l.id)).forEach(l => map.setLayoutProperty(l.id, 'visibility', 'none'));
  if (M3.blayer) { map.setLayerZoomRange(M3.blayer, 13, 24); map.setPaintProperty(M3.blayer, 'fill-extrusion-opacity', ['interpolate', ['linear'], ['zoom'], 13, .55, 14, .92]); }
  map.addSource('ortho', { type: 'raster', tiles: [ORTHO], tileSize: 256, attribution: 'TrueDOP 2024 © Geoportal Berlin, dl-de/zero-2-0' });
  map.addLayer({ id: 'ortho', type: 'raster', source: 'ortho', layout: { visibility: $('#map-base .on')?.dataset.b === 'sat' ? 'visible' : 'none' } }, below);
  map.addSource('cells', { type: 'geojson', data: { type: 'FeatureCollection', features: [] }, promoteId: 'id', attribution: 'PLR: Amt für Statistik Berlin-Brandenburg, CC BY 3.0' });
  const hov = ['boolean', ['feature-state', 'hover'], false], hl = ['boolean', ['feature-state', 'hl'], false];
  map.addLayer({ id: 'cells-fill', type: 'fill', source: 'cells', paint: { 'fill-color': NEUT3D, 'fill-opacity': ['case', hov, .82, hl, .8, .6] } }, below);
  map.addLayer({ id: 'cells-line', type: 'line', source: 'cells', paint: { 'line-color': '#fff', 'line-width': 1.4, 'line-opacity': .9 } }, below);
  map.addLayer({ id: 'cells-hl', type: 'line', source: 'cells', paint: { 'line-color': '#dc2626', 'line-width': ['case', hl, 4, hov, 2.5, 0] } });
  if (M3.bsrc) map.addLayer({ id: 'focus-poi', type: 'circle', source: M3.bsrc, 'source-layer': 'poi', filter: poiFilter3D(), minzoom: 13, paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 13, 2.2, 17, 5], 'circle-color': '#b45309', 'circle-stroke-color': '#fff', 'circle-stroke-width': 1 } });
  M3.tip = new maplibregl.Popup({ closeButton: false, closeOnClick: false, className: 'rtip3', offset: 14, maxWidth: '320px' });
  map.on('mousemove', 'cells-fill', e => {
    const f = e.features[0], r = f && M3.byId?.[f.properties.id];
    if (!r) return;
    hover3D(f.properties.id);
    map.getCanvas().style.cursor = 'pointer';
    M3.tip.setLngLat(e.lngLat).setHTML(tipHTML(r)).addTo(map);
  });
  map.on('mouseleave', 'cells-fill', () => { hover3D(null); map.getCanvas().style.cursor = ''; M3.tip.remove(); });
  map.on('click', 'cells-fill', e => {
    const f = e.features[0], r = f && M3.byId?.[f.properties.id];
    if (!r) return;
    M3.tip.remove(); flashTop(r.cell.id);
    new maplibregl.Popup({ maxWidth: '300px' }).setLngLat(e.lngLat).setHTML(srcHTML(r.cell)).addTo(map);
  });
  map.on('idle', assignBuildings3D);
  map.on('zoomend', zoomHint3D);
}
function baseStyle3D() {
  const sat = $('#map-base .on')?.dataset.b === 'sat', hov = ['boolean', ['feature-state', 'hover'], false], hl = ['boolean', ['feature-state', 'hl'], false];
  M3.map.setLayoutProperty('ortho', 'visibility', sat ? 'visible' : 'none');
  M3.map.setPaintProperty('cells-fill', 'fill-opacity', ['case', hov, sat ? .7 : .82, hl, .7, sat ? .38 : .6]);
}
const poiFilter3D = () => ['==', ['get', 'subclass'], R?.shape === 'gap' ? 'kindergarten' : 'cafe'];
function hover3D(id) {
  if (M3.hover === id || !M3.map) return;
  if (M3.hover) M3.map.setFeatureState({ source: 'cells', id: M3.hover }, { hover: false });
  M3.hover = id;
  if (id) M3.map.setFeatureState({ source: 'cells', id }, { hover: true });
}
function highlight3D(id) {
  if (!M3.loaded) return;
  if (M3.hl) M3.map.setFeatureState({ source: 'cells', id: M3.hl }, { hl: false });
  M3.hl = id;
  if (id) M3.map.setFeatureState({ source: 'cells', id }, { hl: true });
}
function ringBB(g) {
  let x0 = 180, y0 = 90, x1 = -180, y1 = -90;
  const rings = g.type === 'Polygon' ? [g.coordinates[0]] : g.coordinates.map(p => p[0]);
  rings.forEach(r => r.forEach(([x, y]) => { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }));
  return [x0, y0, x1, y1];
}
function inRing(r, x, y) { let k = false; for (let i = 0, j = r.length - 1; i < r.length; j = i++) { const [xi, yi] = r[i], [xj, yj] = r[j]; if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) k = !k; } return k; }
function cellAt(x, y) {
  for (const c of M3.cells) {
    const b = c.bb;
    if (x < b[0] || x > b[2] || y < b[1] || y > b[3]) continue;
    const polys = c.g.type === 'Polygon' ? [c.g.coordinates] : c.g.coordinates;
    if (polys.some(p => inRing(p[0], x, y))) return c.i;
  }
  return -1;
}
function labelPoint(c) {
  const ring = c.g.type === 'Polygon' ? c.g.coordinates[0] : c.g.coordinates[0][0];
  let x = 0, y = 0; ring.forEach(p => { x += p[0]; y += p[1]; });
  const p = [x / ring.length, y / ring.length];
  return cellAt(...p) === c.i ? p : [(c.bb[0] + c.bb[2]) / 2, (c.bb[1] + c.bb[3]) / 2];
}
function assignBuildings3D() {
  if (!M3.cells.length || !M3.map || !M3.bsrc) return;
  for (const f of M3.map.querySourceFeatures(M3.bsrc, { sourceLayer: 'building' })) {
    if (f.id == null || M3.bCell.has(f.id)) continue;
    const g = f.geometry, r = g.type === 'Polygon' ? g.coordinates[0] : g.type === 'MultiPolygon' ? g.coordinates[0][0] : null;
    if (!r) continue;
    let x = 0, y = 0; r.forEach(p => { x += p[0]; y += p[1]; });
    const i = cellAt(x / r.length, y / r.length);
    if (i < 0) continue;
    M3.bCell.set(f.id, i);
    M3.map.setFeatureState({ source: M3.bsrc, sourceLayer: 'building', id: f.id }, { i });
  }
}
function zoomHint3D() { const z = $('#lg-z'); if (M3.map) $('#map3d').classList.toggle('far', M3.map.getZoom() < 14); if (z && M3.map) z.textContent = M3.map.getZoom() >= 14 ? 'buildings tinted by their PLR cell' : 'zoom in to tint buildings'; }
function fit3D(ms) {
  if (!M3.map || !M3.bb) return;
  M3.map.fitBounds([[M3.bb[0], M3.bb[1]], [M3.bb[2], M3.bb[3]]], { padding: 30, pitch: 55, bearing: -12, duration: ms });
}
function setCells3D(gj) {
  const map = M3.map, fid = f => String(f.properties?.id ?? f.id);
  M3.cells = gj.features.filter(f => f.geometry && /Polygon$/.test(f.geometry.type)).map((f, i) => ({ id: fid(f), i, g: f.geometry, bb: ringBB(f.geometry) }));
  M3.cells.forEach(c => { c.p = labelPoint(c); });
  map.getSource('cells').setData({ type: 'FeatureCollection', features: M3.cells.map(c => ({ type: 'Feature', properties: { id: c.id, i: c.i }, geometry: c.g })) });
  M3.bCell.clear();
  if (M3.bsrc) map.removeFeatureState({ source: M3.bsrc, sourceLayer: 'building' });
  if (map.getLayer('focus-poi')) map.setFilter('focus-poi', poiFilter3D());
  M3.bb = M3.cells.reduce((a, c) => [Math.min(a[0], c.bb[0]), Math.min(a[1], c.bb[1]), Math.max(a[2], c.bb[2]), Math.max(a[3], c.bb[3])], [180, 90, -180, -90]);
  M3.hover = null; M3.hl = null; M3.gj = gj;
  fit3D(1500);
}
function paint3D(ranked, byId) {
  M3.pending = [GEN, ranked, byId];
  if (!M3.map) { init3D(); return; }
  M3.map.resize();
  if (!M3.loaded) return;
  const map = M3.map;
  if (M3.gj !== R.cells_geojson) setCells3D(R.cells_geojson);
  M3.byId = byId;
  if (!M3.cells.length) return;
  const cols = M3.cells.map(c => byId[c.id] ? color(byId[c.id].score) : NEUT3D), ex = inp => ['match', inp, ...cols.flatMap((c, i) => [i, c]), NEUT3D];
  map.setPaintProperty('cells-fill', 'fill-color', ex(['get', 'i']));
  if (M3.blayer) map.setPaintProperty(M3.blayer, 'fill-extrusion-color', ex(['to-number', ['coalesce', ['feature-state', 'i'], -1]]));
  markers3D(ranked, byId);
  $('#legend').innerHTML = legendHTML(true); zoomHint3D();
  $$('.top').forEach(t => { t.onmouseenter = () => highlight3D(t.dataset.id); t.onmouseleave = () => highlight3D(null); });
}
function markers3D(ranked, byId) {
  M3.mk.forEach(m => m.remove()); M3.mk = [];
  const top3 = ranked.slice(0, 3).map(r => r.cell.id);
  M3.cells.forEach(c => {
    const r = byId[c.id];
    if (!r) return;
    const cell = r.cell, k = top3.indexOf(c.id), b = [];
    if (k >= 0) b.push(`<span class="bd rk" title="Rank ${k + 1}">${k + 1}</span><span class="bd nm">${esc(cell.name)} · ${r.score.toFixed(2)}</span>`);
    (cell.modifiers || []).forEach(m => b.push(`<span class="bd fu${state.mode === 'future' ? ' on' : ''}" title="${esc(m.label)}">${state.mode === 'future' ? '★' : '☆'}</span>`));
    (cell.risks || []).forEach(x => b.push(`<span class="bd rs" title="${esc(x.text)}">⚠</span>`));
    if (!b.length) return;
    const el = document.createElement('div');
    el.className = 'mk3'; el.innerHTML = `<div class="bds">${b.join('')}</div><div class="stem"></div>`;
    el.querySelector('.bds').onclick = ev => { ev.stopPropagation(); M3.tip.remove(); flashTop(c.id); new maplibregl.Popup({ offset: 30, maxWidth: '300px' }).setLngLat(c.p).setHTML(srcHTML(cell)).addTo(M3.map); };
    M3.mk.push(new maplibregl.Marker({ element: el, anchor: 'bottom' }).setLngLat(c.p).addTo(M3.map));
  });
}
function clear3D() {
  M3.pending = null;
  M3.mk.forEach(m => m.remove()); M3.mk = []; M3.byId = null;
  if (!M3.loaded) return;
  M3.map.setPaintProperty('cells-fill', 'fill-color', NEUT3D);
  if (M3.blayer) M3.map.setPaintProperty(M3.blayer, 'fill-extrusion-color', NEUT3D);
}

/* ---------- point (T1) ---------- */
function renderPoint() {
  M3.pending = null;
  const P = R.candidates, ok = new Set(APPROVAL?.hypothesis_ids || P.hypotheses.map(h => h.id));
  const H = P.hypotheses.filter(h => ok.has(h.id)).sort((a, b) => a.rank_final - b.rank_final);
  $('#d-ftitle').textContent = 'Top-3 business hypotheses';
  const sev = v => v >= 8 ? '#0f766e' : v >= 6 ? '#5eaaa8' : v >= 4 ? '#f59e0b' : '#dc2626';
  const claim = c => `<li>${esc(c.text)} ${cite(c.evidence_id)}</li>`;
  $('#d-find').innerHTML = H.slice(0, 3).map((h, i) => `<div class="top"><div class="rk">#${i + 1}</div><b style="font-size:15px">${esc(h.name)}</b> <span class="small mut">rank initial #${h.rank_initial} → final #${h.rank_final}</span><div><span class="sc">${(+h.score).toFixed(1)}</span><span class="small mut"> / 10 recommendation score</span></div>
    ${h.dimensions.map(d => `<div class="dim"><span>${esc(d.name)}</span><div class="mini" style="height:8px"><div style="width:${d.score * 10}%;background:${sev(d.score)}"></div></div><b>${d.score}</b></div><div class="small mut" style="margin:-2px 0 4px 128px">${esc(d.why)} ${(d.evidence_ids || []).map(cite).join(' ')}</div>`).join('')}
    <div class="fa"><div><h4 style="color:var(--ok)">FOR</h4><ul>${(h.for || []).map(claim).join('')}</ul></div><div><h4 style="color:var(--bad)">AGAINST</h4><ul>${(h.against || []).map(claim).join('')}</ul></div><div><h4 class="mut">UNKNOWNS → validate</h4><ul>${(h.unknowns || []).map(u => `<li>${esc(u.text)}<br><span class="mut">→ ${esc(u.suggested_validation)}</span></li>`).join('')}</ul></div></div></div>`).join('') +
    (R.findings || []).map(f => `<div class="src finding" id="finding-${esc(f.finding_id)}"><div><span class="finding-id">${esc(f.finding_id)}</span><div><b>${esc(f.claim)}</b><div class="quote small">“${esc(f.quote)}”</div><span class="badge">${esc(f.status)} · ${esc(f.verification || 'unverified')}</span> ${(f.evidence_ids || []).map(cite).join(' ')}</div></div></div>`).join('');
  if (H.length > 1) {
    const [a, b] = H; $('#battle-card').classList.remove('hidden');
    $('#battle-title').innerHTML = `Hypothesis Battle: <span style="color:var(--acc)">${esc(a.name)}</span> vs ${esc(b.name)}`;
    $('#d-battle').innerHTML = `<div class="battle">${a.dimensions.map((d, k) => { const db = b.dimensions[k]?.score ?? 0; return `<div class="l">${d.score > db ? '<b>✓</b> ' : ''}<span class="bar" style="width:${d.score * 10}px;background:${d.score >= db ? '#0f766e' : '#94a3b8'}"></span></div><div class="lab">${esc(d.name)}</div><div><span class="bar" style="width:${db * 10}px;background:${db > d.score ? '#0f766e' : '#94a3b8'}"></span>${db > d.score ? ' <b>✓</b>' : ''}</div>`; }).join('')}</div>`;
  } else $('#battle-card').classList.add('hidden');
  const S = P.space, hc = ['#0f766e', '#7c3aed', '#d97706', '#be185d'], hidx = Object.fromEntries(P.hypotheses.map((h, i) => [h.id, i]));
  $('#d-maparea').textContent = `the space, its PLR cell, and competitors within ${P.radius_m || 500} m`;
  show3D(false); $('#map-ctl').classList.add('hidden');
  resetMap().setView([S.lat, S.lon], 15);
  if (R.cells_geojson && S.cell_id) L.geoJSON(R.cells_geojson, { filter: f => String(f.properties?.id ?? f.id) === S.cell_id, style: { color: '#0f766e', weight: 2, fillColor: '#99f6e4', fillOpacity: .35 } }).addTo(map);
  L.circle([S.lat, S.lon], { radius: P.radius_m || 500, color: '#0b1720', weight: 1, dashArray: '4 3', fill: false }).addTo(map);
  (P.competitors || []).filter(k => ok.has(k.hypothesis_id)).forEach(k => L.circleMarker([k.lat, k.lon], { radius: 6, color: '#fff', weight: 1, fillColor: hc[hidx[k.hypothesis_id] % hc.length], fillOpacity: .95 }).bindTooltip(`${esc(k.name || 'competitor')} [${CITE[k.evidence_id] || '?'}]`).addTo(map));
  L.circleMarker([S.lat, S.lon], { radius: 9, color: '#fff', weight: 2, fillColor: '#dc2626', fillOpacity: 1 }).bindTooltip(`<b>${esc(S.address)}</b>${S.cell_name ? '<br>PLR ' + esc(S.cell_name) : ''}`, { permanent: true, direction: 'top', offset: [0, -8] }).addTo(map);
  $('#legend').innerHTML = `<span style="color:#dc2626">●</span> the space ${P.hypotheses.map((h, i) => `<span style="color:${hc[i % hc.length]}">●</span> ${esc(h.name)}`).join(' ')}`;
  bindCites();
}

/* ---------- sources · trace · grade ---------- */
function renderSources() {
  const conf = { high: 'b-ok', medium: 'b-part', low: 'b-bad' }, st = { verified: 'b-ok', partial: 'b-part', unverified: '' };
  const E = R.evidence || [];
  $('#d-src').innerHTML = E.map(e => `<div class="src" id="src-${esc(e.evidence_id)}"><div class="num">[${CITE[e.evidence_id]}]</div><div><div class="row" style="gap:6px"><b>${esc(e.title || e.evidence_id)}</b><span class="badge b-${esc(e.method)}">${esc(e.method)}</span><span class="badge">${esc(e.tier)}</span><span class="badge ${conf[e.confidence] || ''}">confidence: ${esc(e.confidence)}</span><span class="badge ${st[e.status] || ''}">${esc(e.status)}</span><span class="small mut">${esc(e.evidence_id)}</span></div>
    <div class="quote small" style="margin:3px 0">“${esc(e.quote)}”</div>${link(e.url, e.url)}</div></div>`).join('') || '<p class="mut">No ledger entries.</p>';
  const unsupportedEvidenceEntries = E.filter(e => e.status === 'unsupported');
  const unsupportedEvidenceIds = new Set(unsupportedEvidenceEntries.map(e => e.evidence_id));
  const unsupportedFindings = (R.findings || [])
    .filter(f => f.verification === 'unsupported' || f.status === 'rejected')
    .filter(f => !(f.evidence_ids || []).length || !f.evidence_ids.every(id => unsupportedEvidenceIds.has(id)));
  const unsupportedEvidence = unsupportedEvidenceEntries.map(e => `<div class="src"><div class="num" style="color:var(--bad)">✕</div><div><b>${esc(e.claim || e.title || e.quote)}</b> <span class="badge b-bad">unsupported</span><div class="small mut">${esc(e.reason || '')}</div>${link(e.url || e.source_url, e.url || e.source_url)}</div></div>`).join('');
  const unsupportedClaims = unsupportedFindings.map(f => `<div class="src"><div class="num" style="color:var(--bad)">✕</div><div><b>${esc(f.claim)}</b> <span class="badge b-bad">${esc(f.status)} · ${esc(f.verification)}</span><div class="small mut">${esc(f.why || '')}</div></div></div>`).join('');
  $('#d-nover').innerHTML = unsupportedEvidence + unsupportedClaims || '<p class="mut small">Nothing was dropped.</p>';
}
function renderTrace() {
  const T = R.trace || [];
  const metrics = R.metrics || {};
  const rows = Object.entries(metrics).map(([role, value]) => `<tr><td>${esc(role)}</td><td class="small">${esc(value.model || 'deterministic')}</td><td>${value.calls || 0}</td><td>${((value.tokens_in || 0) + (value.tokens_out || 0)).toLocaleString()}</td><td>${(+value.seconds || 0).toFixed(1)}</td></tr>`);
  const totals = Object.values(metrics).reduce((sum, value) => [
    sum[0] + (+value.calls || 0),
    sum[1] + (+value.tokens_in || 0) + (+value.tokens_out || 0),
    sum[2] + (+value.seconds || 0)
  ], [0, 0, 0]);
  $('#d-models').innerHTML = `<table><thead><tr><th>Role</th><th>Model</th><th>Calls</th><th>Tokens</th><th>Seconds</th></tr></thead><tbody>${rows.join('')}<tr><td><b>Total</b></td><td></td><td><b>${totals[0]}</b></td><td><b>${totals[1].toLocaleString()}</b></td><td><b>${totals[2].toFixed(1)}</b></td></tr></tbody></table>`;
  $('#d-trace').innerHTML = T.map(e => evRow(e, e.seq ?? '')).join('');
}
function renderGrade() {
  const g = R.self_grade || [];
  $('#d-grade').innerHTML = g.map(r => `<tr><td style="color:${r.ok ? 'var(--ok)' : 'var(--warn)'};font-weight:800">${r.ok ? '✓' : '!'}</td><td>${esc(r.text)}</td></tr>`).join('') || '<tr><td></td><td class="mut">No self-grade in this result.</td></tr>';
}
