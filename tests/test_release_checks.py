from pathlib import Path

import pytest


@pytest.fixture
def checker(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    import check_release

    return check_release


def wheel_members():
    root = Path(__file__).resolve().parents[1]
    members = {
        "scout-0.1.0.dist-info/METADATA": b"Name: scout\nLicense-Expression: MIT\nDescription-Content-Type: text/markdown\n",
        "scout-0.1.0.dist-info/licenses/LICENSE": (root / "LICENSE").read_bytes(),
        "scout-0.1.0.dist-info/licenses/docs/nationwide-LICENSE.txt": (
            root / "docs/nationwide-LICENSE.txt"
        ).read_bytes(),
    }
    for name in ("app.js", "style.css", "index.html", "regions.json"):
        members["scout/" + name] = b"fixture"
    return members


def test_release_requires_both_project_and_upstream_notices(checker):
    valid = wheel_members()
    checker.validate_members(valid, wheel=True)
    for name in ("LICENSE", "docs/nationwide-LICENSE.txt"):
        altered = dict(valid)
        del altered["scout-0.1.0.dist-info/licenses/" + name]
        with pytest.raises(ValueError, match="Missing license"):
            checker.validate_members(altered, wheel=True)


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "docs/.env.production",
        "data/api-token",
        "scout.sqlite3-wal",
        "docs/session.har",
        "docs/secret.key",
        "docs/reference.jpg",
        "../escape",
        "/tmp/escape",
    ],
)
def test_release_rejects_private_files_and_unsafe_paths(checker, name):
    members = wheel_members()
    members[name] = b"fixture"
    with pytest.raises(ValueError):
        checker.validate_members(members, wheel=True)
