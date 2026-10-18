/* Spec scoring (Risalite_Spec.md, cells.json rules). Pure functions; shared with tests via node-free reimplementation in Python. */
(function (g) {
  function scoreCells(doc, on, weights, mode) {
    const layers = doc.layers.filter(l => on[l.id]);
    const raw = doc.cells.map(c => layers.reduce((s, l) => {
      const v = c.values[l.id];
      return v === null || v === undefined ? s : s + weights[l.id] * (l.direction === '-' ? -1 : 1) * v;
    }, 0));
    const mn = Math.min(...raw), mx = Math.max(...raw);
    return doc.cells.map((cell, i) => {
      let s = mx > mn ? (raw[i] - mn) / (mx - mn) : 0.5;
      if (mode === 'future') s += (cell.modifiers || []).reduce((a, m) => a + (m.bonus ?? 0.10), 0);
      s -= (cell.risks || []).reduce((a, r) => a + (r.demotion || 0), 0);
      return { cell, key: s, score: Math.max(0, Math.min(1, s)) };
    }).sort((a, b) => b.key - a.key);
  }
  g.RisaliteScoring = { scoreCells };
})(window);
