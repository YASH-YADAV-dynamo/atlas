"""API keys and tenants: who a bearer key is, which namespace it may touch, and abuse limits.

A key resolves to a `Principal` (namespace, read-only flag, tenant id). Three kinds exist:

- the admin key (`ATLAS_API_KEY`): read/write on `ATLAS_NAMESPACE`, no limits;
- the public demo key (`ATLAS_DEMO_MCP_KEY`): read-only on `ATLAS_NAMESPACE`;
- self-serve tenant keys (`POST /v1/keys`): read/write on their own `t_<random>` namespace.

Tenant keys are never stored in plain text: the store keeps HMAC-SHA256(pepper, key) only,
with the pepper from `ATLAS_KEY_PEPPER`. The store is one JSON file (`ATLAS_KEYS_PATH`, a
volume on the deployment) rewritten atomically (temp file, fsync, rename) under a lock.
Client IPs are kept the same way (peppered hash), only to enforce the per-IP creation cap.

The namespace always comes from the key, never from the request, so no request can name
another tenant's namespace.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from atlas.contracts.errors import AtlasError
from atlas.settings import Settings

KEY_PREFIX = "atl_"
NAMESPACE_PREFIX = "t_"
WRITE_TOOLS = frozenset({"atlas_report", "atlas_run", "atlas_act"})
_DAY = timedelta(days=1)
_FILE_VERSION = 1


class TenantError(AtlasError):
    """A refused tenant request; `status` is its HTTP status."""

    status = 400


class ReadOnlyKeyError(TenantError):
    code = "read_only_key"
    status = 403


class RateLimitedError(TenantError):
    code = "rate_limited"
    status = 429


class TenantLimitError(TenantError):
    code = "tenant_limit"
    status = 429


class KeysDisabledError(TenantError):
    code = "keys_disabled"
    status = 503


@dataclass(frozen=True)
class Principal:
    """The caller behind one request. `key_id` is a non-secret handle (a hash prefix)."""

    key_id: str
    namespace: str
    read_only: bool
    tenant_id: str | None = None  # set for self-serve keys only
    admin: bool = False

    def require_write(self, what: str) -> None:
        if self.read_only:
            raise ReadOnlyKeyError(
                f"this key is read-only: {what} is disabled. Create a private key with POST /v1/keys"
            )


@dataclass(frozen=True)
class IssuedKey:
    api_key: str
    namespace: str
    tenant_id: str
    note: str = ""  # set when the same address already created keys in the last 24 hours


def _now() -> datetime:
    return datetime.now(UTC)


class KeyRegistry:
    """Resolves bearer keys to principals and issues self-serve keys (thread-safe)."""

    def __init__(self, settings: Settings, path: str | Path | None = None) -> None:
        self._settings = settings
        pepper = settings.atlas_key_pepper.get_secret_value() if settings.atlas_key_pepper else ""
        self._issuing = bool(pepper)
        # Without a pepper no tenant key can be issued; static keys still hash with a process secret.
        self._pepper = pepper.encode() if pepper else secrets.token_bytes(32)
        self._path = Path(path if path is not None else settings.atlas_keys_path)
        self._lock = threading.Lock()
        self._tenants: dict[str, dict[str, Any]] = {}  # key hash -> record
        self._static: list[tuple[str, Principal]] = []
        admin = settings.atlas_api_key.get_secret_value() if settings.atlas_api_key else ""
        demo = settings.atlas_demo_mcp_key.get_secret_value() if settings.atlas_demo_mcp_key else ""
        ns = settings.atlas_namespace
        if admin:
            digest = self._hash(admin)
            self._static.append((digest, Principal(f"admin-{digest[:8]}", ns, read_only=False, admin=True)))
        if demo:
            digest = self._hash(demo)
            self._static.append((digest, Principal(f"demo-{digest[:8]}", ns, read_only=True)))
        if self._issuing:
            self._load()

    # ---- hashing and storage ----------------------------------------------------------------

    def _hash(self, value: str) -> str:
        return hmac.new(self._pepper, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        for record in data.get("tenants", []):
            self._tenants[record["key_hash"]] = record

    def _save(self) -> None:
        """Atomic rewrite: a crash leaves either the old or the new file, never a torn one."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": _FILE_VERSION, "tenants": list(self._tenants.values())}
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".keys-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ---- authentication ---------------------------------------------------------------------

    @property
    def has_admin_key(self) -> bool:
        return any(p.admin for _, p in self._static)

    def authenticate(self, token: str | None) -> Principal | None:
        """The principal for a presented bearer token, or None (constant-time comparisons)."""
        if not token:
            return None
        digest = self._hash(token)
        found: Principal | None = None
        for stored, principal in self._static:
            if hmac.compare_digest(stored, digest):
                found = principal
        with self._lock:
            record = self._tenants.get(digest)
        if record is not None and hmac.compare_digest(record["key_hash"], digest):
            found = Principal(
                key_id=f"tenant-{record['tenant_id']}",
                namespace=record["namespace"],
                read_only=bool(record.get("read_only", False)),
                tenant_id=record["tenant_id"],
            )
        return found

    def authenticate_header(self, authorization: str | bytes | None) -> Principal | None:
        if authorization is None:
            return None
        if isinstance(authorization, bytes):
            authorization = authorization.decode("latin-1")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            return None
        return self.authenticate(token.strip())

    # ---- self-serve keys ----------------------------------------------------------------------

    def issue(self, client_ip: str, now: datetime | None = None) -> IssuedKey:
        """A new private key and namespace, within the per-IP and global caps."""
        if not self._issuing:
            raise KeysDisabledError("self-serve keys are disabled on this server (ATLAS_KEY_PEPPER unset)")
        now = now or _now()
        ip_hash = self._hash(f"ip:{client_ip}")
        cutoff = (now - _DAY).isoformat()
        with self._lock:
            if len(self._tenants) >= self._settings.tenant_max:
                raise TenantLimitError("the server has reached its tenant limit; try again later")
            recent = sum(
                1 for r in self._tenants.values() if r.get("ip_hash") == ip_hash and r["created_at"] > cutoff
            )
            if recent >= self._settings.tenant_keys_per_ip_day:
                raise TenantLimitError(
                    "key creation limit reached for this address; try again tomorrow. "
                    "If Atlas is already connected in this client, keep using that key."
                )
            api_key = KEY_PREFIX + secrets.token_urlsafe(32)
            tenant_id = secrets.token_hex(8)
            namespace = NAMESPACE_PREFIX + tenant_id
            digest = self._hash(api_key)
            self._tenants[digest] = {
                "key_hash": digest,
                "tenant_id": tenant_id,
                "namespace": namespace,
                "read_only": False,
                "created_at": now.isoformat(),
                "ip_hash": ip_hash,
                "events": 0,
            }
            self._save()
        note = (
            f"This address already created {recent} key(s) today. A new key has an empty map. "
            "If Atlas is already connected in this client, keep using that key."
            if recent
            else ""
        )
        return IssuedKey(api_key=api_key, namespace=namespace, tenant_id=tenant_id, note=note)

    def tenant_count(self) -> int:
        with self._lock:
            return len(self._tenants)

    # ---- per-tenant event quota -------------------------------------------------------------

    def _record_for(self, principal: Principal) -> dict[str, Any] | None:
        if principal.tenant_id is None:
            return None
        return next((r for r in self._tenants.values() if r["tenant_id"] == principal.tenant_id), None)

    def check_event_quota(self, principal: Principal) -> None:
        """Raise before a write when the tenant already stores its maximum number of events."""
        with self._lock:
            record = self._record_for(principal)
            if record is not None and record.get("events", 0) >= self._settings.tenant_max_events:
                raise TenantLimitError(
                    f"this key has reached its limit of {self._settings.tenant_max_events} stored events"
                )

    def check_plan_quota(self, principal: Principal) -> None:
        """Raise before plan records a new page when the tenant's map is at its size limit."""
        with self._lock:
            record = self._record_for(principal)
            if record is not None and record.get("plan_states", 0) >= self._settings.tenant_max_plan_states:
                raise TenantLimitError("map size limit reached for this key")

    def note_plan_state(self, principal: Principal) -> None:
        with self._lock:
            record = self._record_for(principal)
            if record is not None:
                record["plan_states"] = record.get("plan_states", 0) + 1
                self._save()

    def note_event(self, principal: Principal) -> None:
        with self._lock:
            record = self._record_for(principal)
            if record is not None:
                record["events"] = record.get("events", 0) + 1
                self._save()


class RateLimiter:
    """Sliding one-minute window of requests per caller (in memory, per process)."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._per_minute = per_minute
        self._clock = clock
        self._hits: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, caller: str) -> bool:
        if self._per_minute <= 0:
            return True
        now = self._clock()
        with self._lock:
            hits = self._hits[caller]
            while hits and hits[0] <= now - 60.0:
                hits.popleft()
            if len(hits) >= self._per_minute:
                return False
            hits.append(now)
            if len(self._hits) > 10_000:  # forget idle callers
                for key in [k for k, v in self._hits.items() if not v]:
                    del self._hits[key]
            return True


def rate_key(principal: Principal, client_ip: str) -> str | None:
    """Who a request is charged to: the key, or key and address for the shared demo key.
    None for the admin key (never limited)."""
    if principal.admin:
        return None
    if principal.read_only and principal.tenant_id is None:
        return f"{principal.key_id}|{client_ip}"
    return principal.key_id
