import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


@dataclass
class Settings:
    data_dir: Path
    telegram_token: str = ""
    telegram_chat: str = ""
    api_token: str = ""
    facebook_cdp: str = ""
    image_references: Path | None = None
    scan_delay: int = 30
    telegram_thread: int | None = None
    bind_hosts: tuple[str, ...] = ("127.0.0.1",)
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver")
    trusted_proxies: tuple[str, ...] = ("127.0.0.1",)

    @classmethod
    def load(cls):
        env = {**dotenv_values(".env"), **os.environ}
        # Sharing a bot configuration is opt-in; never poll its updates.
        shared_file = env.get("SCOUT_TELEGRAM_ENV_FILE") or env.get("HOMEBOT_ENV_FILE")
        shared = dotenv_values(shared_file) if shared_file else {}

        def entries(key, default):
            return tuple(
                value.strip() for value in env.get(key, default).split(",") if value.strip()
            )

        return cls(
            bind_hosts=entries("SCOUT_BIND_HOSTS", "127.0.0.1"),
            allowed_hosts=entries("SCOUT_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver"),
            trusted_proxies=entries("SCOUT_TRUSTED_PROXIES", "127.0.0.1"),
            data_dir=Path(env.get("SCOUT_DATA", "data")).resolve(),
            telegram_token=env.get("TELEGRAM_BOT_TOKEN") or shared.get("TELEGRAM_BOT_TOKEN") or "",
            telegram_chat=env.get("TELEGRAM_CHAT_ID") or shared.get("TELEGRAM_CHAT_ID") or "",
            api_token=env.get("SCOUT_API_TOKEN", ""),
            facebook_cdp=env.get("FACEBOOK_CDP_URL", ""),
            image_references=Path(env["SCOUT_IMAGE_REFERENCES"]).expanduser().resolve()
            if env.get("SCOUT_IMAGE_REFERENCES")
            else None,
            scan_delay=max(10, int(env.get("SCAN_DELAY_SECONDS", "30"))),
            telegram_thread=int(env["TELEGRAM_MESSAGE_THREAD_ID"])
            if env.get("TELEGRAM_MESSAGE_THREAD_ID")
            else None,
        )

    @property
    def reference_root(self):
        return self.image_references or self.data_dir / "image-references"

    def prepare(self):
        if self.telegram_thread is not None and self.telegram_thread <= 0:
            raise ValueError("TELEGRAM_MESSAGE_THREAD_ID must be positive")
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(self.data_dir / "browsers"))
        if not self.api_token:
            import secrets

            token_file = self.data_dir / "api-token"
            try:
                with token_file.open("x") as stream:
                    token_file.chmod(0o600)
                    stream.write(secrets.token_urlsafe(32))
            except FileExistsError:
                pass
            self.api_token = token_file.read_text().strip()
        if len(self.api_token) < 24:
            raise ValueError("SCOUT_API_TOKEN must have at least 24 characters")
