"""Render Scout's user service for this checkout; never start or restart it implicitly."""

import argparse
import os
from pathlib import Path


def quote_path(path, *, executable=False):
    value = str(path)
    if any(ord(char) < 32 for char in value):
        raise ValueError("Service paths cannot contain control characters")
    value = value.replace("%", "%%")
    if executable:
        value = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
        return '"' + value + '"'
    if "\\" in value:
        raise ValueError("Working-directory paths with backslashes are unsupported")
    # WorkingDirectory consumes the entire value and does not accept shell quotes.
    return value


def render(root):
    root = root.resolve()
    executable = root / ".venv/bin/scout"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("Run uv sync in the Scout checkout first")
    return (
        (root / "deploy/scout.service")
        .read_text()
        .replace("@SCOUT_ROOT@", quote_path(root))
        .replace("@SCOUT_EXECUTABLE@", quote_path(executable, executable=True))
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    parser.add_argument("--output", type=Path, default=config_home / "systemd/user/scout.service")
    args = parser.parse_args()
    try:
        unit = render(args.root)
    except ValueError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(unit)
    print(f"Wrote {args.output}")
    print("Activate with: systemctl --user daemon-reload && systemctl --user enable --now scout")


if __name__ == "__main__":
    main()
