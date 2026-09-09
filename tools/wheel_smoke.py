"""Exercise an installed wheel with temporary state and no external collectors."""

import asyncio
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

import httpx

import scout
from scout.api import create_app
from scout.settings import Settings
from scout.store import Store


async def main():
    assert Path(scout.__file__).is_relative_to(Path(sys.prefix)), (
        "Use the installed wheel, not an editable checkout"
    )
    with tempfile.TemporaryDirectory() as directory:
        settings = Settings(Path(directory), api_token="wheel-smoke-" + "x" * 24)
        store = Store(settings.data_dir / "scout.sqlite3")
        app = create_app(store, settings, start_worker=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            for route in ("/", "/app.js", "/style.css"):
                assert (await client.get(route)).status_code == 200
            assert (await client.get("/api/health")).status_code == 401
            response = await client.get(
                "/api/health", headers={"Authorization": "Bearer " + settings.api_token}
            )
            assert response.status_code == 200
            assert response.json()["image_profiles"] == []
        assert files("scout").joinpath("regions.json").is_file()
    print("Installed wheel: resources, API authentication and empty-state health passed.")


if __name__ == "__main__":
    asyncio.run(main())
