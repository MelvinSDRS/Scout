import io
import json
import queue
import subprocess
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from scout.login_client import (
    READY_PREFIX,
    connect,
    handoff_path,
    read_output,
    remote_command,
    viewer_url,
)
from scout.login_handoff import handoff_socket, pause_service


def test_protocol_is_filtered_and_prompts_are_immediate():
    events = queue.Queue()
    output = io.StringIO()
    data = "Connecting\r\n[sudo] password: " + "\r\n" + READY_PREFIX
    data += json.dumps({"password": "1234abcd"}) + "\r\nDone\r\n"
    read_output(io.StringIO(data), events, output)
    assert events.get() == ("ready", "1234abcd")
    assert events.get() == ("closed", None)
    assert output.getvalue() == "Connecting\r\n[sudo] password: \r\nDone\r\n"
    assert "1234abcd" not in output.getvalue()


def test_sudo_prompt_is_not_buffered_until_newline():
    events = queue.Queue()
    output = io.StringIO()
    read_output(io.StringIO("[sudo] password: "), events, output)
    assert output.getvalue() == "[sudo] password: "


@pytest.mark.parametrize("payload", ["[]", "{}", "not json", '{"password": "' + "x" * 1100])
def test_bad_handoff_is_not_echoed(payload):
    events = queue.Queue()
    output = io.StringIO()
    read_output(io.StringIO(READY_PREFIX + payload + "\n"), events, output)
    assert events.get()[0] == "error"
    assert not output.getvalue()


@pytest.mark.parametrize("value", ["../unsafe", "a" * 31, "/tmp/socket", "A" * 32])
def test_handoff_rejects_paths(value):
    with pytest.raises(ValueError):
        handoff_path(value)


def test_socket_directory_is_private_and_not_reused():
    import secrets

    identifier = secrets.token_hex(16)
    with handoff_socket(identifier) as path:
        assert path.parent.stat().st_mode & 0o777 == 0o700
        with pytest.raises(FileExistsError):
            with handoff_socket(identifier):
                pass
        path.touch()
    assert not path.parent.exists()


def test_viewer_credential_is_fragment_only():
    url = urlsplit(viewer_url(45678, "1234abcd"))
    assert url.hostname == "127.0.0.1"
    assert not url.query
    assert "password=1234abcd" in url.fragment
    for password in ("bad", None, 1, "abcd&abc"):
        with pytest.raises(RuntimeError):
            viewer_url(45678, password)


def test_remote_directory_is_shell_quoted(tmp_path):
    directory = tmp_path / "Scout $(touch INJECTED); with spaces"
    (directory / ".venv/bin").mkdir(parents=True)
    executable = directory / ".venv/bin/scout"
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    executable.chmod(0o700)
    result = subprocess.run(
        ["sh", "-c", remote_command(str(directory), "a" * 32)],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
    )
    assert result.stdout.splitlines() == ["login", "--handoff", "a" * 32]
    assert not (tmp_path / "INJECTED").exists()
    assert '"$HOME"/' in remote_command("~/Scout folder", "a" * 32)


@pytest.mark.parametrize(
    "active,own,fail,expected",
    [
        (True, True, False, ["stop", "start"]),
        (True, True, True, ["stop", "start"]),
        (True, False, False, []),
        (False, True, False, []),
    ],
)
def test_service_restores_only_this_checkout(tmp_path, monkeypatch, active, own, fail, expected):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path if own else tmp_path / "another"
    stdout = f"ActiveState={'active' if active else 'inactive'}\nWorkingDirectory={directory}\n"
    monkeypatch.setattr("scout.login_handoff.shutil.which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(
        "scout.login_handoff.subprocess.run",
        Mock(return_value=subprocess.CompletedProcess([], 0, stdout, "")),
    )
    actions = []
    monkeypatch.setattr("scout.login_handoff.service_command", actions.append)
    if fail:
        with pytest.raises(RuntimeError):
            with pause_service():
                raise RuntimeError("login failed")
    else:
        with pause_service():
            pass
    assert actions == expected


def test_connect_opens_viewer_and_does_not_print_credential(monkeypatch, capsys):
    process = Mock()
    process.stdout = io.StringIO(READY_PREFIX + '{"password":"1234abcd"}\nVerified\n')
    process.poll.return_value = 0
    process.wait.return_value = 0
    popen = Mock(return_value=process)
    opened = Mock(return_value=True)
    monkeypatch.setattr("scout.login_client.subprocess.Popen", popen)
    monkeypatch.setattr("scout.login_client.wait_viewer", Mock())
    monkeypatch.setattr("scout.login_client.webbrowser.open", opened)
    connect("my-server", "/srv/Scout folder")
    command = popen.call_args.args[0]
    assert command.count("ssh") == 1
    assert "ExitOnForwardFailure=yes" in command
    assert "ControlPath=none" in command
    tunnel = command[command.index("-L") + 1]
    assert tunnel.startswith("127.0.0.1:")
    assert tunnel.endswith("/viewer.sock")
    assert "password=1234abcd" in opened.call_args.args[0]
    assert "1234abcd" not in capsys.readouterr().out


def test_connect_terminates_ssh_when_tunnel_fails(monkeypatch):
    process = Mock()
    process.stdout = io.StringIO(READY_PREFIX + '{"password":"1234abcd"}\n')
    process.poll.return_value = None
    monkeypatch.setattr("scout.login_client.subprocess.Popen", Mock(return_value=process))
    monkeypatch.setattr(
        "scout.login_client.wait_viewer", Mock(side_effect=RuntimeError("no tunnel"))
    )
    with pytest.raises(RuntimeError, match="no tunnel"):
        connect("my-server")
    process.terminate.assert_called_once()
    process.wait.assert_called_once()


def test_disconnect_cancels_browser_work(monkeypatch):
    import asyncio
    import os
    from types import SimpleNamespace

    from scout.login_handoff import login_until_disconnect

    read_fd, write_fd = os.pipe()
    cleaned = []

    async def browser(settings):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    monkeypatch.setattr("scout.login.browser_login", browser)
    monkeypatch.setattr("scout.login_handoff.sys.stdin", SimpleNamespace(fileno=lambda: read_fd))
    os.close(write_fd)
    try:
        with pytest.raises(RuntimeError, match="SSH disconnected"):
            asyncio.run(login_until_disconnect(None))
    finally:
        os.close(read_fd)
    assert cleaned == [True]


def test_service_restoration_survives_closed_terminal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stdout = f"ActiveState=active\nWorkingDirectory={tmp_path}\n"
    monkeypatch.setattr("scout.login_handoff.shutil.which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(
        "scout.login_handoff.subprocess.run",
        Mock(return_value=subprocess.CompletedProcess([], 0, stdout, "")),
    )
    actions = []
    monkeypatch.setattr("scout.login_handoff.service_command", actions.append)
    with pause_service():
        monkeypatch.setattr("builtins.print", Mock(side_effect=OSError("terminal closed")))
    assert actions == ["stop", "start"]


def test_dashboard_protocol_hides_sign_in_code():
    output = io.StringIO()
    events = queue.Queue()
    read_output(
        io.StringIO(READY_PREFIX + json.dumps({"kind": "dashboard", "ticket": "x" * 43}) + "\n"),
        events,
        output,
    )
    assert events.get() == ("dashboard", "x" * 43)
    assert output.getvalue() == ""


def test_dashboard_remote_command_keeps_worker_running():
    command = remote_command("Scout", "a" * 32, dashboard=True)
    assert command.endswith("exec .venv/bin/scout open --handoff")
    assert " login " not in command and "systemctl" not in command


def test_sigterm_during_driver_start_is_cleaned_before_event_loop_exits(tmp_path, monkeypatch):
    import asyncio
    import os
    import signal
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from scout.login_handoff import login_until_disconnect
    from scout.providers.facebook import Facebook
    from scout.settings import Settings

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr("scout.login_handoff.sys.stdin", SimpleNamespace(fileno=lambda: read_fd))
    previous = signal.getsignal(signal.SIGTERM)

    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()
        runtime = SimpleNamespace(stop=AsyncMock())

        async def start():
            started.set()
            await release.wait()
            return runtime

        async def browser(settings):
            fb = Facebook(settings)
            try:
                await fb.start()
            finally:
                await fb.close()

        monkeypatch.setattr(
            "playwright.async_api.async_playwright", lambda: SimpleNamespace(start=start)
        )
        monkeypatch.setattr("scout.login.browser_login", browser)
        task = asyncio.create_task(login_until_disconnect(Settings(tmp_path)))
        await started.wait()
        os.kill(os.getpid(), signal.SIGTERM)
        asyncio.get_running_loop().call_later(0.02, release.set)
        with pytest.raises(RuntimeError, match="SSH disconnected"):
            await asyncio.wait_for(task, timeout=2)
        runtime.stop.assert_awaited_once()

    try:
        asyncio.run(exercise())
        assert signal.getsignal(signal.SIGTERM) == previous
    finally:
        os.close(read_fd)
        os.close(write_fd)
