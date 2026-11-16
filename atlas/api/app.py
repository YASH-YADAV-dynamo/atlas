"""The Route API: validate (contract models), authenticate, map errors, call core.

Every `/v1/*` call runs as the `Principal` of its bearer key (see atlas/mcp/keys.py) and touches
only that principal's namespace, so REST and the MCP tools of the same key see the same map.
`POST /v1/keys` and `GET /v1/mcp-info` are public.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, cast

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from starlette.applications import Starlette
from starlette.routing import Route

from atlas.contracts.api_models import (
    CHANGES_PATH,
    HEALTH_PATH,
    LOCATE_PATH,
    MAP_PATH,
    PLAN_PATH,
    REPORT_PATH,
    SKILLS_PATH,
    SLEEP_PATH,
    ChangesResponse,
    ErrorBody,
    LocateRequest,
    LocateResponse,
    PlanRequest,
    PlanResponse,
    ReportRequest,
    ReportResponse,
    SkillsResponse,
)
from atlas.contracts.errors import AtlasError, UnauthorizedError, http_status_for
from atlas.contracts.mcp_models import Mode, tools_for
from atlas.contracts.ports import Clock, GraphStore, MapStore, RouteStore
from atlas.contracts.types import MapView, Site, SleepReport
from atlas.core import changes, learner, planner, sleep
from atlas.core.locate import locate
from atlas.core.signature import state_from_snapshot
from atlas.log import log_event
from atlas.mcp.keys import (
    KeyRegistry,
    Principal,
    RateLimitedError,
    RateLimiter,
    TenantError,
    rate_key,
)
from atlas.mcp.server import KEY_PATH_PREFIX, MCP_PATH, build_http_app, tenant_tools_factory
from atlas.settings import Settings

KEYS_PATH = "/v1/keys"
MCP_INFO_PATH = "/v1/mcp-info"


def client_ip(request: Request) -> str:
    """The last X-Forwarded-For entry (appended by our own proxy), else the socket address."""
    forwarded = request.headers.get("x-forwarded-for", "")
    last = forwarded.rsplit(",", 1)[-1].strip()
    if last:
        return last
    return request.client.host if request.client else "unknown"


def public_mcp_url(settings: Settings, request: Request) -> str:
    if settings.atlas_public_url:
        return settings.atlas_public_url.rstrip("/") + MCP_PATH
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}{MCP_PATH}"


def _caller(request: Request, authorization: Annotated[str | None, Header()] = None) -> Principal:
    """The principal of this request's bearer key (the app's own authenticator)."""
    authenticate: Callable[[Request, str | None], Principal] = request.app.state.atlas_authenticate
    return authenticate(request, authorization)


Caller = Annotated[Principal, Depends(_caller)]


def create_app(
    store: GraphStore, clock: Clock, settings: Settings, keys: KeyRegistry | None = None
) -> FastAPI:
    # Both stores implement ChangeStore; the MapStore port the factory returns does not name it yet.
    routes = cast(RouteStore, store)
    skills = cast(sleep.SleepStore, store)
    registry = keys if keys is not None else KeyRegistry(settings)
    limiter = RateLimiter(settings.tenant_requests_per_minute)
    # No admin key configured (local development): unauthenticated calls act as the admin.
    open_mode = not registry.has_admin_key
    anonymous = Principal("anonymous", settings.atlas_namespace, read_only=False, admin=True)

    def authenticate(request: Request, authorization: str | None) -> Principal:
        if open_mode and authorization is None:
            return anonymous
        principal = registry.authenticate_header(authorization)
        if principal is None:
            raise UnauthorizedError("missing or invalid bearer key")
        charged = rate_key(principal, client_ip(request))
        if charged is not None and not limiter.allow(charged):
            raise RateLimitedError("too many requests for this key; slow down")
        return principal

    app = FastAPI(title="Atlas Route API")
    app.state.atlas_authenticate = authenticate
    v1 = APIRouter()

    @app.exception_handler(AtlasError)
    def _atlas_error(_: Request, error: AtlasError) -> JSONResponse:
        body = ErrorBody(code=error.code, message=str(error) or error.code)
        log_event("api_error", code=error.code)
        status = error.status if isinstance(error, TenantError) else http_status_for(error)
        return JSONResponse(status_code=status, content=body.model_dump())

    @app.get(HEALTH_PATH)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ---- public: self-serve keys and connection info ----------------------------------------

    @app.post(KEYS_PATH, status_code=201)
    def post_keys(request: Request) -> dict[str, object]:
        issued = registry.issue(client_ip(request))
        log_event("tenant_created", tenants=registry.tenant_count())
        return {
            "api_key": issued.api_key,
            "namespace": issued.namespace,
            "mcp_url": public_mcp_url(settings, request),
            "read_only": False,
        }

    @app.get(MCP_INFO_PATH)
    def get_mcp_info(request: Request) -> dict[str, object]:
        demo = settings.atlas_demo_mcp_key.get_secret_value() if settings.atlas_demo_mcp_key else None
        return {
            "mcp_url": public_mcp_url(settings, request),
            "demo_key": demo or None,
            "tools": [spec.name for spec in tools_for(Mode.PLANNER)],
        }

    # ---- the Route API, per principal --------------------------------------------------------

    @v1.post(LOCATE_PATH)
    def post_locate(request: LocateRequest, caller: Caller) -> LocateResponse:
        ns = caller.namespace
        if caller.read_only:  # a read-only key never records a newly seen page
            state_id = state_from_snapshot(ns, request.site, request.snapshot).id
            return LocateResponse(state_id=state_id, known=store.get_state(ns, state_id) is not None)
        result = locate(store, ns, request.site, request.snapshot)
        return LocateResponse(state_id=result.state_id, known=result.known)

    @v1.post(PLAN_PATH)
    def post_plan(request: PlanRequest, caller: Caller) -> PlanResponse:
        result = planner.plan(
            store,
            caller.namespace,
            request,
            max_steps=settings.plan_max_steps,
            max_steps_cap=settings.plan_max_steps_cap,
            path_count=settings.plan_path_count,
        )
        return PlanResponse.model_validate(result.model_dump())

    @v1.post(REPORT_PATH)
    def post_report(request: ReportRequest, caller: Caller) -> ReportResponse:
        caller.require_write("report")
        registry.check_event_quota(caller)
        ns = caller.namespace
        result = learner.report(store, clock, ns, request)
        if not result.duplicate:
            registry.note_event(caller)
        change = changes.note_report(routes, clock, ns, result)
        if change is not None:
            log_event("flow_change", transition_id=change.transition_id)
        return ReportResponse(state_id=result.state_id, known=result.known, duplicate=result.duplicate)

    @v1.get(MAP_PATH)
    def get_map(site: Site, caller: Caller) -> MapView:
        return store.get_map(caller.namespace, site)

    @v1.get(CHANGES_PATH)
    def get_changes(site: Site, caller: Caller) -> ChangesResponse:
        return ChangesResponse(site=site, changes=routes.list_changes(caller.namespace, site))

    @v1.post(SLEEP_PATH)
    def post_sleep(site: Site, caller: Caller) -> SleepReport:
        caller.require_write("sleep")
        report = sleep.run_sleep(skills, clock, caller.namespace, site)
        log_event("sleep", created=len(report.created), removed=len(report.removed))
        return report

    @v1.get(SKILLS_PATH)
    def get_skills(site: Site, caller: Caller) -> SkillsResponse:
        ns = caller.namespace
        return SkillsResponse(site=site, skills=skills.list_skills(ns, site), log=skills.list_log(ns, site))

    app.include_router(v1)
    return app


def mount_mcp(app: FastAPI, store: MapStore, clock: Clock, settings: Settings, keys: KeyRegistry) -> None:
    """Serve MCP (planner mode, one namespace per key) at /mcp in this same app, with its session
    manager running for the app's lifetime. Pass the registry `create_app` got, so a key means
    the same principal in REST and in MCP."""
    tenants = tenant_tools_factory(store, clock, settings.mcp_site, keys)
    mcp = build_http_app(tenants, Mode.PLANNER, settings, keys=keys)
    app.router.routes.append(Route(MCP_PATH, endpoint=mcp))
    app.router.routes.append(Route(KEY_PATH_PREFIX + "{key:path}", endpoint=mcp))
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(host: Starlette) -> AsyncIterator[None]:
        async with mcp.lifespan(), inner(host):
            yield

    app.router.lifespan_context = lifespan
