"""FastAPI backend for the Aurum dashboard.

    python src/api/server.py                 # http://127.0.0.1:8000  (docs at /docs)

Endpoints:
    GET  /api/status        bot state, cached account metrics, last pass
    POST /api/control       {"action": "start|pause|resume|stop|toggle", "dry_run": true, "interval_seconds": 60}
    GET  /api/credentials   MT5 login/server from .env, password masked
    POST /api/credentials   update .env (blank or masked password keeps the stored one)
    GET  /                  the dashboard (single page, polls the API every 3 s)
    GET  /api/positions     open positions + pending orders for the bot's symbol and magic number
    POST /api/positions/{ticket}/close   close a bot position / cancel a bot pending order
    GET  /api/signal        last setup result and ICT detector stats
    GET  /api/reports       list generated Aurum reports
    GET  /reports/{name}    serve one report

Security: binds to 127.0.0.1 by default. If ``AURUM_API_TOKEN`` is set, the
control, credential and close endpoints require ``Authorization: Bearer <token>``.
Set a token before exposing the server beyond localhost.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Literal

if __name__ == "__main__":  # allow `python src/api/server.py`
    _root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(_root / "src"), str(_root)]

from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from aurum.capturer import tradingview_symbol  # noqa: E402
from api.state import (  # noqa: E402
    DEFAULT_ENV_PATH,
    DEFAULT_REPORTS_DIR,
    BotStateManager,
    CredentialStore,
    NotConnectedError,
    StateError,
)
from config.settings import Settings, load_settings  # noqa: E402
from mt5.executor import TicketNotFoundError  # noqa: E402

logger = logging.getLogger(__name__)

DASHBOARD_HTML = Path(__file__).resolve().parent / "templates" / "dashboard.html"
REPORT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*\.html$")


class ControlRequest(BaseModel):
    action: Literal["start", "pause", "resume", "stop", "toggle"]
    dry_run: bool = True  # only used when the action starts the bot
    interval_seconds: float | None = Field(default=None, gt=0, le=86_400)


class CredentialsRequest(BaseModel):
    login: int | None = Field(default=None, gt=0)
    server: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=128)
    path: str | None = Field(default=None, max_length=512)


def create_app(
    state: BotStateManager | None = None,
    credentials: CredentialStore | None = None,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings_loader: Callable[[], Settings] = load_settings,
    api_token: str | None = None,
) -> FastAPI:
    """Build the app. Every dependency is injectable for tests."""
    state = state or BotStateManager()
    credentials = credentials or CredentialStore(DEFAULT_ENV_PATH)
    reports_dir = Path(reports_dir)
    token = (api_token if api_token is not None else os.getenv("AURUM_API_TOKEN", "")).strip() or None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        state.stop(timeout=10)  # never leave the scan thread running after shutdown

    app = FastAPI(title="Aurum MT5 Trader API", version="1.0.0", lifespan=lifespan)
    app.state.bot = state

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if token is None:
            return
        expected = f"Bearer {token}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Missing or invalid API token")

    def trading_info() -> dict[str, Any]:
        try:
            settings = settings_loader()
            t = settings.trading
            return {"symbol": t.symbol, "magic_number": t.magic_number, "risk_percent": t.risk_percent,
                    "allow_live_trading": t.allow_live_trading,
                    "tv_symbol": tradingview_symbol(t.symbol, settings.charts.exchange)}
        except ValueError as exc:  # malformed .env
            return {"settings_error": str(exc)}

    # ------------------------------------------------------------ dashboard
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))

    # ------------------------------------------------------------ status / control
    @app.get("/api/status")
    def get_status() -> dict[str, Any]:
        return {**state.snapshot(), "trading": trading_info(), "server_time": dt.datetime.now(dt.timezone.utc).isoformat()}

    @app.post("/api/control", dependencies=[Depends(require_token)])
    def control(req: ControlRequest) -> dict[str, Any]:
        try:
            if req.action == "start":
                return state.start(req.dry_run, req.interval_seconds)
            if req.action == "toggle":
                return state.toggle(req.dry_run, req.interval_seconds)
            if req.action == "pause":
                return state.pause()
            if req.action == "resume":
                return state.resume()
            return state.stop()
        except StateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ------------------------------------------------------------ credentials
    @app.get("/api/credentials", dependencies=[Depends(require_token)])
    def get_credentials() -> dict[str, Any]:
        return credentials.read()

    @app.post("/api/credentials", dependencies=[Depends(require_token)])
    def update_credentials(req: CredentialsRequest) -> dict[str, Any]:
        try:
            saved = credentials.update(req.login, req.server, req.password, req.path)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        running = state.snapshot()["status"] not in ("stopped", "error")
        return {**saved, "restart_required": running}

    # ------------------------------------------------------------ trading data
    @app.get("/api/positions")
    def get_positions() -> dict[str, Any]:
        try:
            return state.positions()
        except NotConnectedError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/positions/{ticket}/close", dependencies=[Depends(require_token)])
    def close_ticket(ticket: int) -> dict[str, Any]:
        try:
            result = state.close_ticket(ticket)
        except NotConnectedError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TicketNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not result["success"]:
            raise HTTPException(status_code=502, detail=f"Broker rejected the request: {result['message']}")
        return result

    @app.get("/api/signal")
    def get_signal() -> dict[str, Any]:
        return state.signal_view()

    # ------------------------------------------------------------ reports
    @app.get("/api/reports")
    def list_reports() -> dict[str, Any]:
        files = sorted(reports_dir.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True) \
            if reports_dir.is_dir() else []
        return {"reports": [
            {
                "name": f.name,
                "url": f"/reports/{f.name}",
                "size_bytes": f.stat().st_size,
                "modified": dt.datetime.fromtimestamp(f.stat().st_mtime, tz=dt.timezone.utc).isoformat(),
            }
            for f in files if REPORT_NAME.match(f.name)
        ]}

    @app.get("/reports/{name}")
    def get_report(name: str) -> FileResponse:
        if not REPORT_NAME.match(name):
            raise HTTPException(status_code=404, detail="Report not found")
        path = (reports_dir / name).resolve()
        if path.parent != reports_dir.resolve() or not path.is_file():
            raise HTTPException(status_code=404, detail="Report not found")
        return FileResponse(path, media_type="text/html")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    host = os.getenv("AURUM_API_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost") and not os.getenv("AURUM_API_TOKEN"):
        logger.warning("Serving on %s without AURUM_API_TOKEN: anyone on the network can control the bot", host)
    uvicorn.run(app, host=host, port=int(os.getenv("AURUM_API_PORT", "8000")))
