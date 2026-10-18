import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[3] / "frontend" / "app" / "app.js"


def test_report_headings_grouped_citations_and_untrusted_html():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the browser renderer check")
    script = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const lines = source.split('\n');
const code = [
  lines.find(line => line.startsWith('const esc =')),
  lines.find(line => line.startsWith('const cite =')),
  source.slice(source.indexOf('function renderInline('), source.indexOf('const fmt =')),
  'renderMarkdown(input)'
].join('\n');
const html = vm.runInNewContext(code, {
  input: '# Report\n\n## Findings\n\nSupported [ev_a, ev_b] and [ds_c].\n\n<script>alert(1)</script>',
  CITE: {ev_a: 1, ev_b: 2, ds_c: 3}, R: {evidence: []}
});
process.stdout.write(JSON.stringify(html));
"""
    result = subprocess.run(
        [node, "-e", script, str(APP_JS)], capture_output=True, text=True, check=True
    )
    html = json.loads(result.stdout)
    assert "<h3>Report</h3>" in html
    assert "<h4>Findings</h4>" in html
    for evidence_id in ("ev_a", "ev_b", "ds_c"):
        assert f'data-e="{evidence_id}"' in html
    assert "[ev_a" not in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
