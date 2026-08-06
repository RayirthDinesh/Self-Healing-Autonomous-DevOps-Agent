"""Dashboard HTTP surface: auth, run list, run detail, artifacts."""

import pytest
from fastapi.testclient import TestClient

import dashboard
import run_tracker

SECRET = "test-secret"


@pytest.fixture(autouse=True)
def not_local():
    """Default to the exposed posture; the local-mode tests opt in."""
    dashboard.enable_local_mode(False)
    yield
    dashboard.enable_local_mode(False)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "memory.db"))
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("DASHBOARD_SECRET", raising=False)
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


def test_ui_moves_the_key_out_of_the_url_into_a_cookie(client, seeded):
    """The query string lands in access logs and history, so it must not stick."""
    r = client.get(f"/ui?key={SECRET}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui"
    cookie = r.headers["set-cookie"]
    assert SECRET in cookie and "HttpOnly" in cookie

    page = client.get(f"/ui?key={SECRET}")          # follows the redirect
    assert page.status_code == 200 and "SRE" in page.text


def test_dashboard_refuses_when_no_secret_is_configured(client, seeded, monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECRET")
    assert client.get(f"/api/runs?key={SECRET}").status_code == 401


def test_dashboard_secret_is_preferred_over_the_webhook_secret(client, seeded, monkeypatch):
    """A read-only secret must not have to be the one that can trigger runs."""
    monkeypatch.setenv("DASHBOARD_SECRET", "view-only")
    assert client.get("/api/runs?key=view-only").status_code == 200
    assert client.get(f"/api/runs?key={SECRET}").status_code == 401


# ── local mode: clone the repo, run it, no secret ────────────────────────────

@pytest.fixture
def local_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DB", str(tmp_path / "memory.db"))
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("DASHBOARD_SECRET", raising=False)
    run_tracker.set_current_run(None)
    dashboard.enable_local_mode(True)


@pytest.fixture
def local_client(local_env):
    """A browser on this machine hitting the loopback listener."""
    return TestClient(dashboard.build_app(), base_url="http://localhost",
                      client=("127.0.0.1", 51000))


def test_local_mode_needs_no_secret(local_client, seeded):
    assert local_client.get("/api/runs").status_code == 200
    assert local_client.get("/ui").status_code == 200


def test_local_mode_rejects_a_non_loopback_host_header(local_client, seeded):
    """Defends against DNS rebinding: an attacker page resolving its own
    hostname to 127.0.0.1 would otherwise read the console from the browser."""
    r = local_client.get("/api/runs", headers={"Host": "evil.example.com"})
    assert r.status_code == 401


def test_local_mode_rejects_a_remote_client(local_env, seeded):
    """Local mode is loopback-only; a routed client still needs the secret."""
    remote = TestClient(dashboard.build_app(), base_url="http://localhost",
                        client=("203.0.113.9", 51000))
    assert remote.get("/api/runs").status_code == 401


def test_mounting_into_another_app_never_enables_local_mode(client, seeded):
    """main.py binds 0.0.0.0, so importing the router must stay authenticated."""
    assert dashboard.local_mode() is False
    assert client.get("/api/runs").status_code == 401


def test_loopback_bind_detection():
    assert dashboard._is_loopback_bind("127.0.0.1")
    assert dashboard._is_loopback_bind("localhost")
    assert dashboard._is_loopback_bind("::1")
    assert not dashboard._is_loopback_bind("0.0.0.0")
    assert not dashboard._is_loopback_bind("10.0.0.5")
