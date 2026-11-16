"""HTTP implementation of the RouteApi port."""

from __future__ import annotations

import httpx
from pydantic import BaseModel

from atlas.contracts import errors
from atlas.contracts.api_models import (
    LOCATE_PATH,
    PLAN_PATH,
    REPORT_PATH,
    LocateRequest,
    LocateResponse,
    PlanRequest,
    PlanResponse,
    ReportRequest,
    ReportResponse,
)

_BY_CODE: dict[str, type[errors.AtlasError]] = {
    kind.code: kind
    for kind in vars(errors).values()
    if isinstance(kind, type) and issubclass(kind, errors.AtlasError) and kind is not errors.AtlasError
}


class HttpRouteApi:
    def __init__(self, base_url: str, api_key: str | None, timeout: float = 30.0) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(base_url=base_url, headers=headers, timeout=timeout)

    def _post[T: BaseModel](self, path: str, request: BaseModel, response_type: type[T]) -> T:
        try:
            response = self._http.post(
                path, content=request.model_dump_json(), headers={"Content-Type": "application/json"}
            )
        except httpx.HTTPError as exc:
            raise errors.StoreUnavailableError(f"route api unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise _typed_error(response)
        return response_type.model_validate_json(response.content)

    def locate(self, request: LocateRequest) -> LocateResponse:
        return self._post(LOCATE_PATH, request, LocateResponse)

    def plan(self, request: PlanRequest) -> PlanResponse:
        return self._post(PLAN_PATH, request, PlanResponse)

    def report(self, request: ReportRequest) -> ReportResponse:
        return self._post(REPORT_PATH, request, ReportResponse)

    def close(self) -> None:
        self._http.close()


def _typed_error(response: httpx.Response) -> errors.AtlasError:
    try:
        body = response.json()
        code, message = str(body["code"]), str(body.get("message", ""))
    except (ValueError, KeyError, TypeError):
        return errors.AtlasError(f"route api returned HTTP {response.status_code}")
    return _BY_CODE.get(code, errors.AtlasError)(message)
