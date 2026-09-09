import asyncio
import secrets
import time
from contextlib import asynccontextmanager, suppress
from importlib.resources import files
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .image_filter import PhotoFilter
from .image_profiles import available_profiles, get_profile
from .models import SearchSpec, Watch
from .providers.facebook import REGIONS
from .searches import Searches
from .worker import run


class WatchOptions(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    interval_minutes: int = Field(default=60, ge=30, le=10080)


def create_app(store, settings, start_worker=True):
    searches = Searches(store)
    photos = PhotoFilter(store, settings)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(run(store, settings)) if start_worker else None
        app.state.worker = task
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Scout", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    bearer = HTTPBearer(auto_error=False)

    def authorize(
        request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)
    ):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Cross-origin requests are disabled")
        if credentials is None or not secrets.compare_digest(
            credentials.credentials, settings.api_token
        ):
            raise HTTPException(401, "Enter your access token")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return files("scout").joinpath("index.html").read_text()

    @app.get("/app.js")
    def javascript():
        return Response(files("scout").joinpath("app.js").read_text(), media_type="text/javascript")

    @app.get("/style.css")
    def stylesheet():
        return Response(files("scout").joinpath("style.css").read_text(), media_type="text/css")

    def validate_profile(spec):
        if spec.image_profile and get_profile(settings.reference_root, spec.image_profile) is None:
            raise HTTPException(
                422, "Photo references unavailable; configure the profile or use title filters only"
            )

    @app.get("/api/searches", dependencies=[Depends(authorize)])
    def recent_searches():
        return searches.recent()

    @app.post("/api/searches", dependencies=[Depends(authorize)], status_code=202)
    def search(spec: SearchSpec):
        validate_profile(spec)
        try:
            return {"id": searches.submit(spec)}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    def find_search(ident):
        value = searches.get(ident)
        if value is None:
            raise HTTPException(404, "Search not found")
        return value

    @app.get("/api/searches/{ident}", dependencies=[Depends(authorize)])
    def search_status(ident: str):
        return find_search(ident)

    @app.get("/api/searches/{ident}/results", dependencies=[Depends(authorize)])
    def search_results(
        ident: str,
        country: Literal["US", "CA", "FR"],
        suggestions: bool = False,
        offset: int = Query(0, ge=0),
        limit: int = Query(60, ge=1, le=100),
    ):
        find_search(ident)
        return searches.results(ident, country, suggestions, offset, limit)

    @app.post("/api/searches/{ident}/cancel", dependencies=[Depends(authorize)])
    def cancel_search(ident: str):
        if not searches.cancel(ident):
            raise HTTPException(404, "Search not found")
        return {"cancelled": ident}

    @app.post("/api/searches/{ident}/watch", dependencies=[Depends(authorize)], status_code=201)
    def watch_search(ident: str, options: WatchOptions):
        saved = find_search(ident)
        if saved["watch_id"] is None:
            validate_profile(SearchSpec.model_validate(saved["spec"]))
        try:
            return {"id": searches.create_watch(ident, options.name, options.interval_minutes)}
        except KeyError:
            raise HTTPException(404, "Search not found") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/watches", dependencies=[Depends(authorize)])
    def watches():
        return store.watches()

    @app.post("/api/watches", dependencies=[Depends(authorize)], status_code=201)
    def add(watch: Watch):
        validate_profile(watch)
        try:
            return {"id": store.add(watch, REGIONS)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/watches/{ident}/review", dependencies=[Depends(authorize)])
    def photo_review(
        ident: int, offset: int = Query(0, ge=0), limit: int = Query(60, ge=1, le=100)
    ):
        if not any(w["id"] == ident for w in store.watches()):
            raise HTTPException(404, "Watch not found")
        return photos.review(ident, offset, limit)

    @app.post(
        "/api/watches/{ident}/review/{source}/{listing_id}/notify",
        dependencies=[Depends(authorize)],
    )
    def approve_photo(ident: int, source: Literal["facebook"], listing_id: str):
        try:
            photos.approve(ident, source, listing_id)
        except KeyError:
            raise HTTPException(404, "Review item not found") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"queued": listing_id}

    @app.delete("/api/watches/{ident}", dependencies=[Depends(authorize)])
    def remove(ident: int):
        if not store.remove(ident):
            raise HTTPException(404, "Watch not found")
        return {"deleted": ident}

    @app.get("/api/health", dependencies=[Depends(authorize)])
    def health():
        data = store.health()
        for scan in data["scans"]:
            scan["region_name"] = next(
                (
                    region["name"]
                    for region in REGIONS[scan["country"]]
                    if region["city"] == scan["anchor"]
                ),
                "Country search",
            )
        heartbeat = data["worker_heartbeat"]
        task = getattr(app.state, "worker", None)
        data["worker_running"] = bool(
            heartbeat and time.time() - float(heartbeat) < 300 and (task is None or not task.done())
        )
        data["image_profiles"] = available_profiles(settings.reference_root)
        data["telegram_configured"] = bool(settings.telegram_token and settings.telegram_chat)
        data["telegram_topic_url"] = (
            f"https://t.me/c/{str(settings.telegram_chat)[4:]}/{settings.telegram_thread}"
            if settings.telegram_thread and str(settings.telegram_chat).startswith("-100")
            else None
        )
        data["coverage"] = (
            "Regional samples; completeness and Facebook radius enforcement are not guaranteed"
        )
        return data

    return app
