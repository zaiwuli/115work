import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock
from pydantic import ValidationError
from sqlalchemy import select, text

from app.adapters.p115 import DisabledAdapter, FakeP115Adapter, P115Adapter
from app.api.routes import router
from app.api.management import router as management_router
from app.persistence.models import Connection
from app.persistence.db import Database
from app.runtime.config import Config
from app.runtime.secrets import SecretStore
from app.runtime.request_limits import RequestLimitMiddleware
from app.scheduler.engine import Engine


ROOT = Path(__file__).resolve().parent.parent


def migrate(db):
    config = AlembicConfig(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    with db.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def create_app(config: Config | None = None, adapter=None) -> FastAPI:
    config = config or Config()

    @asynccontextmanager
    async def lifespan(app):
        config.prepare()
        lock = FileLock(str(config.data_dir / "config" / "owner.lock"), timeout=0)
        lock.acquire()
        try:
            if config.db_path.exists():
                backups = config.data_dir / "config/backups"
                backups.mkdir(exist_ok=True)
                with sqlite3.connect(config.db_path) as source:
                    with sqlite3.connect(backups / f"startup-{time.time_ns()}.db") as target:
                        source.backup(target)
            db = Database(config.db_path)
            migrate(db)
            key_path = config.data_dir / "config/master.key"
            if not key_path.exists():
                with db.session() as session:
                    existing_credentials = session.scalar(select(Connection).where(Connection.credential != ""))
                if db.settings().get("passwords") or existing_credentials:
                    raise RuntimeError("MASTER_KEY_MISSING_RESTORE_BACKUP")
            secret_store = SecretStore(key_path)
            token_path = config.data_dir / "config/admin-token"
            token = config.api_token.get_secret_value()
            if not token and not config.local_no_auth:
                if not token_path.exists():
                    descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "w", encoding="ascii") as output:
                        output.write(secrets.token_urlsafe(32))
                token = token_path.read_text(encoding="ascii").strip()
            config.adapter = db.settings().get("adapter_mode", config.adapter)
            if adapter is not None:
                selected = adapter
            elif config.adapter == "p115":
                cookie = config.cookie.get_secret_value()
                if not cookie:
                    with db.session() as session:
                        connection = session.scalar(select(Connection).where(
                            Connection.kind == "115", Connection.enabled,
                        ))
                        if connection:
                            cookie = secret_store.decrypt(connection.credential)
                selected = P115Adapter(cookie, config.remote_timeout) if cookie else DisabledAdapter()
            elif config.adapter == "fake":
                selected = FakeP115Adapter()
            elif config.adapter == "disabled":
                selected = DisabledAdapter()
            else:
                raise ValueError("UNKNOWN_ADAPTER_MODE")
            engine = Engine(db, config, selected, secret_store)
            app.state.db, app.state.config, app.state.secrets = db, config, secret_store
            app.state.engine, app.state.api_token = engine, token
            app.state.login_attempts = {}
            engine.start()
            app.state.ready = True
            try:
                yield
            finally:
                app.state.ready = False
                await engine.stop()
                close = getattr(engine.adapter, "close", None)
                if close:
                    await close()
                db.engine.dispose()
        finally:
            lock.release()

    app = FastAPI(title="115work", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestLimitMiddleware)

    @app.middleware("http")
    async def request_guard(request: Request, call_next):
        request_id = secrets.token_hex(12)
        request.state.request_id = request_id
        size = request.headers.get("content-length")
        if size and (not size.isdigit() or int(size) > 5 * 1024 * 1024):
            return JSONResponse({"error": {"code": "REQUEST_SIZE_LIMIT"}}, status_code=413)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        code = str(exc) if str(exc).isupper() and len(str(exc)) < 100 else "INVALID_INPUT"
        return JSONResponse({"error": {"code": code}}, status_code=422)

    @app.exception_handler(ValidationError)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Don't return rejected input; it may contain credentials.
        return JSONResponse({"error": {"code": "VALIDATION_ERROR"}}, status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({"error": {"code": exc.detail}}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        logging.getLogger("115work").error("request=%s error=INTERNAL_ERROR", request.state.request_id)
        return JSONResponse({"error": {"code": "INTERNAL_ERROR"}}, status_code=500)

    @app.get("/health")
    @app.get("/health/live")
    def live():
        return {"status": "live", "version": "0.1.0"}

    @app.get("/health/ready")
    def ready():
        if not getattr(app.state, "ready", False):
            raise HTTPException(503, detail="NOT_READY")
        with app.state.db.session() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ready", "dependency_mode": config.adapter}

    app.include_router(router)
    app.include_router(management_router)
    frontend = ROOT / "frontend/dist"
    if (frontend / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")
    if (frontend / "icons").is_dir():
        app.mount("/icons", StaticFiles(directory=frontend / "icons"), name="icons")

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    @app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
    def apple_icon():
        return FileResponse(frontend / "apple-touch-icon.png", media_type="image/png")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(frontend / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/")
    def index():
        if (frontend / "index.html").is_file():
            return FileResponse(frontend / "index.html")
        return JSONResponse({"status": "frontend_not_built", "health": "/health/ready"}, status_code=503)

    return app


app = create_app()
