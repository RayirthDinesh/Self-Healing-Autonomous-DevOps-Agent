"""Dashboard HTTP surface: auth, run list, run detail, artifacts."""

import pytest
from fastapi.testclient import TestClient

import dashboard
import run_tracker

SECRET = "test-secret"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "memory.db"))
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    run_tracker.set_current_run(None)
    return TestClient(dashboard.build_app())


@pytest.fixture
def seeded():
    run_id = run_tracker.start_run("acme/app", "bug/x", "abc1234", source="webhook",
                                   mode="graph")
    run_tracker.step("ingest", detail={"error_class": "type-error"})
    art_id = run_tracker.artifact("diff", "attempt 1", "--- a\n+++ b\n", node="validator")
    run_tracker.finish_run("passed", "published", pr_url="https://pr/1")
    return run_id, art_id


def test_api_requires_the_secret(client, seeded):
    assert client.get("/api/runs").status_code == 401
    assert client.get("/ui").status_code == 401


def test_run_list_returns_runs_with_the_key(client, seeded):
    r = client.get(f"/api/runs?key={SECRET}")
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["repo"] == "acme/app"
    assert runs[0]["status"] == "passed"


def test_header_and_cookie_both_authenticate(client, seeded):
    assert client.get("/api/runs", headers={"X-Webhook-Secret": SECRET}).status_code == 200
    client.cookies.set("sre_key", SECRET)
    assert client.get("/api/runs").status_code == 200


def test_run_detail_carries_events_and_artifact_index(client, seeded):
    run_id, art_id = seeded
    body = client.get(f"/api/runs/{run_id}?key={SECRET}").json()
    assert body["run"]["outcome"] == "published"
    assert body["events"][0]["node"] == "ingest"
    assert body["events"][0]["detail"] == {"error_class": "type-error"}
    index = body["artifacts"]
    assert index[0]["id"] == art_id and index[0]["kind"] == "diff"
    assert "body" not in index[0]  # bodies are fetched on demand


def test_artifact_fetch_returns_the_body(client, seeded):
    _, art_id = seeded
    body = client.get(f"/api/artifacts/{art_id}?key={SECRET}").json()
    assert body["body"] == "--- a\n+++ b\n"


def test_unknown_ids_are_404(client, seeded):
    assert client.get(f"/api/runs/nope?key={SECRET}").status_code == 404
    assert client.get(f"/api/artifacts/9999?key={SECRET}").status_code == 404
    assert client.get(f"/api/runs/nope/stream?key={SECRET}").status_code == 404


def test_stream_emits_the_run_then_closes(client, seeded):
    run_id, _ = seeded
    with client.stream("GET", f"/api/runs/{run_id}/stream?key={SECRET}") as r:
        assert r.status_code == 200
        chunks = []
        for line in r.iter_lines():
            chunks.append(line)
            if line.startswith("event: done"):
                break
    payload = "\n".join(chunks)
    assert "ingest" in payload
    assert "event: done" in payload


def test_ui_page_is_served_and_sets_the_cookie(client, seeded):
    r = client.get(f"/ui?key={SECRET}")
    assert r.status_code == 200
    assert "SRE" in r.text
    assert r.cookies.get("sre_key") == SECRET


def test_dashboard_refuses_when_no_secret_is_configured(client, seeded, monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECRET")
    assert client.get(f"/api/runs?key={SECRET}").status_code == 401
