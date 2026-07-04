from __future__ import annotations

import asyncio
import hashlib
import hmac
from contextlib import asynccontextmanager
from importlib import resources
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from mdv.auth import Entitlements, basic_credentials
from mdv.collection import CollectionService, collection_json
from mdv.config import Settings
from mdv.db import SQLiteStore
from mdv.resolution import MappingResolveRequest, MappingResolveResponse


def _query_filters(request: Request) -> dict[str, object]:
    query = request.query_params

    def value(name: str, default=None):
        return query.get(name.upper()) or query.get(name.lower()) or default

    def values(name: str) -> list[str]:
        collected = [*query.getlist(name.upper()), *query.getlist(name.lower())]
        result = []
        for raw in collected:
            for item in raw.split(","):
                normalized = item.strip().upper()
                if normalized and normalized not in result:
                    result.append(normalized)
        return result

    result: dict[str, object] = {
        "limit": value("LIMIT", 5000),
        "offset": value("OFFSET", 0),
    }
    for query_name, filter_name in (
        ("TYPE", "type"),
        ("PRODUCT", "product"),
        ("CONTRACT", "contract"),
        ("EXPIRY", "expiry"),
        ("DIRECTION", "direction"),
        ("FUTURES", "futures"),
        ("STOCK", "stock"),
        ("TAG", "tags"),
        ("VENUE", "venue"),
        ("QUOTE", "quote"),
        ("SETTLE", "settle"),
        ("SYMBOL", "symbol"),
        ("STATUS", "status"),
        ("ACTIVE", "active"),
    ):
        result[filter_name] = values(query_name)
        result[f"{filter_name}_not"] = values(f"{query_name}!")
    return result


def _canonical_mdv_query(request: Request) -> str:
    return "&".join(
        f"{quote(key, safe='!')}={quote(value, safe='')}"
        for key, value in request.query_params.multi_items()
        if key and value
    )


def _log_query_filters(request: Request) -> dict[str, object]:
    query = request.query_params

    def value(name: str, default=None):
        return query.get(name) or query.get(name.lower()) or default

    return {
        "limit": int(value("LIMIT", 100)),
        "offset": int(value("OFFSET", 0)),
        "venue": value("VENUE"),
        "action": value("ACTION"),
        "tag": value("TAG"),
        "symbol": value("SYMBOL"),
        "product": value("PRODUCT"),
        "date_from": value("DATE_FROM"),
        "date_to": value("DATE_TO"),
    }


def create_app(
    *,
    settings: Settings | None = None,
    store: SQLiteStore | None = None,
    entitlements: Entitlements | None = None,
) -> FastAPI:
    settings = settings or Settings.from_yaml()
    store = store or SQLiteStore(settings.db_path)
    entitlements = entitlements or Entitlements.load(settings.entitlements_path)
    templates = Jinja2Templates(directory=str(resources.files("mdv").joinpath("templates")))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        store.migrate()
        # Re-apply the current generic matcher to stored raw observations on
        # every deployment, even when no exchange refresh is requested.
        store.rebuild_symbol_matches()
        should_refresh = settings.refresh_on_startup == "always" or (
            settings.refresh_on_startup == "if-empty" and store.market_count() == 0
        )
        if should_refresh:
            await CollectionService(store, timeout_seconds=settings.http_timeout_seconds).collect_all()
        yield

    app = FastAPI(title="Asset Master Data", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.settings = settings
    app.state.entitlements = entitlements
    basic_auth_cache: dict[bytes, str] = {}

    @app.middleware("http")
    async def require_authentication(request: Request, call_next):
        if request.url.path in {"/login", "/favicon.ico"}:
            return await call_next(request)

        username = None
        credentials = basic_credentials(request.headers.get("authorization"))
        if credentials:
            cache_key = hmac.new(
                entitlements.session_secret,
                f"{credentials[0]}\0{credentials[1]}".encode("utf-8"),
                hashlib.sha256,
            ).digest()
            username = basic_auth_cache.get(cache_key)
            if username is None and await asyncio.to_thread(
                entitlements.authenticate, *credentials
            ):
                username = credentials[0]
                if len(basic_auth_cache) >= 128:
                    basic_auth_cache.pop(next(iter(basic_auth_cache)))
                basic_auth_cache[cache_key] = username
        if username is None:
            session = request.cookies.get(settings.session_cookie_name, "")
            username = entitlements.session_username(session)
        if username is None:
            if request.method == "GET" and not request.url.path.startswith("/api/") and request.url.path != "/health":
                next_path = request.url.path
                if request.url.query:
                    next_path += f"?{request.url.query}"
                return RedirectResponse(f"/login?next={quote(next_path, safe='')}", status_code=303)
            return JSONResponse(
                {"detail": "authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="asset-master-data"'},
            )
        request.state.auth_username = username
        return await call_next(request)

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_page(request: Request):
        next_path = _safe_next(request.query_params.get("next"))
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"next_path": next_path, "error": None},
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(
            status_code=204,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.post("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login(request: Request):
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        next_path = _safe_next(form.get("next", ["/mdv"])[0])
        if not entitlements.authenticate(username, password):
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"next_path": next_path, "error": "Invalid username or password."},
                status_code=401,
            )
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            settings.session_cookie_name,
            entitlements.issue_session(username, settings.session_ttl_seconds),
            max_age=settings.session_ttl_seconds,
            httponly=True,
            secure=settings.session_cookie_secure,
            samesite="strict",
        )
        return response

    @app.post("/logout", include_in_schema=False)
    async def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(settings.session_cookie_name)
        return response

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/mdv")

    @app.get("/health")
    async def health():
        return {"status": "ok", "markets": store.market_count()}

    @app.get("/api/v1/markets")
    async def api_markets(request: Request):
        try:
            rows = store.list_markets(_query_filters(request))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"count": len(rows), "markets": rows}

    @app.get("/api/v1/assets")
    async def api_assets(request: Request):
        try:
            return store.list_assets(_query_filters(request))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/v1/mappings/resolve",
        response_model=MappingResolveResponse,
        response_model_exclude_none=True,
    )
    def api_resolve_mappings(payload: MappingResolveRequest):
        return store.resolve_venue_mappings(
            source=payload.source.model_dump(exclude={"symbols"}),
            target=payload.target.model_dump(),
            symbols=payload.source.symbols,
        )

    @app.get("/api/v1/stats")
    async def api_stats():
        return store.stats()

    @app.get("/api/v1/logs")
    async def api_logs(request: Request):
        try:
            return store.list_collection_runs(**_log_query_filters(request))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/metadata")
    async def api_metadata():
        return store.filter_metadata()

    @app.get("/metadata", response_class=HTMLResponse, include_in_schema=False)
    async def metadata(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="metadata.html",
            context={"metadata": store.filter_metadata()},
        )

    @app.post("/api/v1/refresh")
    async def api_refresh(request: Request):
        venue = request.query_params.get("VENUE") or request.query_params.get("venue")
        try:
            results = await CollectionService(store, timeout_seconds=settings.http_timeout_seconds).collect(
                venue=venue,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return collection_json(results, scope=str(venue or "ALL").strip().upper())

    @app.post("/mdv/refresh", include_in_schema=False)
    async def refresh_page(request: Request):
        venue = request.query_params.get("VENUE") or request.query_params.get("venue")
        try:
            await CollectionService(store, timeout_seconds=settings.http_timeout_seconds).collect(
                venue=str(venue) if venue else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse("/logs", status_code=303)

    @app.get("/logs", response_class=HTMLResponse, include_in_schema=False)
    async def logs(request: Request):
        try:
            log_filters = _log_query_filters(request)
            collection_log = store.list_collection_runs(**log_filters)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request=request,
            name="logs.html",
            context={
                "collection_log": collection_log,
                "filters": {
                    "action": str(log_filters["action"] or "").upper(),
                    "tag": str(log_filters["tag"] or "").upper(),
                    "venue": str(log_filters["venue"] or "").upper(),
                    "symbol": str(log_filters["symbol"] or "").upper(),
                    "product": str(log_filters["product"] or "").upper(),
                    "date_from": log_filters["date_from"] or "",
                    "date_to": log_filters["date_to"] or "",
                },
            },
        )

    @app.get("/mdv", response_class=HTMLResponse)
    async def mdv(request: Request):
        raw_query = request.scope.get("query_string", b"").decode("latin-1")
        canonical_query = _canonical_mdv_query(request)
        if raw_query != canonical_query:
            location = "/mdv" + (f"?{canonical_query}" if canonical_query else "")
            return RedirectResponse(location)
        filters = _query_filters(request)
        try:
            asset_view = store.list_assets(filters)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request=request,
            name="mdv.html",
            context={
                "asset_view": asset_view,
                "filters": filters,
                "filter_metadata": store.filter_metadata()["filters"],
                "stats": store.stats(),
            },
        )

    return app


def _safe_next(value: str | None) -> str:
    candidate = str(value or "/mdv")
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/mdv"
    return candidate
