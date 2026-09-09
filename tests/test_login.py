import pytest

from scout.login import remote_display, remote_needed
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
    monkeypatch.setattr("scout.login.shutil.which", lambda cmd: None)
    with pytest.raises(RuntimeError, match="install-login-runtime"):
        with remote_display(Settings(tmp_path)):
            pass


def test_remote_refuses_existing_cdp(tmp_path):
    with pytest.raises(RuntimeError, match="Unset FACEBOOK_CDP_URL"):
        with remote_display(Settings(tmp_path, facebook_cdp="http://127.0.0.1:9222")):
            pass
