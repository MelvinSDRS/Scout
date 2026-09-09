from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from scout.api import create_app
from scout.dashboard import dashboard_link
from scout.dashboard_auth import DashboardAuth
from scout.settings import Settings
from scout.store import Store


@pytest.fixture
def setup(tmp_path):
    settings = Settings(tmp_path, api_token="synthetic-dashboard-secret-" + "x" * 24)
    store = Store(tmp_path / "scout.sqlite3")
    auth = DashboardAuth(store, settings)
    with TestClient(create_app(store, settings, start_worker=False)) as client:
        yield settings, store, auth, client


def sign_in(client, ticket):
    return client.post(
        "/api/session", json={"ticket": ticket}, headers={"Origin": "http://testserver"}
    )


def test_one_time_link_creates_remembered_session(setup):
    settings, store, auth, client = setup
    ticket = auth.issue_ticket()
    assert ticket not in client.get("/").text
    assert settings.api_token not in client.get("/").text
    result = sign_in(client, ticket)
    assert result.status_code == 200
    cookie = result.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age=2592000" in cookie
    assert result.headers["cache-control"] == "no-store"
    session = client.cookies.get(auth.cookie_name)
    assert client.get("/api/health").status_code == 200
    with store.connect() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM dashboard_auth")]
    assert ticket not in str(rows) and session not in str(rows)
    assert sign_in(client, ticket).status_code == 401
    assert (
        client.get("/api/health").status_code == 200
    )  # Invalid link doesn't revoke a valid session.


def test_ticket_consumption_is_atomic(setup):
    _, _, auth, _ = setup
    ticket = auth.issue_ticket()

    def exchange():
        try:
            auth.create_session(ticket=ticket)
            return True
        except HTTPException:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: exchange(), range(2))) == [False, True]


def test_expired_and_rotated_credentials_are_rejected(setup):
    settings, store, auth, client = setup
    ticket = auth.issue_ticket()
    with store.connect() as db:
        db.execute("UPDATE dashboard_auth SET expires=0")
    assert sign_in(client, ticket).status_code == 401
    assert sign_in(client, auth.issue_ticket()).status_code == 200
    session = client.cookies.get(auth.cookie_name)
    rotated = DashboardAuth(store, Settings(settings.data_dir, api_token="different-" + "x" * 24))
    assert not rotated.valid_session(session)
    with store.connect() as db:
        db.execute("UPDATE dashboard_auth SET expires=0")
    assert client.get("/api/health").status_code == 401


def test_session_survives_server_restart_and_logout_revokes_it(setup):
    settings, store, auth, client = setup
    assert sign_in(client, auth.issue_ticket()).status_code == 200
    session = client.cookies.get(auth.cookie_name)
    with TestClient(create_app(store, settings, start_worker=False)) as restarted:
        restarted.cookies.set(auth.cookie_name, session)
        assert restarted.get("/api/health").status_code == 200
        assert (
            restarted.delete("/api/session", headers={"Origin": "http://testserver"}).status_code
            == 200
        )
        assert restarted.get("/api/health").status_code == 401
    assert not auth.valid_session(session)


def test_cookie_mutations_and_sign_in_require_same_origin(setup):
    _, _, auth, client = setup
    ticket = auth.issue_ticket()
    for headers in (
        {},
        {"Origin": "https://evil.test"},
        {"Origin": "null"},
        {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"},
    ):
        assert (
            client.post("/api/session", json={"ticket": ticket}, headers=headers).status_code == 403
        )
    assert sign_in(client, ticket).status_code == 200
    for headers in ({}, {"Origin": "https://evil.test"}, {"Origin": "null"}):
        assert client.delete("/api/session", headers=headers).status_code == 403
        assert client.post("/api/watches", json={}, headers=headers).status_code == 403
    assert client.get("/api/health", headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.get("/api/health").status_code == 200


def test_manual_token_and_api_bearer_still_work(setup):
    settings, _, auth, client = setup
    assert (
        client.post(
            "/api/session",
            json={"token": settings.api_token},
            headers={"Origin": "http://testserver"},
        ).status_code
        == 200
    )
    previous = client.cookies.get(auth.cookie_name)
    assert sign_in(client, auth.issue_ticket()).status_code == 200
    assert not auth.valid_session(previous)
    client.cookies.clear()
    assert (
        client.get(
            "/api/health", headers={"Authorization": "Bearer " + settings.api_token}
        ).status_code
        == 200
    )
    assert client.get("/api/health").status_code == 401


def test_https_cookie_and_untrusted_hosts(setup):
    settings, store, auth, _ = setup
    with TestClient(
        create_app(store, settings, start_worker=False), base_url="https://testserver"
    ) as client:
        result = client.post(
            "/api/session",
            json={"ticket": auth.issue_ticket()},
            headers={"Origin": "https://testserver"},
        )
        assert result.status_code == 200 and "Secure" in result.headers["set-cookie"]
        assert client.get("/api/health", headers={"Host": "evil.test"}).status_code == 400


def test_launcher_link_uses_fragment_and_rejects_unsafe_urls(setup):
    settings, store, _, _ = setup
    assert dashboard_link(store, settings, "http://127.0.0.1:8765").startswith(
        "http://127.0.0.1:8765/#signin="
    )
    for url in (
        "javascript:alert(1)",
        "http://example.org",
        "https://user:password@example.org",  # pragma: allowlist secret - synthetic rejected URL
        "https://example.org/?secret=123",
        "https://example.org/#existing",
    ):
        with pytest.raises(RuntimeError):
            dashboard_link(store, settings, url)
