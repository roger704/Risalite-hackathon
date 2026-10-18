import importlib.util
import shutil
from pathlib import Path

import httpx

from tests.integration.test_factcheck import FEATURES, PAGES, public_resolver

FIXTURE = Path(__file__).parents[1] / "fixtures" / "runs" / "demo_area"
SCRIPT = Path(__file__).parents[3] / "scripts" / "verify_citations.py"
NEWS = "https://www.berlin.de/ba-friedrichshain-kreuzberg/aktuelles/wohnungsbau"


def load_script():
    spec = importlib.util.spec_from_file_location("verify_citations", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def handler(request):
    if request.url.host == "gdi.berlin.de":
        return httpx.Response(200, json=FEATURES)
    if str(request.url) == NEWS:
        return httpx.Response(200, text=f"<p>{PAGES[NEWS]}</p>")
    return httpx.Response(404)


async def test_verify_citations_on_fixture_run(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    run_dir = tmp_path / "demo_area"
    shutil.copytree(FIXTURE, run_dir)
    script = load_script()
    transport = httpx.MockTransport(handler)
    code = await script.main([str(run_dir)], transport=transport, resolver=public_resolver)
    out = capsys.readouterr().out
    assert code == 1
    assert "no citable ledger entry e7" in out
    assert "score: 85.7% (7 checks)" in out and "FAIL" in out
    code = await script.main(
        [str(run_dir), "--threshold", "0.8"], transport=transport, resolver=public_resolver
    )
    assert code == 0 and "PASS" in capsys.readouterr().out


async def test_verify_citations_requires_ledger(tmp_path):
    assert await load_script().main([str(tmp_path)]) == 2


async def test_verify_citations_flags_unresolvable_url(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    run_dir = tmp_path / "demo_area"
    shutil.copytree(FIXTURE, run_dir)
    transport = httpx.MockTransport(lambda r: httpx.Response(404))
    code = await load_script().main(
        [str(run_dir), "--threshold", "0.5"], transport=transport, resolver=public_resolver
    )
    assert code == 1


async def test_judge_rejects_private_urls():
    from app.evidence.judge import _resolves

    async def resolver(host, port):
        return ["127.0.0.1"]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok, detail = await _resolves(client, "https://intranet.example/x", resolver)
    assert not ok and "url rejected" in detail
