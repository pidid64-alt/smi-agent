"""FastAPI-приложение: безопасные заголовки, единый формат ошибок, статический интерфейс, публичная раздача медиа по токену."""

from __future__ import annotations

import logging
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from ..config import Settings, get_settings
from ..container import Container
from ..core.errors import AppError
from ..db.models import MediaAsset, PlatformVersion, Publication
from ..security.redaction import install_log_redaction, redact
from .deps import RateLimiter

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"


def create_app(ctx: Container | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or (ctx.settings if ctx else get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        install_log_redaction()
        c = ctx or Container(settings)
        c.db.create_all()
        app.state.ctx = c
        worker = None
        if settings.demo_mode and settings.is_production:
            raise RuntimeError("Демо-режим (SMI_DEMO_MODE) запрещён в production")
        if settings.demo_mode:
            from ..demo.seed import seed_demo_if_empty

            seed_demo_if_empty(c)
        if settings.embedded_worker:
            from ..ops.worker import Worker

            worker = Worker(c)
            threading.Thread(target=worker.run_forever, name="smi-worker", daemon=True).start()
        yield
        if worker:
            worker.stop()
        if ctx is None:
            c.close()

    app = FastAPI(
        title="Smi-Agent API", version="0.1.0", lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None, redoc_url=None, openapi_url="/openapi.json" if settings.docs_enabled else None,
    )  # fmt: skip
    app.state.limiter = RateLimiter()
    app.state.trust_proxy = settings.trust_proxy
    if ctx is not None:
        app.state.ctx = ctx

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        rid = request.headers.get("x-request-id", "")[:40] or uuid.uuid4().hex[:16]
        try:
            resp: Response = await call_next(request)
        except Exception:  # noqa: BLE001
            log.exception("unhandled error rid=%s path=%s", rid, request.url.path)
            resp = JSONResponse({"error": {"code": "internal", "message": "Внутренняя ошибка сервера", "details": {"request_id": rid}}}, status_code=500)
        h = resp.headers
        h["X-Request-ID"] = rid
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "no-referrer"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        h["Content-Security-Policy"] = CSP
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        if settings.is_production:
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path.startswith("/api"):
            h["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(AppError)
    async def app_error(_request: Request, exc: AppError):
        headers = {"Retry-After": str(exc.details.get("retry_after_s", 60))} if exc.status == 429 else None
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, "details": exc.details}}, status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        fields = [{"field": ".".join(str(x) for x in e["loc"][1:]), "message": redact(str(e["msg"]))[:200]} for e in exc.errors()[:10]]
        return JSONResponse({"error": {"code": "validation", "message": "Некорректные данные запроса", "details": {"fields": fields}}}, status_code=422)

    from .routers import auth, control, editorial, insights, project

    for r in (auth, project, editorial, control, insights):
        app.include_router(r.router, prefix="/api")

    @app.get("/media/{name}")
    def public_media(name: str, request: Request):
        """Публичная раздача медиа по неугадываемому токену — нужна Instagram Graph API (она скачивает image_url). Только для публикаций в работе."""
        token = name.rsplit(".", 1)[0]
        c: Container = request.app.state.ctx
        with c.db.read() as s:
            asset = s.scalars(select(MediaAsset).where(MediaAsset.public_token == token)).first()
            if asset is None:
                return JSONResponse({"error": {"code": "not_found", "message": "Файл не найден"}}, status_code=404)
            live = False
            vids = [v for v in s.scalars(select(PlatformVersion).where(PlatformVersion.is_current.is_(True)))]
            ver_ids = [v.id for v in vids if any(m.get("asset_id") == asset.id for m in (v.media or []))]
            if ver_ids:
                live = s.scalars(select(Publication).where(Publication.platform_version_id.in_(ver_ids), Publication.state.in_(["scheduled", "publishing", "published"]))).first() is not None
            if not live:
                return JSONResponse({"error": {"code": "not_found", "message": "Файл не найден"}}, status_code=404)
            path = (Path(c.settings.media_dir) / asset.storage_path).resolve()
            if Path(c.settings.media_dir).resolve() not in path.parents or not path.exists():
                return JSONResponse({"error": {"code": "not_found", "message": "Файл не найден"}}, status_code=404)
            return FileResponse(path, media_type=asset.mime, headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/api/health/live")
    def live():
        return {"status": "ok"}

    if STATIC.exists():
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def app_factory() -> FastAPI:  # для uvicorn --factory
    return create_app()
