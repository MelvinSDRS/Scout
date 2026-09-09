"""Dependency-free laptop launcher; Facebook and Playwright stay on the Linux server."""

import argparse
import json
import queue
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

READY_PREFIX = "SCOUT_LOGIN_READY "


def handoff_path(identifier):
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ValueError("Invalid login handoff identifier")
    return f"/tmp/scout-login-{identifier}/viewer.sock"


def remote_command(directory, identifier, setup=False):
    # SSH joins arguments into a shell command: quote the directory explicitly.
    if directory.startswith("~/"):
        directory = '"$HOME"/' + shlex.quote(directory[2:])
    else:
        directory = shlex.quote(directory)
    command = (
        f"cd -- {directory} || exit 1; "
        "if [ ! -x .venv/bin/scout ]; then "
        "if command -v uv >/dev/null 2>&1; then uv sync --frozen; "
        'elif [ -x "$HOME/.local/bin/uv" ]; then "$HOME/.local/bin/uv" sync --frozen; '
        'else echo "Install uv on the server, then retry login." >&2; exit 1; fi; '
        "[ -x .venv/bin/scout ] || exit 1; fi; "
        f"exec .venv/bin/scout login --handoff {shlex.quote(identifier)}"
    )
    return command + (" --setup" if setup else "")


def viewer_url(port, password):
    if not isinstance(password, str) or not re.fullmatch(r"[a-f0-9]{8}", password):
        raise RuntimeError("The server returned an invalid login handoff. Update Scout and retry.")
    # noVNC reads fragments; the temporary credential is never in HTTP requests/logs.
    return f"http://127.0.0.1:{port}/vnc.html#autoconnect=true&resize=scale&password={password}"


def read_output(stream, events, output):
    """Hide protocol credentials while forwarding even non-newline sudo prompts immediately."""
    prefix = ""
    normal = False
    try:
        while char := stream.read(1):
            if normal:
                output.write(char)
                output.flush()
                if char == "\n":
                    normal = False
                continue
            prefix += char
            if prefix.startswith(READY_PREFIX):
                if char == "\n":
                    try:
                        event = json.loads(prefix[len(READY_PREFIX) :])
                        events.put(("ready", event["password"]))
                    except (ValueError, KeyError, TypeError):
                        events.put(("error", "Invalid login handoff. Update Scout on the server."))
                    prefix = ""
                elif len(prefix) > 1024:
                    events.put(("error", "Invalid login handoff from server."))
                    return
            elif not READY_PREFIX.startswith(prefix):
                output.write(prefix)
                output.flush()
                normal = char != "\n"
                prefix = ""
    finally:
        events.put(("closed", None))


def wait_viewer(process, port, timeout=20):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "SSH disconnected before the viewer opened. Check its output and retry."
            )
        try:
            with opener.open(f"http://127.0.0.1:{port}/vnc.html", timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    raise RuntimeError(
        "The SSH viewer tunnel did not become ready. Check SSH forwarding permissions."
    )


def connect(host, directory="Scout", setup=False, ssh_port=None, ssh_config=None):
    if not host or host.startswith("-") or any(c.isspace() for c in host):
        raise RuntimeError("Use a user@server destination or an SSH config alias.")
    if not shutil.which("ssh"):
        raise RuntimeError("Install the OpenSSH client on this computer, then retry.")
    identifier = secrets.token_hex(16)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command = [
        "ssh",
        "-tt",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-L",
        f"127.0.0.1:{port}:{handoff_path(identifier)}",
    ]
    if ssh_port:
        command += ["-p", str(ssh_port)]
    if ssh_config:
        command += ["-F", str(ssh_config)]
    command += ["--", host, remote_command(directory, identifier, setup)]
    print("Connecting to Scout. Complete any SSH or setup prompts in this terminal.", flush=True)
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"
    )
    events = queue.Queue()
    reader = threading.Thread(
        target=read_output, args=(process.stdout, events, sys.stdout), daemon=True
    )
    reader.start()
    opened = False
    # Includes initial installation and the server login timeout.
    deadline = time.monotonic() + 3600
    try:
        while True:
            if time.monotonic() >= deadline:
                raise RuntimeError("Remote login timed out. Run the command again to continue.")
            try:
                event, value = events.get(timeout=0.2)
            except queue.Empty:
                if process.poll() is not None and not reader.is_alive():
                    break
                continue
            if event == "error":
                raise RuntimeError(value)
            if event == "closed":
                break
            if event == "ready" and not opened:
                url = viewer_url(port, value)
                wait_viewer(process, port)
                print(
                    "Opening Facebook login in your browser. Finish signing in there.", flush=True
                )
                try:
                    browser_opened = webbrowser.open(url, new=2)
                except (webbrowser.Error, OSError):
                    browser_opened = False
                if not browser_opened:
                    print(f"Open this temporary login link on this computer:\n{url}", flush=True)
                opened = True
        status = process.wait(timeout=10)
        if status != 0:
            raise RuntimeError(f"Remote login ended with status {status}. See the message above.")
        if not opened:
            raise RuntimeError(
                "The server did not start a login viewer. Update Scout there and retry."
            )
        print("Login complete. The SSH tunnel is closed.")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        reader.join(timeout=2)
        if not reader.is_alive():
            process.stdout.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open your Scout server's Facebook login locally")
    parser.add_argument("host", help="user@server or your usual SSH alias")
    parser.add_argument("--directory", default="Scout", help="Server checkout (default: ~/Scout)")
    parser.add_argument("--setup", action="store_true", help="Reinstall server login prerequisites")
    parser.add_argument("--ssh-port", type=int, help="SSH port (otherwise uses your SSH config)")
    parser.add_argument("--ssh-config", help="Use a specific OpenSSH configuration file")
    args = parser.parse_args(argv)
    if args.ssh_port is not None and not 1 <= args.ssh_port <= 65535:
        parser.error("SSH port must be between 1 and 65535")
    try:
        connect(args.host, args.directory, args.setup, args.ssh_port, args.ssh_config)
    except KeyboardInterrupt:
        parser.exit(130, "Login cancelled; the connection is closed.\n")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
