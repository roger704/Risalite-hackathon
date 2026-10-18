/* Deterministic client-side scoring — mirrors backend app.scoring.v2 (§9.7 D8, §5.5, §5.8.6).
 * Values are directional (1 = good) when doc.values_directional is true: never flip '-' again.
 * score_present = weighted mean over enabled layers with a value (weights re-normalized, no
 * rescale); score_future = clamp01(present + clamp(Σ bonus, ±bonus_cap)). Parity: golden vectors
 * in backend/tests/scoring/golden_vectors.json (backend/tests/scoring/test_frontend_parity.py). */
(function (g) {
  const EPS = 1e-9;
  const clamp01 = v => Math.max(0, Math.min(1, v));
  function present(doc, cell, on, weights) {
    let total = 0, sum = 0;
    for (const layer of doc.layers) {
      const w = on[layer.id] ? Number(weights[layer.id] ?? 0) : 0;
      let v = cell.values[layer.id];
      if (!(w > 0) || v === null || v === undefined) continue;
      // Legacy documents (no values_directional flag) carry raw-direction values.
      if (doc.values_directional === undefined && layer.direction === '-') v = 1 - v;
      total += w * v; sum += w;
    }
    return sum > 0 ? total / sum : null;
  }
  function future(doc, cell, base) {
    if (base === null) return null;
    const cap = doc.bonus_cap ?? 0.15;
    const bonus = (cell.modifiers || []).reduce((a, m) => a + (m.bonus ?? 0.10), 0);
    return clamp01(base + Math.max(-cap, Math.min(cap, bonus)));
  }
  function baseOrder(items) {
    return items.map((_, i) => i).sort((a, b) => {
      const x = items[a], y = items[b];
      if (x.eligible !== y.eligible) return x.eligible ? -1 : 1;
      if ((x.score === null) !== (y.score === null)) return x.score === null ? 1 : -1;
      if (x.score !== y.score) return (y.score ?? 0) - (x.score ?? 0);
      return x.id < y.id ? -1 : x.id > y.id ? 1 : 0;
    });
  }
  function tiers(items, order, band) {
    const out = {};
    let tier = 0, prev = null;
    for (const i of order) {
      const it = items[i];
      const tied = prev !== null && prev.eligible === it.eligible && prev.score !== null &&
        it.score !== null && Math.abs(prev.score - it.score) <= band + EPS;
      if (!tied) tier++;
      out[i] = tier; prev = it;
    }
    return out;
  }
  function demote(group, items) {
    const keys = group.map((index, pos) => {
      if (!items[index].demoted) return pos;
      for (let j = pos + 1; j < group.length; j++) if (!items[group[j]].demoted) return j + 0.5;
      return pos;
    });
    return group.map((index, pos) => ({ index, pos, key: keys[pos] }))
      .sort((a, b) => a.key - b.key || a.pos - b.pos).map(e => e.index);
  }
  /* §5.5 rank on (eligible, score, id) base order; incomplete cells always after eligible ones. */
  function rank(entries, band) {
    const items = entries.map(e => ({
      id: String(e.cell.id), score: e.score, eligible: !e.cell.incomplete,
      demoted: (e.cell.demoted_by || []).length > 0,
    }));
    const order = baseOrder(items);
    const tier = tiers(items, order, band ?? 0.02);
    const ranked = [...demote(order.filter(i => items[i].eligible), items),
      ...demote(order.filter(i => !items[i].eligible), items)];
    return ranked.map(i => ({ ...entries[i], tier: tier[i] }));
  }
  function scoreCells(doc, on, weights, mode) {
    const entries = doc.cells.map(cell => {
      const p = present(doc, cell, on, weights), f = future(doc, cell, p);
      const s = mode === 'future' && f !== null ? f : p;
      return { cell, score_present: p, score_future: f, score: s ?? 0, rankScore: s };
    });
    return rank(entries.map(e => ({ ...e, score: e.rankScore })), doc.tie_band)
      .map(({ rankScore, ...e }) => ({ ...e, score: e.score ?? 0 }));
  }
  const api = { scoreCells, rank };
  g.RisaliteScoring = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
