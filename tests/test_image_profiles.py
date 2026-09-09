import asyncio

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from scout.api import create_app
from scout.image_filter import PhotoFilter
from scout.image_profiles import available_profiles, get_profile
from scout.models import Listing, SearchResult, SearchSpec, Watch
from scout.providers.facebook import REGIONS
from scout.searches import Searches
from scout.settings import Settings
from scout.store import Store


def reference(root, name="camera", color="red"):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "front.jpg"
    Image.new("RGB", (16, 16), color).save(path)
    return path


def test_only_valid_profiles_are_advertised(tmp_path):
    assert available_profiles(tmp_path) == []
    reference(tmp_path)
    (tmp_path / "empty").mkdir()
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad/front.jpg").write_text("not a photo")
    assert available_profiles(tmp_path) == [{"id": "camera", "label": "Camera"}]
    first = get_profile(tmp_path, "camera")
    reference(tmp_path, color="blue")
    assert get_profile(tmp_path, "camera").fingerprint != first.fingerprint
    for n in range(5):
        Image.new("RGB", (16, 16)).save(tmp_path / "camera" / f"{n}.jpg")
    assert available_profiles(tmp_path) == []


@pytest.mark.parametrize("ident", ["../camera", "/tmp/camera", "", "A", "x" * 65])
def test_profile_ids_cannot_escape_reference_root(ident, tmp_path):
    assert get_profile(tmp_path, ident) is None
    with pytest.raises(ValidationError):
        SearchSpec(query="camera", countries=["FR"], image_profile=ident)


def test_api_rejects_unavailable_profiles_including_saved_search_conversion(tmp_path):
    settings = Settings(tmp_path, api_token="x" * 32)
    store = Store(tmp_path / "test.db")
    with TestClient(create_app(store, settings, start_worker=False)) as client:
        client.headers["Authorization"] = "Bearer " + settings.api_token
        payload = {
            "name": "camera",
            "query": "camera",
            "countries": ["FR"],
            "image_profile": "camera",
        }
        assert client.get("/api/health").json()["image_profiles"] == []
        assert client.post("/api/searches", json=payload).status_code == 422
        assert client.post("/api/watches", json=payload).status_code == 422
        image = reference(settings.reference_root)
        assert client.get("/api/health").json()["image_profiles"] == [
            {"id": "camera", "label": "Camera"}
        ]
        search = client.post("/api/searches", json=payload)
        assert search.status_code == 202
        image.unlink()
        ident = search.json()["id"]
        assert client.post(f"/api/searches/{ident}/watch", json={}).status_code == 422
        assert store.watches() == []
        assert Searches(store).get(ident)["spec"]["image_profile"] == "camera"
        assert client.get("/api/health").json()["image_profiles"] == []


def test_changed_or_missing_references_do_not_reuse_old_matches(tmp_path, monkeypatch):
    settings = Settings(tmp_path)
    ref = reference(settings.reference_root)
    store = Store(tmp_path / "test.db")
    watch = Watch(name="camera", query="camera", countries=["FR"], image_profile="camera")
    store.add(watch, REGIONS)
    scan = store.due(1)[0]
    photos = PhotoFilter(store, settings)
    item = Listing(
        "facebook",
        "1",
        "camera",
        "https://facebook.com/marketplace/item/1",
        "FR",
        image_url="https://a.fbcdn.net/1.jpg",
    )
    calls = []

    async def inspect(item, profile):
        calls.append(profile.fingerprint)
        return {"decision": "match" if len(calls) == 1 else "review", "inliers": 20}

    monkeypatch.setattr(photos, "inspect", inspect)

    async def run():
        result = SearchResult([item])
        assert (await photos.filter(scan, watch, result)).listings == [item]
        assert (await photos.filter(scan, watch, result)).listings == [item]
        assert len(calls) == 1
        reference(settings.reference_root, color="blue")
        assert (await photos.filter(scan, watch, result)).listings == []
        assert len(calls) == 2 and calls[0] != calls[1]
        ref.unlink()
        assert (await photos.filter(scan, watch, result)).listings == []
        assert len(calls) == 2
        assert (
            photos.review(scan["watch_id"])["items"][0]["reason"] == "Photo references unavailable"
        )
        assert store.health()["pending_alerts"] == 0

    asyncio.run(run())


def test_photo_subprocess_uses_the_selected_reference_folder(tmp_path, monkeypatch):
    import random

    import httpx

    from scout import image_filter

    settings = Settings(tmp_path / "state", image_references=tmp_path / "custom-references")
    directory = settings.reference_root / "camera"
    directory.mkdir(parents=True)
    image = Image.frombytes("RGB", (160, 160), random.Random(0).randbytes(160 * 160 * 3))
    image.save(directory / "front.jpg")
    data = (directory / "front.jpg").read_bytes()
    profile = get_profile(settings.reference_root, "camera")
    client = httpx.AsyncClient
    monkeypatch.setattr(
        image_filter.httpx,
        "AsyncClient",
        lambda **kwargs: client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data)),
            **kwargs,
        ),
    )
    photos = PhotoFilter(Store(tmp_path / "test.db"), settings)
    item = Listing(
        "facebook",
        "1",
        "camera",
        "https://facebook.com/marketplace/item/1",
        "FR",
        image_url="https://a.fbcdn.net/1.jpg",
    )
    result = asyncio.run(photos.inspect(item, profile))
    assert result["decision"] == "match"
    assert result["inliers"] >= 12
