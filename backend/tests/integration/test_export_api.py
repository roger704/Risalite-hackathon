"""HTTP and CLI boundaries for research run exports."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.cli import main as cli

RUNS = Path(__file__).resolve().parents[1] / "fixtures" / "runs"


@pytest.fixture
def client():
    with TestClient(create_app(runs_dir=RUNS)) as test_client:
        yield test_client


def test_create_app_keeps_fourth_positional_argument_as_runs_dir(tmp_path):
    app = create_app(None, tmp_path / "data", tmp_path / "config", RUNS)
    with TestClient(app) as test_client:
        response = test_client.get("/api/v1/runs/demo-coffee/export/report.md")
    assert response.status_code == 200


@pytest.mark.parametrize("explicit_runs_dir", [False, True])
def test_completed_live_run_exports_from_configured_runs_dir(
    tmp_path, monkeypatch, explicit_runs_dir
):
    configured_dir = tmp_path / "configured-runs"
    explicit_dir = tmp_path / "explicit-runs"
    monkeypatch.setenv("RISALITE_RUNS", "live")
    monkeypatch.setenv("RISALITE_RUNS_DIR", str(configured_dir))
    monkeypatch.setenv("RISALITE_AREA_DATA", "fixtures")
    monkeypatch.setenv("RISALITE_RESEARCH_FAKE", "1")
    app = create_app(runs_dir=explicit_dir) if explicit_runs_dir else create_app()
    expected_dir = explicit_dir if explicit_runs_dir else configured_dir

    with TestClient(app) as test_client:
        created = test_client.post(
            "/research",
            json={"question": "Where is it smart to open a coffee place in Kreuzberg?"},
        ).json()
        authorization = {"Authorization": f"Bearer {created['run_token']}"}
        approved = test_client.post(
            f"/runs/{created['run_id']}/approve",
            json={"wave2": False},
            headers=authorization,
        )
        assert approved.status_code == 200
        events = test_client.get(f"/runs/{created['run_id']}/events", headers=authorization)
        assert "data:" in events.text
        result = test_client.get(f"/runs/{created['run_id']}/result", headers=authorization)
        assert result.status_code == 200
        assert result.json()["status"] == "done"

        export_path = f"/api/v1/runs/{created['run_id']}/export/sources.csv"
        assert test_client.get(export_path).status_code == 404
        assert (
            test_client.get(export_path, headers={"Authorization": "Bearer invalid"}).status_code
            == 404
        )
        assert test_client.get(export_path, headers=authorization).status_code == 200

    assert (expected_dir / created["run_id"] / "run.json").is_file()


@pytest.mark.parametrize(
    "fmt,media,disposition",
    [
        ("report.md", "text/markdown; charset=utf-8", "attachment"),
        ("report.html", "text/html; charset=utf-8", "inline"),
        ("sources.csv", "text/csv; charset=utf-8", "attachment"),
        ("sources.bib", "application/x-bibtex; charset=utf-8", "attachment"),
    ],
)
def test_each_export_format_has_expected_headers(client, fmt, media, disposition):
    response = client.get(f"/api/v1/runs/demo-coffee/export/{fmt}")
    assert response.status_code == 200
    assert response.headers["content-type"] == media
    assert response.headers["content-disposition"] == (
        f'{disposition}; filename="risalite-demo-coffee-{fmt}"'
    )
    if fmt == "sources.csv":
        assert response.content.startswith(b"\xef\xbb\xbf")


def test_mix_filters_change_top_rank_in_report(client):
    default = client.get("/api/v1/runs/demo-coffee/export/report.md")
    changed = client.get(
        "/api/v1/runs/demo-coffee/export/report.md",
        params={"layers": "demand_density,competition,rent", "mode": "present"},
    )
    future = client.get(
        "/api/v1/runs/demo-coffee/export/report.md",
        params={"mode": "future"},
    )
    assert "1. Bergmannkiez" in default.text
    assert "1. Admiralskiez" in changed.text
    assert "1. Admiralskiez" in future.text


@pytest.mark.parametrize("run_id", ["..%2F..", "a.b"])
def test_invalid_and_traversal_run_ids_are_not_found(client, run_id):
    response = client.get(f"/api/v1/runs/{run_id}/export/report.md")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_unknown_format_unknown_layer_and_extra_filter_errors(client):
    unknown_format = client.get("/api/v1/runs/demo-coffee/export/nope")
    assert unknown_format.status_code == 404
    assert unknown_format.json()["error"]["code"] == "UNKNOWN_FORMAT"

    unknown_layer = client.get(
        "/api/v1/runs/demo-coffee/export/report.md", params={"layers": "not-a-layer"}
    )
    assert unknown_layer.status_code == 422
    assert unknown_layer.json()["error"]["code"] == "UNKNOWN_LAYER"

    extra = client.get("/api/v1/runs/demo-coffee/export/report.md", params={"limit": 10})
    assert extra.status_code == 422
    assert extra.json()["error"]["code"] == "UNSUPPORTED_FILTER"


def test_cli_export_writes_requested_file(tmp_path, capsys):
    output = tmp_path / "report.html"
    result = cli.main(
        [
            "export",
            str(RUNS / "demo-coffee"),
            "--format",
            "html",
            "-o",
            str(output),
        ]
    )
    assert result == 0
    assert output.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert capsys.readouterr().out == ""
