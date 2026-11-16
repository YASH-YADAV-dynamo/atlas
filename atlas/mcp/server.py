"""MCP transports. Thin by design: tool names and input models come from the contract
(`tools_for(mode)`), how they are listed from `atlas.mcp.catalog`, behaviour from `AtlasTools`;
this module only validates, dispatches and frames the result as `tool_text(result)`.

Over HTTP every request carries its own `Principal` (resolved from the bearer key by
`TenantGate`), and the tools it calls are bound to that principal's namespace.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, MutableMapping
from concurrent.futures import Executor
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from mcp import types
from mcp.server import Server, ServerRequestContext
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ValidationError

from atlas.contracts.errors import AtlasError, ToolUnavailableError, UnauthorizedError
from atlas.contracts.mcp_models import (
    MCP_RESPONSE_TOKEN_BUDGET,
    SITE_CHANGES_URI,
    SITES_URI,
    Mode,
    ToolSpec,
    response_tokens,
    tool_text,
    tools_for,
)
from atlas.contracts.ports import Clock, MapStore
from atlas.mcp.catalog import SERVER_INSTRUCTIONS, listed_description, listed_schema
from atlas.mcp.coerce import coerce
from atlas.mcp.keys import WRITE_TOOLS, KeyRegistry, Principal, RateLimiter, rate_key
from atlas.mcp.resources import changes_resource, sites_resource
from atlas.mcp.tools import AtlasTools
from atlas.settings import Settings

MCP_PATH = "/mcp"
KEY_PATH_PREFIX = MCP_PATH + "/k/"
SERVER_NAME = "atlas"
_CHANGES_URI = re.compile("^" + re.escape(SITE_CHANGES_URI).replace(r"\{site\}", "([^/]+)") + "$")

type Scope = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
type Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
type ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
type ToolsFor = Callable[[Principal], AtlasTools]

PRINCIPAL_SCOPE_KEY = "atlas.principal"
_SESSION_HEADER = b"mcp-session-id"


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message[:300])], is_error=True)


_EXPECTED: dict[str, str] = {
    "atlas_plan": "atlas_plan {site, goal_text, params?, snapshot_summary:{url,title}}",
    "atlas_report": "atlas_report {site, from_state, steps:[{action:{kind,locator:{role,name}},"
    "to:{url,title}}], goal_text?, goal_reached?} or {site, plan_id, done?}",
    "atlas_find_skills": "atlas_find_skills {site?, goal_text?}",
    "atlas_recall": "atlas_recall {entity_type, key}",
}
_MESSAGE_LIMIT = 300


def _validation_message(error: ValidationError, tool: str | None = None) -> str:
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "arguments"
    message = f"invalid {where}: {first['msg']}"
    hint = _EXPECTED.get(tool or "")
    if hint is None:
        return message[:_MESSAGE_LIMIT]
    suffix = f" Expected: {hint}"
    return message[: _MESSAGE_LIMIT - len(suffix)] + suffix


def _with_notes(result: BaseModel, notes: tuple[str, ...]) -> BaseModel:
    """The result carrying the corrections made to the input, as many as still fit the budget."""
    if "notes" not in type(result).model_fields:
        return result
    for count in range(len(notes), 0, -1):
        candidate = result.model_copy(update={"notes": notes[:count]})
        if response_tokens(candidate) <= MCP_RESPONSE_TOKEN_BUDGET:
            return candidate
    return result


def build_server(
    tools: AtlasTools | ToolsFor,
    mode: Mode,
    executor: Executor | None = None,
    *,
    quota: KeyRegistry | None = None,
) -> Server:
    """The MCP server for one mode. Tools run on `executor` when given (a browser session must
    stay on the thread that created it), otherwise inline.

    `tools` is one `AtlasTools` (stdio: one local agent) or a function from the request's
    `Principal` to the `AtlasTools` of that principal's namespace (HTTP: many tenants). The
    principal is read from each request's own ASGI scope, never from shared state.
    """
    specs: dict[str, ToolSpec] = {spec.name: spec for spec in tools_for(mode)}
    listed = [
        types.Tool(
            name=spec.name,
            description=listed_description(spec, mode),
            input_schema=listed_schema(spec, mode),
        )
        for spec in specs.values()
    ]

    def tools_of(ctx: ServerRequestContext[Any]) -> tuple[AtlasTools, Principal | None]:
        if isinstance(tools, AtlasTools):
            return tools, None
        principal = principal_of(ctx.request)
        if principal is None:  # the HTTP gate always sets one; refuse rather than guess
            raise UnauthorizedError("no principal on this request")
        return tools(principal), principal

    async def list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=listed)

    async def call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        spec = specs.get(params.name)
        if spec is None:
            return _error(f"unknown tool {params.name[:60]}; available: {', '.join(specs)}")
        arguments, notes = coerce(spec.name, dict(params.arguments or {}))
        try:
            request = spec.input_model.model_validate(arguments)
        except ValidationError as error:
            return _error(_validation_message(error, spec.name))
        try:
            session_tools, principal = tools_of(ctx)
            writes = spec.name in WRITE_TOOLS
            if principal is not None and writes:
                principal.require_write(spec.name)
                if quota is not None:
                    quota.check_event_quota(principal)
            method: Callable[[Any], BaseModel] = getattr(session_tools, spec.name.removeprefix("atlas_"))
            if executor is None and principal is not None:
                # Hosted tools are stateless and the store is thread-safe: run them off the event
                # loop so one tenant's HydraDB round trips never stall every other tenant.
                result = await asyncio.to_thread(method, request)
            elif executor is None:
                result = method(request)
            else:
                result = await asyncio.get_running_loop().run_in_executor(executor, method, request)
            stored = writes and not getattr(result, "duplicate", True)
            if principal is not None and quota is not None and stored:
                for _ in range(max(1, getattr(result, "learned", None) or 1)):
                    quota.note_event(principal)
        except (AtlasError, ValidationError) as error:
            text = str(error) if isinstance(error, AtlasError) else _validation_message(error, spec.name)
            return _error(text)
        if notes:
            result = _with_notes(result, notes)
        return types.CallToolResult(content=[types.TextContent(type="text", text=tool_text(result))])

    async def list_resources(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListResourcesResult:
        return types.ListResourcesResult(
            resources=[types.Resource(name="sites", uri=SITES_URI, description="Mapped sites and sizes")]
        )

    async def list_templates(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListResourceTemplatesResult:
        template = types.ResourceTemplate(
            name="site-changes", uri_template=SITE_CHANGES_URI, description="Recent flow changes of a site"
        )
        return types.ListResourceTemplatesResult(resource_templates=[template])

    async def read_resource(
        ctx: ServerRequestContext[Any], params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        session_tools, _ = tools_of(ctx)
        store: Any = session_tools.store
        ns = session_tools.namespace
        uri = str(params.uri)
        changes = _CHANGES_URI.match(uri)
        try:
            if uri == SITES_URI:
                text = tool_text(sites_resource(store, ns))
            elif changes is not None:
                text = tool_text(changes_resource(store, ns, changes.group(1)))
            else:
                raise ValueError(f"unknown resource {uri[:100]}")
        except ValidationError as error:
            raise ValueError(_validation_message(error)) from error
        content = types.TextResourceContents(uri=uri, mime_type="application/json", text=text)
        return types.ReadResourceResult(contents=[content])

    return Server(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        website_url="https://atlas.yashdynamo.fun",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=list_resources,
        on_list_resource_templates=list_templates,
        on_read_resource=read_resource,
    )


def principal_of(request: object) -> Principal | None:
    """The principal the HTTP gate attached to this request's own scope (None on stdio)."""
    scope = getattr(request, "scope", None)
    if not isinstance(scope, MutableMapping):
        return None
    principal = cast(MutableMapping[str, Any], scope).get(PRINCIPAL_SCOPE_KEY)
    return principal if isinstance(principal, Principal) else None


def path_key_of(path: str) -> str | None:
    """The key embedded in `/mcp/k/<key>` (None for any other path)."""
    if not path.startswith(KEY_PATH_PREFIX):
        return None
    return path[len(KEY_PATH_PREFIX) :].strip("/")


def _header(scope: Scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value
    return None


def scope_client_ip(scope: Scope) -> str:
    """The last X-Forwarded-For entry (appended by our own proxy), else the socket address."""
    forwarded = (_header(scope, b"x-forwarded-for") or b"").decode("latin-1")
    last = forwarded.rsplit(",", 1)[-1].strip()
    if last:
        return last
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


async def _refuse(send: Send, status: int, code: str, message: str) -> None:
    body = json.dumps({"code": code, "message": message}).encode()
    headers = [(b"content-type", b"application/json")]
    if status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class _SessionOwners:
    """MCP session id -> key id of the principal that created it (bounded, oldest dropped)."""

    def __init__(self, limit: int = 10_000) -> None:
        self._owners: OrderedDict[str, str] = OrderedDict()
        self._limit = limit
        self._lock = threading.Lock()

    def owner(self, session_id: str) -> str | None:
        with self._lock:
            return self._owners.get(session_id)

    def bind(self, session_id: str, key_id: str) -> None:
        with self._lock:
            if session_id in self._owners:
                return  # the first binder wins; a session is never rebound
            self._owners[session_id] = key_id
            while len(self._owners) > self._limit:
                self._owners.popitem(last=False)

    def forget(self, session_id: str) -> None:
        with self._lock:
            self._owners.pop(session_id, None)


class TenantGate:
    """ASGI gate in front of the MCP transport. Per request it resolves the bearer key to a
    `Principal`, rate-limits it, checks that a resumed MCP session belongs to that same key, and
    attaches the principal to this request's own scope (never to shared state)."""

    def __init__(self, app: ASGIApp, keys: KeyRegistry, limiter: RateLimiter) -> None:
        self._app = app
        self._keys = keys
        self._limiter = limiter
        self._sessions = _SessionOwners()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path: str = scope.get("path", "") if scope["type"] == "http" else ""
        if not (path == MCP_PATH or path.startswith(MCP_PATH + "/")):
            await self._app(scope, receive, send)
            return
        path_key = path_key_of(path)
        if path_key is not None:
            # `/mcp/k/<key>` is `/mcp` with that bearer key, for clients that cannot set headers.
            principal = self._keys.authenticate(path_key)
            scope = {**scope, "path": MCP_PATH, "raw_path": MCP_PATH.encode()}
        else:
            principal = self._keys.authenticate_header(_header(scope, b"authorization"))
        if principal is None:
            await _refuse(send, 401, "unauthorized", "missing or invalid key")
            return
        charged = rate_key(principal, scope_client_ip(scope))
        if charged is not None and not self._limiter.allow(charged):
            await _refuse(send, 429, "rate_limited", "too many requests for this key; slow down")
            return
        raw_session = _header(scope, _SESSION_HEADER)
        session_id = raw_session.decode("latin-1") if raw_session is not None else None
        if session_id is not None and self._sessions.owner(session_id) != principal.key_id:
            # Unknown, expired or another key's session: indistinguishable on purpose.
            await _refuse(send, 404, "session_not_found", "unknown MCP session; initialize a new one")
            return
        scoped: Scope = {**scope, PRINCIPAL_SCOPE_KEY: principal}
        deleting = scope.get("method") == "DELETE" and session_id is not None
        owner = principal.key_id

        async def tracked_send(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status = int(message.get("status", 500))
                for key, value in message.get("headers", ()):
                    if key.lower() == _SESSION_HEADER and status < 400:
                        self._sessions.bind(value.decode("latin-1"), owner)
                if deleting and session_id is not None and status < 300:
                    self._sessions.forget(session_id)
            await send(message)

        await self._app(scoped, receive, tracked_send)


class McpHttpApp:
    """The MCP streamable-HTTP endpoint as one ASGI app (gate included) and the lifespan that
    runs its session manager. Served alone it runs its own lifespan; mounted in a host app,
    the host enters `lifespan()`."""

    def __init__(self, gate: TenantGate, server: Server) -> None:
        self._gate = gate
        self._server = server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._gate(scope, receive, send)

    def lifespan(self) -> AbstractAsyncContextManager[None]:
        return self._server.session_manager.run()


def tenant_tools_factory(
    store: MapStore, clock: Clock, site: str, keys: KeyRegistry | None = None
) -> ToolsFor:
    """Planner-mode tools for a principal: a fresh, browserless `AtlasTools` per call, bound to
    the principal's namespace. Planner tools keep no session state, so nothing is shared."""

    def make(principal: Principal) -> AtlasTools:
        gate = on_new = None
        if keys is not None and principal.tenant_id is not None and not principal.read_only:
            registry = keys
            gate = lambda: registry.check_plan_quota(principal)  # noqa: E731
            on_new = lambda: registry.note_plan_state(principal)  # noqa: E731
        return AtlasTools(
            store,
            clock,
            principal.namespace,
            site,
            None,
            read_only=principal.read_only,
            new_state_gate=gate,
            on_new_state=on_new,
        )

    return make


def build_http_app(
    tools: AtlasTools | ToolsFor,
    mode: Mode,
    settings: Settings,
    executor: Executor | None = None,
    *,
    keys: KeyRegistry | None = None,
) -> McpHttpApp:
    """Streamable HTTP at `/mcp`, planner mode only, never served without a key.

    With one `AtlasTools` every key is served from that instance's namespace (a single-tenant
    local server); with `tenant_tools_factory` each request gets the tools of its own
    principal's namespace. The SDK's DNS-rebinding guard is off: the deployment sits behind a
    public host name and the bearer key is the access control.
    """
    if mode is not Mode.PLANNER:
        raise ToolUnavailableError("MCP over HTTP serves planner mode only; use stdio for runner mode")
    registry = keys if keys is not None else KeyRegistry(settings)
    if keys is None and not registry.has_admin_key:
        raise UnauthorizedError("ATLAS_API_KEY must be set to serve MCP over HTTP")
    server = build_server(tools, mode, executor, quota=registry)
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    limiter = RateLimiter(settings.tenant_requests_per_minute)
    return McpHttpApp(TenantGate(app, registry, limiter), server)
