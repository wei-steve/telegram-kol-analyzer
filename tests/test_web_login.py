"""Tests for the in-app web login that replaces Nginx Basic Auth.

The whole feature is dormant unless three environment variables are set, so
every test here either leaves them unset (the behaviour every other test in
this repository relies on) or sets all three through ``monkeypatch``.
"""

import logging
import time

import pytest
from fastapi.testclient import TestClient

from telegram_kol_research.web_app import (
    DEFAULT_WEB_LOGIN_SESSION_DAYS,
    WEB_LOGIN_PASSWORD_HASH_ENV,
    WEB_LOGIN_SESSION_COOKIE_NAME,
    WEB_LOGIN_SESSION_DAYS_ENV,
    WEB_LOGIN_SESSION_SECRET_ENV,
    WEB_LOGIN_USERNAME_ENV,
    WebLoginConfig,
    build_web_login_password_hash,
    create_web_app,
    issue_web_login_cookie_value,
    load_web_login_config,
    sanitize_web_login_next,
    verify_web_login_cookie_value,
    verify_web_login_password,
)

USERNAME = "steven"
PASSWORD = "correct-horse-battery"
SECRET = "a" * 64


@pytest.fixture
def password_hash():
    # One derivation shared by every test that needs it: scrypt is deliberately
    # slow, and re-deriving it per test dominates the runtime of this module.
    return build_web_login_password_hash(PASSWORD)


def _enable_login(monkeypatch, password_hash, *, days=None):
    monkeypatch.setenv(WEB_LOGIN_USERNAME_ENV, USERNAME)
    monkeypatch.setenv(WEB_LOGIN_PASSWORD_HASH_ENV, password_hash)
    monkeypatch.setenv(WEB_LOGIN_SESSION_SECRET_ENV, SECRET)
    if days is None:
        monkeypatch.delenv(WEB_LOGIN_SESSION_DAYS_ENV, raising=False)
    else:
        monkeypatch.setenv(WEB_LOGIN_SESSION_DAYS_ENV, str(days))


def _disable_login(monkeypatch):
    for name in (
        WEB_LOGIN_USERNAME_ENV,
        WEB_LOGIN_PASSWORD_HASH_ENV,
        WEB_LOGIN_SESSION_SECRET_ENV,
        WEB_LOGIN_SESSION_DAYS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _client(tmp_path, **kwargs):
    app = create_web_app(database_path=tmp_path / "research.db")
    return TestClient(app, **kwargs)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def test_login_disabled_when_no_environment_is_set(tmp_path, monkeypatch):
    _disable_login(monkeypatch)

    client = _client(tmp_path)

    assert client.get("/static/app.css").status_code == 200
    response = client.get("/api/monitor-status")
    assert response.status_code != 401


def test_login_disabled_leaves_login_page_redirecting_home(tmp_path, monkeypatch):
    _disable_login(monkeypatch)

    client = _client(tmp_path)

    response = client.get("/login", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/"


@pytest.mark.parametrize(
    "present",
    [
        {WEB_LOGIN_USERNAME_ENV: USERNAME},
        {WEB_LOGIN_SESSION_SECRET_ENV: SECRET},
        {
            WEB_LOGIN_USERNAME_ENV: USERNAME,
            WEB_LOGIN_SESSION_SECRET_ENV: SECRET,
        },
    ],
)
def test_partial_configuration_refuses_to_build_the_app(
    tmp_path, monkeypatch, present
):
    _disable_login(monkeypatch)
    for name, value in present.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        create_web_app(database_path=tmp_path / "research.db")


def test_password_hash_must_look_like_a_scrypt_hash(monkeypatch):
    with pytest.raises(ValueError):
        load_web_login_config(
            {
                WEB_LOGIN_USERNAME_ENV: USERNAME,
                WEB_LOGIN_PASSWORD_HASH_ENV: "plaintext",
                WEB_LOGIN_SESSION_SECRET_ENV: SECRET,
            }
        )


def test_session_days_defaults_and_overrides(password_hash):
    base = {
        WEB_LOGIN_USERNAME_ENV: USERNAME,
        WEB_LOGIN_PASSWORD_HASH_ENV: password_hash,
        WEB_LOGIN_SESSION_SECRET_ENV: SECRET,
    }

    assert load_web_login_config(base).session_days == DEFAULT_WEB_LOGIN_SESSION_DAYS
    assert load_web_login_config({**base, WEB_LOGIN_SESSION_DAYS_ENV: "7"}).session_days == 7
    with pytest.raises(ValueError):
        load_web_login_config({**base, WEB_LOGIN_SESSION_DAYS_ENV: "0"})
    with pytest.raises(ValueError):
        load_web_login_config({**base, WEB_LOGIN_SESSION_DAYS_ENV: "many"})


# --------------------------------------------------------------------------
# hash helper
# --------------------------------------------------------------------------


def test_password_hash_round_trip(password_hash):
    assert password_hash.startswith("scrypt$")
    assert len(password_hash.split("$")) == 6
    assert verify_web_login_password(PASSWORD, password_hash) is True
    assert verify_web_login_password(PASSWORD + "x", password_hash) is False
    assert verify_web_login_password("", password_hash) is False


def test_password_hash_is_salted(password_hash):
    other = build_web_login_password_hash(PASSWORD)

    assert other != password_hash
    assert verify_web_login_password(PASSWORD, other) is True


@pytest.mark.parametrize(
    "bad",
    ["", "scrypt$", "scrypt$16384$8$1$notbase64$notbase64", "bcrypt$1$2$3$4$5"],
)
def test_malformed_hashes_are_rejected_rather_than_raising(bad):
    assert verify_web_login_password(PASSWORD, bad) is False


# --------------------------------------------------------------------------
# gate behaviour
# --------------------------------------------------------------------------


def test_unauthenticated_navigation_redirects_to_login(
    tmp_path, monkeypatch, password_hash
):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)

    response = client.get(
        "/logs", headers={"Accept": "text/html"}, follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/login?next=%2Flogs"


def test_unauthenticated_fetch_gets_401_json(tmp_path, monkeypatch, password_hash):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)

    response = client.get(
        "/api/monitor-status", headers={"Accept": "application/json"}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "authentication required"


def test_login_page_and_static_assets_stay_reachable(
    tmp_path, monkeypatch, password_hash
):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)

    page = client.get("/login")
    css = client.get("/static/app.css")

    assert page.status_code == 200
    assert css.status_code == 200
    assert 'autocomplete="username"' in page.text
    assert 'autocomplete="current-password"' in page.text
    # A standalone page: the workbench bundle must not run behind the gate.
    assert "app.js" not in page.text


def test_static_asset_cache_header_is_unchanged_by_the_gate(
    tmp_path, monkeypatch, password_hash
):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)

    version = client.get("/login").headers["X-Workbench-Asset-Version"]
    response = client.get(f"/static/app.css?v={version}")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_wrong_password_rerenders_without_a_cookie(
    tmp_path, monkeypatch, password_hash
):
    _enable_login(monkeypatch, password_hash)
    monkeypatch.setattr(
        "telegram_kol_research.web_app.WEB_LOGIN_FAILURE_DELAY_SECONDS", 0.0
    )
    client = _client(tmp_path)

    response = client.post(
        "/login",
        data={"username": USERNAME, "password": "wrong", "next": "/"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "用户名或密码错误" in response.text
    assert "set-cookie" not in response.headers
    assert WEB_LOGIN_SESSION_COOKIE_NAME not in client.cookies


def test_wrong_username_rerenders_without_a_cookie(
    tmp_path, monkeypatch, password_hash
):
    _enable_login(monkeypatch, password_hash)
    monkeypatch.setattr(
        "telegram_kol_research.web_app.WEB_LOGIN_FAILURE_DELAY_SECONDS", 0.0
    )
    client = _client(tmp_path)

    response = client.post(
        "/login",
        data={"username": "intruder", "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "set-cookie" not in response.headers


def test_failed_login_logs_the_client_ip_without_the_password(
    tmp_path, monkeypatch, password_hash
):
    _enable_login(monkeypatch, password_hash)
    monkeypatch.setattr(
        "telegram_kol_research.web_app.WEB_LOGIN_FAILURE_DELAY_SECONDS", 0.0
    )
    client = _client(tmp_path)
    # The app configures its own logging, so attach directly to the module
    # logger rather than relying on propagation to caplog's root handler.
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collector(level=logging.WARNING)
    module_logger = logging.getLogger("telegram_kol_research.web_app")
    module_logger.addHandler(handler)
    try:
        client.post(
            "/login",
            data={"username": USERNAME, "password": "wrong", "next": "/"},
            headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
            follow_redirects=False,
        )
    finally:
        module_logger.removeHandler(handler)

    messages = [record.getMessage() for record in records]
    assert any("203.0.113.7" in message for message in messages)
    assert not any("wrong" in message for message in messages)


def test_correct_password_sets_cookie_and_redirects_to_next(
    tmp_path, monkeypatch, password_hash
):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)

    response = client.post(
        "/login",
        data={"username": USERNAME, "password": PASSWORD, "next": "/logs"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/logs"
    cookie_header = response.headers["set-cookie"]
    assert cookie_header.startswith(f"{WEB_LOGIN_SESSION_COOKIE_NAME}=v1.")
    assert "HttpOnly" in cookie_header
    assert "SameSite=lax" in cookie_header
    assert "Path=/" in cookie_header
    assert f"Max-Age={DEFAULT_WEB_LOGIN_SESSION_DAYS * 86400}" in cookie_header
    # http:// in the test client, so no Secure flag is expected here.
    assert "Secure" not in cookie_header


def test_https_forwarded_login_marks_the_cookie_secure(
    tmp_path, monkeypatch, password_hash
):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)

    response = client.post(
        "/login",
        data={"username": USERNAME, "password": PASSWORD, "next": "/"},
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "203.0.113.7",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "Secure" in response.headers["set-cookie"]


def test_authenticated_request_is_served(tmp_path, monkeypatch, password_hash):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)

    client.post(
        "/login",
        data={"username": USERNAME, "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    response = client.get("/logs", headers={"Accept": "text/html"})

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_tampered_cookie_is_rejected(tmp_path, monkeypatch, password_hash):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)
    config = WebLoginConfig(
        username=USERNAME, password_hash=password_hash, session_secret=SECRET
    )
    good = issue_web_login_cookie_value(config, int(time.time()))
    version, expires_at, nonce, signature = good.split(".")
    forged = f"{version}.{int(expires_at) + 86400}.{nonce}.{signature}"

    client.cookies.set(WEB_LOGIN_SESSION_COOKIE_NAME, forged)
    response = client.get("/api/monitor-status")

    assert response.status_code == 401


def test_cookie_signed_with_another_secret_is_rejected(
    tmp_path, monkeypatch, password_hash
):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)
    other = WebLoginConfig(
        username=USERNAME, password_hash=password_hash, session_secret="b" * 64
    )

    client.cookies.set(
        WEB_LOGIN_SESSION_COOKIE_NAME,
        issue_web_login_cookie_value(other, int(time.time())),
    )
    response = client.get("/api/monitor-status")

    assert response.status_code == 401


def test_expired_cookie_is_rejected(tmp_path, monkeypatch, password_hash):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)
    config = WebLoginConfig(
        username=USERNAME, password_hash=password_hash, session_secret=SECRET
    )
    stale = issue_web_login_cookie_value(
        config, int(time.time()) - config.session_max_age_seconds - 10
    )

    client.cookies.set(WEB_LOGIN_SESSION_COOKIE_NAME, stale)
    response = client.get("/api/monitor-status")

    assert response.status_code == 401


@pytest.mark.parametrize("garbage", ["", "not-a-cookie", "v1.x.y.z", "v2.1.2.3"])
def test_malformed_cookies_are_rejected(
    tmp_path, monkeypatch, password_hash, garbage
):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)

    client.cookies.set(WEB_LOGIN_SESSION_COOKIE_NAME, garbage)
    response = client.get("/api/monitor-status")

    assert response.status_code == 401


def test_cookie_is_renewed_once_past_the_halfway_mark(
    tmp_path, monkeypatch, password_hash
):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)
    config = WebLoginConfig(
        username=USERNAME, password_hash=password_hash, session_secret=SECRET
    )
    now = int(time.time())
    # Issued so that only a quarter of its lifetime remains.
    aging = issue_web_login_cookie_value(
        config, now - (config.session_max_age_seconds * 3) // 4
    )

    client.cookies.set(WEB_LOGIN_SESSION_COOKIE_NAME, aging)
    response = client.get("/api/monitor-status")

    assert response.status_code == 200
    assert WEB_LOGIN_SESSION_COOKIE_NAME in response.headers.get("set-cookie", "")
    renewed = response.headers["set-cookie"].split("=", 1)[1].split(";")[0]
    assert verify_web_login_cookie_value(config, renewed, now) > now + (
        config.session_max_age_seconds // 2
    )


def test_fresh_cookie_is_not_renewed(tmp_path, monkeypatch, password_hash):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)
    config = WebLoginConfig(
        username=USERNAME, password_hash=password_hash, session_secret=SECRET
    )

    client.cookies.set(
        WEB_LOGIN_SESSION_COOKIE_NAME,
        issue_web_login_cookie_value(config, int(time.time())),
    )
    response = client.get("/api/monitor-status")

    assert response.status_code == 200
    assert WEB_LOGIN_SESSION_COOKIE_NAME not in response.headers.get("set-cookie", "")


# --------------------------------------------------------------------------
# loopback exemption
# --------------------------------------------------------------------------


def test_direct_loopback_without_forwarded_for_is_exempt(
    tmp_path, monkeypatch, password_hash
):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path, client=("127.0.0.1", 50000))

    response = client.get("/api/monitor-status")

    assert response.status_code == 200


def test_loopback_with_forwarded_for_is_not_exempt(
    tmp_path, monkeypatch, password_hash
):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path, client=("127.0.0.1", 50000))

    response = client.get(
        "/api/monitor-status", headers={"X-Forwarded-For": "203.0.113.9"}
    )

    assert response.status_code == 401


def test_non_loopback_client_is_not_exempt(tmp_path, monkeypatch, password_hash):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path, client=("203.0.113.9", 50000))

    response = client.get("/api/monitor-status")

    assert response.status_code == 401


# --------------------------------------------------------------------------
# next-path handling and logout
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/logs", "/logs"),
        ("/strategy-records?page=2", "/strategy-records?page=2"),
        ("", "/"),
        (None, "/"),
        ("//evil.example.com/", "/"),
        ("https://evil.example.com/", "/"),
        ("http://evil.example.com/", "/"),
        ("/\\evil.example.com", "/"),
        ("evil.example.com", "/"),
    ],
)
def test_next_path_is_restricted_to_same_site_paths(raw, expected):
    assert sanitize_web_login_next(raw) == expected


def test_offsite_next_falls_back_to_root_on_login(
    tmp_path, monkeypatch, password_hash
):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)

    response = client.post(
        "/login",
        data={
            "username": USERNAME,
            "password": PASSWORD,
            "next": "https://evil.example.com/",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_login_page_carries_the_next_path_into_the_form(
    tmp_path, monkeypatch, password_hash
):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)

    page = client.get("/login?next=/logs")

    assert 'name="next" value="/logs"' in page.text


def test_already_authenticated_login_page_redirects(
    tmp_path, monkeypatch, password_hash
):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)
    client.post(
        "/login",
        data={"username": USERNAME, "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )

    response = client.get("/login?next=/logs", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/logs"


def test_logout_clears_the_cookie_and_relocks_the_site(
    tmp_path, monkeypatch, password_hash
):
    _enable_login(monkeypatch, password_hash)
    client = _client(tmp_path)
    client.post(
        "/login",
        data={"username": USERNAME, "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )

    logout = client.post("/logout", follow_redirects=False)

    assert logout.status_code == 302
    assert logout.headers["location"] == "/login"
    assert client.get("/api/monitor-status").status_code == 401


# --------------------------------------------------------------------------
# CLI helper
# --------------------------------------------------------------------------


def test_cli_hash_helper_round_trips_a_piped_password():
    from typer.testing import CliRunner

    from telegram_kol_research.cli import app as cli_app

    result = CliRunner().invoke(
        cli_app, ["web-login-password-hash"], input=PASSWORD + "\n"
    )

    assert result.exit_code == 0
    printed = result.stdout.strip().splitlines()[-1]
    assert printed.startswith("scrypt$")
    assert verify_web_login_password(PASSWORD, printed) is True
    assert verify_web_login_password("something-else", printed) is False
    assert PASSWORD not in printed


def test_cli_hash_helper_rejects_an_empty_password():
    from typer.testing import CliRunner

    from telegram_kol_research.cli import app as cli_app

    result = CliRunner().invoke(cli_app, ["web-login-password-hash"], input="\n")

    assert result.exit_code == 1
