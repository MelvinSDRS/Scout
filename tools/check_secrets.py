"""Scan publishable current files without transmitting suspected credentials."""

import json
import os
import subprocess
import sys
from pathlib import Path

PRIVATE_DIRS = {
    "data",
    ".venv",
    ".git",
    ".agents",
    ".codex",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "dist",
    "build",
}


def private_path(path):
    p = Path(path)
    return (
        any(part in PRIVATE_DIRS for part in p.parts)
        or (p.name.startswith(".env") and p.name != ".env.example")
        or p.suffix.lower()
        in {".db", ".sqlite", ".sqlite3", ".har", ".log", ".pem", ".key", ".pyc"}
        or ".sqlite3" in p.name
    )


def public_files(root):
    paths = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in PRIVATE_DIRS]
        for name in files:
            path = Path(base) / name
            if not private_path(path.relative_to(root)):
                paths.append(path)
    return sorted(paths)


def scan_paths(paths):
    command = Path(sys.executable).with_name("detect-secrets")
    result = subprocess.run(
        [str(command), "scan", "--all-files", "--no-verify", *map(str, paths)],
        capture_output=True,
        text=True,
        check=True,
    )
    findings = json.loads(result.stdout)["results"]
    for name, entries in findings.items():
        for entry in entries:
            # Never print raw or hashed credentials to logs.
            print(f"Potential secret: {name}:{entry['line_number']} ({entry['type']})")
    return not findings


def main():
    root = Path.cwd()
    tracked = subprocess.run(["git", "ls-files", "-z"], capture_output=True)
    if tracked.returncode == 0:
        private = [p for p in tracked.stdout.decode().split("\0") if p and private_path(p)]
        if private:
            for p in private:
                print(f"Private/generated file is tracked: {p}")
            raise SystemExit(1)
    else:
        print("No accessible Git index; scanning current files only. History is not checked.")
    paths = public_files(root)
    if not scan_paths(paths):
        raise SystemExit(1)
    print(f"Secret scan passed for {len(paths)} current files; network verification disabled.")


if __name__ == "__main__":
    main()
