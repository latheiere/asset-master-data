from __future__ import annotations

import asyncio
import hashlib
import hmac
from contextlib import asynccontextmanager
from importlib import resources
from urllib.parse import parse_qs, quote, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from mdv import __version__, build_revision
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
        "limit": value("LIMIT", 500),
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
        ("FINANCING", "financing"),
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

    def enabled(name: str) -> bool:
        return str(value(name, "")).strip().lower() in {"1", "true", "yes", "on"}

    return {
        "limit": int(value("LIMIT", 10)),
        "offset": int(value("OFFSET", 0)),
        "venue": value("VENUE"),
        "action": value("ACTION"),
        "tag": value("TAG"),
        "symbol": value("SYMBOL"),
        "product": value("PRODUCT"),
        "date_from": value("DATE_FROM"),
        "date_to": value("DATE_TO"),
        "changed_only": enabled("CHANGES_ONLY"),
    }


def _financing_query_filters(request: Request) -> dict[str, object]:
    query = request.query_params
    return {
        "venue": query.get("VENUE") or query.get("venue"),
        "product": query.get("PRODUCT") or query.get("product"),
        "role": query.get("ROLE") or query.get("role"),
        "symbol": query.get("SYMBOL") or query.get("symbol"),
        "eligible": query.get("ELIGIBLE") or query.get("eligible"),
        "limit": query.get("LIMIT") or query.get("limit") or 5000,
        "offset": query.get("OFFSET") or query.get("offset") or 0,
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

    app = FastAPI(title="Asset Master Data", version=__version__, lifespan=lifespan)
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
        return RedirectResponse("/coverage")

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "version": __version__,
            "revision": build_revision(),
            "markets": store.market_count(),
        }

    @app.get("/api/v1/markets")
    async def api_markets(request: Request):
        try:
            rows = await asyncio.to_thread(store.list_markets, _query_filters(request))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"count": len(rows), "markets": rows}

    @app.get("/api/v1/assets")
    async def api_assets(request: Request):
        try:
            return await asyncio.to_thread(store.list_assets, _query_filters(request))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/financing")
    def api_financing(request: Request):
        try:
            return store.list_financing(_financing_query_filters(request))
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
        return await asyncio.to_thread(store.stats)

    @app.get("/api/v1/logs")
    async def api_logs(request: Request):
        try:
            return await asyncio.to_thread(store.list_collection_runs, **_log_query_filters(request))
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
            context={"metadata": store.filter_metadata(), "active_nav": "metadata"},
        )

    def manual_action_payload(form: dict) -> dict:
        return {
            "action_type": form.get("action_type"),
            "venue": form.get("venue"),
            "source_symbol": form.get("source_symbol"),
            "target_symbol": form.get("target_symbol"),
            "note": form.get("note"),
            "enabled": form.get("enabled") in {"1", "true", "on"},
        }

    async def manual_action_form(request: Request) -> dict[str, str]:
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if content_type != "application/x-www-form-urlencoded":
            raise HTTPException(status_code=415, detail="form must be URL encoded")
        parsed = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        return {name: values[-1] for name, values in parsed.items()}

    @app.get("/manual-actions", response_class=HTMLResponse, include_in_schema=False)
    async def manual_actions(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="manual_actions.html",
            context={
                "actions": store.list_manual_asset_actions(), "error": None,
                "active_nav": "manual",
            },
        )

    @app.post("/manual-actions", response_class=HTMLResponse, include_in_schema=False)
    async def create_manual_action(request: Request):
        form = await manual_action_form(request)
        try:
            store.create_manual_asset_action(manual_action_payload(form))
        except ValueError as exc:
            return templates.TemplateResponse(
                request=request,
                name="manual_actions.html",
                context={
                    "actions": store.list_manual_asset_actions(), "error": str(exc),
                    "active_nav": "manual",
                },
                status_code=422,
            )
        return RedirectResponse("/manual-actions", status_code=303)

    @app.post("/manual-actions/{action_id}", include_in_schema=False)
    async def update_manual_action(action_id: str, request: Request):
        form = await manual_action_form(request)
        try:
            store.update_manual_asset_action(action_id, manual_action_payload(form))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse("/manual-actions", status_code=303)

    @app.post("/manual-actions/{action_id}/delete", include_in_schema=False)
    async def delete_manual_action(action_id: str):
        try:
            store.delete_manual_asset_action(action_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse("/manual-actions", status_code=303)

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
            collection_log = await asyncio.to_thread(store.list_collection_runs, **log_filters)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        def page_url(offset: int) -> str:
            items = [
                (key, value)
                for key, value in request.query_params.multi_items()
                if key.upper() not in {"LIMIT", "OFFSET"}
            ]
            items.extend((("LIMIT", str(log_filters["limit"])), ("OFFSET", str(offset))))
            return "/logs?" + urlencode(items)

        offset = int(log_filters["offset"])
        limit = int(log_filters["limit"])
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
                    "changed_only": log_filters["changed_only"],
                    "limit": limit,
                    "offset": offset,
                },
                "previous_page": page_url(max(0, offset - limit)) if offset else None,
                "next_page": page_url(offset + limit)
                if offset + limit < collection_log["count"] else None,
                "active_nav": "logs",
            },
        )

    async def asset_view_page(request: Request, *, template_name: str):
        raw_query = request.scope.get("query_string", b"").decode("latin-1")
        canonical_query = _canonical_mdv_query(request)
        if raw_query != canonical_query:
            location = request.url.path + (f"?{canonical_query}" if canonical_query else "")
            return RedirectResponse(location)
        filters = _query_filters(request)
        if "LIMIT" not in request.query_params and "limit" not in request.query_params:
            filters["limit"] = 200
        symbol_filters = list(filters["symbol"])
        show_asset_detail = (
            template_name == "mdv.html"
            and len(symbol_filters) == 1
            and "*" not in symbol_filters[0]
        )
        try:
            asset_view = await asyncio.to_thread(
                store.list_assets, filters, include_details=show_asset_detail
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        filter_metadata = await asyncio.to_thread(store.filter_metadata)
        stats = await asyncio.to_thread(store.stats)
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context={
                "asset_view": asset_view,
                "filters": filters,
                "filter_metadata": filter_metadata["filters"],
                "stats": stats,
                "view_path": request.url.path,
                "show_asset_detail": show_asset_detail,
                "active_nav": "coverage" if template_name == "coverage.html" else "assets",
            },
        )

    @app.get("/coverage", response_class=HTMLResponse, include_in_schema=False)
    async def coverage(request: Request):
        return await asset_view_page(request, template_name="coverage.html")

    @app.get("/asset", response_class=HTMLResponse, include_in_schema=False)
    async def asset(request: Request):
        return await asset_view_page(request, template_name="mdv.html")

    @app.get("/mdv", response_class=HTMLResponse)
    async def mdv(request: Request):
        """Backward-compatible alias for the asset explorer."""
        return await asset_view_page(request, template_name="mdv.html")

    return app


def _safe_next(value: str | None) -> str:
    candidate = str(value or "/mdv")
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/mdv"
    return candidate
