import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from scout.login import (
    available_port,
    browser_login,
    check_session,
    login,
    remote_display,
    remote_needed,
    viewer_instructions,
    wait_for_session,
)
from scout.login_setup import install_login, run_install
from scout.settings import Settings


def test_ssh_selects_remote_display(tmp_path, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    settings = Settings(tmp_path)
    assert remote_needed(settings)
    monkeypatch.setenv("DISPLAY", ":1")
    assert not remote_needed(settings)
    assert remote_needed(settings, force=True)
    monkeypatch.delenv("DISPLAY")
    settings.facebook_cdp = "http://127.0.0.1:9222"
    assert not remote_needed(settings)


def test_missing_runtime_explains_fix(tmp_path, monkeypatch):
    monkeypatch.setattr("scout.login.remote_tools", lambda settings: ("", "", ["Xvfb"]))
    with pytest.raises(RuntimeError, match="scout login --setup --remote"):
        with remote_display(Settings(tmp_path)):
            pass


def test_remote_refuses_existing_cdp(tmp_path):
    with pytest.raises(RuntimeError, match="Unset FACEBOOK_CDP_URL"):
        with remote_display(Settings(tmp_path, facebook_cdp="http://127.0.0.1:9222")):
            pass


def test_explicit_occupied_port_is_not_silently_changed():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        with pytest.raises(RuntimeError, match="busy"):
            available_port(listener.getsockname()[1])


def test_default_port_falls_back(monkeypatch):
    sock = Mock()
    sock.bind.side_effect = [OSError(), None]
    sock.getsockname.return_value = ("127.0.0.1", 45678)
    context = Mock(__enter__=Mock(return_value=sock), __exit__=Mock(return_value=False))
    monkeypatch.setattr("scout.login.socket.socket", lambda: context)
    assert available_port() == 45678
    assert sock.bind.call_args_list[-1].args == (("127.0.0.1", 0),)


def test_tunnel_command_handles_alias_and_keeps_password_out_of_url(capsys):
    viewer_instructions(6091, "testonly", "my-server")
    output = capsys.readouterr().out
    assert "-L 6091:127.0.0.1:6091 -- my-server" in output
    assert "ExitOnForwardFailure=yes" in output
    assert "6091/vnc.html?autoconnect=true&resize=scale" in output
    assert "password=" not in output


def test_setup_uses_settings_browser_path_and_current_python(tmp_path, monkeypatch):
    import sys

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "custom-browsers"))
    commands = []
    monkeypatch.setattr("scout.login_setup.run_install", commands.append)
    monkeypatch.setattr("scout.login_setup.remote_tools", lambda _: ("", "", ["Xvfb"]))
    monkeypatch.setattr("scout.login_setup.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr("scout.login_setup.os.geteuid", lambda: 1000)
    install_login(Settings(tmp_path), remote=True)
    assert commands == [
        ["sudo", "apt-get", "update"],
        ["sudo", "apt-get", "install", "-y", "xvfb", "xauth", "x11vnc", "novnc"],
        [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
    ]


def test_setup_skips_existing_remote_packages(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr("scout.login_setup.run_install", commands.append)
    monkeypatch.setattr("scout.login_setup.remote_tools", lambda _: ("vnc", "novnc", []))
    install_login(Settings(tmp_path), remote=True)
    assert len(commands) == 1
    assert "playwright" in commands[0]


def test_setup_failure_has_recovery_command(monkeypatch):
    monkeypatch.setattr("scout.login_setup.subprocess.run", Mock(side_effect=OSError()))
    with pytest.raises(RuntimeError, match="rerun scout login --setup"):
        run_install(["missing-command"])


def test_setup_only_does_not_open_browser(tmp_path, monkeypatch):
    install = Mock()
    browser = Mock(side_effect=AssertionError("browser must not open"))
    monkeypatch.setattr("scout.login.install_login", install)
    monkeypatch.setattr("scout.login.browser_login", browser)
    login(Settings(tmp_path), force_remote=True, setup_only=True)
    install.assert_called_once_with(Settings(tmp_path), remote=True)
    browser.assert_not_called()


def test_login_lock_prevents_installation(tmp_path, monkeypatch):
    from scout.worker import worker_lock

    install = Mock()
    monkeypatch.setattr("scout.login.install_login", install)
    with worker_lock(tmp_path), pytest.raises(RuntimeError, match="systemctl --user stop scout"):
        login(Settings(tmp_path), setup=True)
    install.assert_not_called()


def test_check_works_without_remote_runtime(tmp_path, monkeypatch):
    check = AsyncMock()
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("scout.login.check_session", check)
    monkeypatch.setattr("scout.login.remote_display", Mock(side_effect=AssertionError()))
    login(Settings(tmp_path), check=True)
    check.assert_awaited_once()


@pytest.mark.parametrize("verification_fails", [False, True])
def test_save_and_verify_before_resuming_scans(tmp_path, monkeypatch, verification_fails):
    events = []
    page = SimpleNamespace(is_closed=lambda: False, close=AsyncMock())

    async def close():
        events.append("save")

    async def verify(settings):
        events.append("verify")
        if verification_fails:
            raise RuntimeError("verification failed")

    fb = SimpleNamespace(close=close)
    monkeypatch.setattr("scout.login.Facebook", lambda settings: fb)
    monkeypatch.setattr("scout.login.open_marketplace", AsyncMock(return_value=page))
    monkeypatch.setattr("scout.login.wait_for_session", AsyncMock())
    monkeypatch.setattr("scout.login.check_session", verify)
    monkeypatch.setattr(
        "scout.login.Store",
        lambda path: SimpleNamespace(resume_source=lambda source: events.append("resume")),
    )
    if verification_fails:
        with pytest.raises(RuntimeError, match="verification failed"):
            asyncio.run(browser_login(Settings(tmp_path)))
        assert events == ["save", "verify"]
    else:
        asyncio.run(browser_login(Settings(tmp_path)))
        assert events == ["save", "verify", "resume"]
    page.close.assert_awaited_once()


def test_timeout_and_closed_browser_are_actionable(monkeypatch):
    page = SimpleNamespace(is_closed=lambda: False)
    monkeypatch.setattr("scout.login.session_ready", AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(wait_for_session(page, timeout=0.01))
    page.is_closed = lambda: True
    with pytest.raises(RuntimeError, match="browser closed"):
        asyncio.run(wait_for_session(page))


def test_failed_check_closes_its_tab_and_connection(tmp_path, monkeypatch):
    page = SimpleNamespace(is_closed=lambda: False, close=AsyncMock())
    fb = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr("scout.login.Facebook", lambda settings: fb)
    monkeypatch.setattr("scout.login.open_marketplace", AsyncMock(return_value=page))
    monkeypatch.setattr("scout.login.wait_for_session", AsyncMock(side_effect=RuntimeError()))
    with pytest.raises(RuntimeError, match="could not be verified"):
        asyncio.run(check_session(Settings(tmp_path)))
    page.close.assert_awaited_once()
    fb.close.assert_awaited_once()


def test_setup_cli_loads_custom_data_directory_from_dotenv(tmp_path, monkeypatch):
    import os
    import sys

    from scout.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SCOUT_DATA", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("FACEBOOK_CDP_URL", raising=False)
    monkeypatch.setenv("SCOUT_API_TOKEN", "synthetic-test-api-token-only")
    (tmp_path / ".env").write_text("SCOUT_DATA=custom-data\n")
    installed = []

    def install(settings, remote):
        installed.append((settings.data_dir, os.environ["PLAYWRIGHT_BROWSERS_PATH"], remote))

    monkeypatch.setattr("scout.login.install_login", install)
    monkeypatch.setattr(sys, "argv", ["scout", "login", "--setup-only", "--remote"])
    main()
    assert installed == [(tmp_path / "custom-data", str(tmp_path / "custom-data/browsers"), True)]
    assert not (tmp_path / "data").exists()


def test_cancellation_during_playwright_start_stops_the_driver(tmp_path, monkeypatch):
    from scout.providers.facebook import Facebook

    async def exercise():
        started = asyncio.Event()
        release = asyncio.Event()
        runtime = SimpleNamespace(stop=AsyncMock())

        async def start():
            started.set()
            await release.wait()
            return runtime

        monkeypatch.setattr(
            "playwright.async_api.async_playwright", lambda: SimpleNamespace(start=start)
        )
        fb = Facebook(Settings(tmp_path))
        task = asyncio.create_task(fb.start())
        await started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        runtime.stop.assert_awaited_once()
        assert fb.playwright is None

    asyncio.run(exercise())
