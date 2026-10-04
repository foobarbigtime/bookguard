from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .static_assets import RevalidatedStaticFiles
from .auth import BasicAuthMiddleware
from .config import settings
from .request_origin import SameOriginMiddleware
from .db import init_local_db, load_persisted_settings
from .routes.automatic import router as automatic_router
from .routes.hardlink_conflicts import router as hardlink_router
from .routes.pages import router as pages_router
from .routes.repairs import router as repairs_router
from .routes.system import router as system_router
from .routes.triage import router as triage_router
from .routes.verification import router as verification_router
from .change_log import record_app_start
from .import_watch import init_import_watch_db, watcher as import_watcher
from .scheduler import init_scheduler_db, scheduler
from .scanner import start_scan
from .triage import init_triage_db
from .verifier import init_verification_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_local_db()
    init_triage_db()
    init_verification_db()
    record_app_start()
    settings.apply(load_persisted_settings())
    if settings.scan_on_start:
        start_scan()
    init_import_watch_db()
    import_watcher.start()
    init_scheduler_db()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()
        import_watcher.stop()


app = FastAPI(title="BookGuard", version=__version__, lifespan=lifespan)
app.add_middleware(BasicAuthMiddleware)
# Added last so it runs first: reject cross-site writes before anything else.
app.add_middleware(SameOriginMiddleware)
app.mount("/static", RevalidatedStaticFiles(directory="static"), name="static")
app.include_router(pages_router)
app.include_router(system_router)
app.include_router(repairs_router)
app.include_router(triage_router)
app.include_router(verification_router)
app.include_router(automatic_router)
app.include_router(hardlink_router)
