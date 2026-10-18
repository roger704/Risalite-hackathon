/* Deterministic client-side scoring and §5.5 risk ranking. */
(function (g) {
  function scoreCells(doc, on, weights, mode) {
    const layers = doc.layers.filter(l => on[l.id]);
    const means = Object.fromEntries(layers.map(layer => {
      const values = doc.cells.map(cell => cell.values[layer.id])
        .filter(value => value !== null && value !== undefined);
      const mean = values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0;
      return [layer.id, mean];
    }));
    const raw = doc.cells.map(c => layers.reduce((s, l) => {
      const value = c.values[l.id] ?? means[l.id];
      return s + weights[l.id] * (l.direction === '-' ? -1 : 1) * value;
    }, 0));
    const mn = Math.min(...raw), mx = Math.max(...raw);
    const base = doc.cells.map((cell, i) => {
      let s = mx > mn ? (raw[i] - mn) / (mx - mn) : 0.5;
      if (mode === 'future') s += (cell.modifiers || []).reduce((a, m) => a + (m.bonus ?? 0.10), 0);
      return { cell, key: s, score: Math.max(0, Math.min(1, s)) };
    }).sort((a, b) => b.key - a.key || String(a.cell.id).localeCompare(String(b.cell.id)));
    return rank(base);
  }
  function rank(cells) {
    const base = cells.map((entry, index) => ({ ...entry, baseIndex: index }));
    const keys = base.map((entry, i) => {
      if (!(entry.cell.demoted_by || []).length) return i;
      let j = i + 1;
      while (j < base.length && (base[j].cell.demoted_by || []).length) j++;
      return j < base.length ? j + 0.5 : i;
    });
    return base.map((entry, i) => ({ ...entry, rankKey: keys[i] }))
      .sort((a, b) => a.rankKey - b.rankKey || a.baseIndex - b.baseIndex)
      .map(({ baseIndex, rankKey, ...entry }) => entry);
  }
  g.SpotWiseScoring = { scoreCells, rank };
})(window);
