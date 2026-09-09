from scout.settings import Settings


def test_scout_settings_preserve_private_deployment_and_access_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "state"
    data.mkdir()
    token = "test-" + "x" * 32
    (data / "api-token").write_text(token)
    shared = tmp_path / "shared.env"
    # Synthetic env values; the detector mistakes the field names for high-entropy data.
    shared.write_text(
        "TELEGRAM_BOT_TOKEN=test-bot\nTELEGRAM_CHAT_ID=test-chat\n"  # pragma: allowlist secret
    )
    (tmp_path / ".env").write_text(
        f"SCOUT_DATA={data}\nHOMEBOT_ENV_FILE={shared}\n"
        "SCOUT_BIND_HOSTS=127.0.0.1,172.20.0.1\n"
        "SCOUT_ALLOWED_HOSTS=localhost,scout.example.com\n"
        "SCOUT_TRUSTED_PROXIES=127.0.0.1,172.20.0.2\n"
    )
    for key in (
        "SCOUT_DATA",
        "SCOUT_API_TOKEN",
        "SCOUT_BIND_HOSTS",
        "SCOUT_ALLOWED_HOSTS",
        "SCOUT_TRUSTED_PROXIES",
        "HOMEBOT_ENV_FILE",
        "SCOUT_TELEGRAM_ENV_FILE",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(data / "browsers"))
    settings = Settings.load()
    settings.prepare()
    assert settings.data_dir == data
    assert settings.api_token == token
    assert settings.bind_hosts == ("127.0.0.1", "172.20.0.1")
    assert settings.allowed_hosts == ("localhost", "scout.example.com")
    assert settings.trusted_proxies == ("127.0.0.1", "172.20.0.2")
    assert (settings.telegram_token, settings.telegram_chat) == ("test-bot", "test-chat")
    monkeypatch.setenv("SCOUT_API_TOKEN", "override-" + "x" * 24)
    assert Settings.load().api_token == "override-" + "x" * 24


def test_shared_telegram_settings_are_opt_in(tmp_path, monkeypatch):
    from scout import settings as module

    monkeypatch.chdir(tmp_path)
    for key in (
        "SCOUT_TELEGRAM_ENV_FILE",
        "HOMEBOT_ENV_FILE",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    reads = []

    def read(path):
        reads.append(path)
        return {} if path == ".env" else {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test"}

    monkeypatch.setattr(module, "dotenv_values", read)
    assert Settings.load().telegram_token == ""
    assert reads == [".env"]
    monkeypatch.setenv("SCOUT_TELEGRAM_ENV_FILE", "shared.env")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "override")
    configured = Settings.load()
    assert configured.telegram_token == "test"
    assert configured.telegram_chat == "override"
    assert reads[-1] == "shared.env"


def test_custom_image_reference_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCOUT_IMAGE_REFERENCES", "my-references")
    assert Settings.load().reference_root == tmp_path / "my-references"
    monkeypatch.delenv("SCOUT_IMAGE_REFERENCES")
    settings = Settings.load()
    assert settings.reference_root == settings.data_dir / "image-references"
