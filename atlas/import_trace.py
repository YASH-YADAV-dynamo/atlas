"""Trace import: recorded agent runs (JSON lines of `TraceStep`) become a map through the Route API.

    python -m atlas.import_trace FILE.jsonl

`TraceRecorder` wraps any `BrowserDriver` and writes such a file from a live run. It records the
actions exactly as the driver receives them, so callers must pass templated actions for logins
(`{password}`, never the value): a trace file is evidence and is stored as given.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from pydantic import ValidationError

from atlas.contracts.api_models import LocateRequest, ReportRequest
from atlas.contracts.errors import AtlasError, BrowserActionError, TraceFormatError
from atlas.contracts.ports import BrowserDriver, RouteApi
from atlas.contracts.trace_models import ImportSummary, TraceStep, trace_event_id
from atlas.contracts.types import Action, Params, Site, Snapshot

EXIT_FORMAT_ERROR = 2
EXIT_IMPORT_FAILED = 1


def _reason(error: ValidationError) -> str:
    """What is wrong with a line, from error kinds and field names only (never the values)."""
    found = error.errors()
    if found[0]["type"] == "json_invalid":
        return "invalid JSON"
    fields = sorted({".".join(str(part) for part in e["loc"]) or "line" for e in found})
    return "bad fields " + ", ".join(fields)


def parse_trace(lines: Iterable[str]) -> Iterator[TraceStep]:
    """One `TraceStep` per non-blank line; `TraceFormatError(line=<1-based>)` on a bad line."""
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            yield TraceStep.model_validate_json(line)
        except ValidationError as error:
            raise TraceFormatError(f"line {number}: {_reason(error)}", line=number) from error


def import_trace(routes: RouteApi, steps: Iterable[TraceStep]) -> ImportSummary:
    """Replay recorded steps through `locate` and `report`; the map is only touched by those calls."""
    count = new_states = duplicates = failed = goals = 0
    runs: set[str] = set()
    for step in steps:
        count += 1
        runs.add(step.run_id)
        located = routes.locate(LocateRequest(site=step.site, snapshot=step.before))
        linked = step.ok and step.goal_reached and step.goal_text is not None
        reported = routes.report(
            ReportRequest(
                site=step.site,
                from_state=located.state_id,
                action=step.action,
                to_snapshot=step.after,
                ok=step.ok,
                ms=step.ms,
                event_id=trace_event_id(step),
                goal_text=step.goal_text,
                params=step.params,
                goal_reached=linked,
                run_id=step.run_id,
            )
        )
        new_states += (not located.known) + (not reported.known)
        duplicates += reported.duplicate
        failed += not step.ok
        goals += linked
    return ImportSummary(
        steps=count,
        runs=len(runs),
        new_states=new_states,
        duplicates=duplicates,
        failed_steps=failed,
        goals_linked=goals,
    )


class TraceRecorder:
    """A `BrowserDriver` that passes everything to `driver` and records each `act` as a `TraceStep`."""

    def __init__(
        self,
        driver: BrowserDriver,
        *,
        site: Site,
        run_id: str,
        goal_text: str | None = None,
        params: Params | None = None,
    ) -> None:
        self._driver = driver
        self._site, self._run_id = site, run_id
        self._goal_text, self._params = goal_text, dict(params or {})
        self._steps: list[TraceStep] = []

    def snapshot(self) -> Snapshot:
        return self._driver.snapshot()

    def url(self) -> str:
        return self._driver.url()

    def act(self, action: Action) -> None:
        before = self._driver.snapshot()
        started = time.monotonic()
        error: BrowserActionError | None = None
        try:
            self._driver.act(action)
        except BrowserActionError as caught:
            error = caught
        ms = min(int((time.monotonic() - started) * 1000), 600_000)
        self._steps.append(
            TraceStep(
                run_id=self._run_id,
                index=len(self._steps),
                site=self._site,
                before=before,
                action=action,
                after=before if error is not None else self._driver.snapshot(),
                ok=error is None,
                ms=ms,
                goal_text=self._goal_text,
                params=self._params,
            )
        )
        if error is not None:
            raise error

    def steps(self, goal_reached: bool) -> tuple[TraceStep, ...]:
        """The recorded steps; `goal_reached` marks the run's last step as having reached the goal."""
        if not (goal_reached and self._steps):
            return tuple(self._steps)
        last = self._steps[-1].model_copy(update={"goal_reached": True})
        return (*self._steps[:-1], last)

    def write(self, path: Path, goal_reached: bool) -> None:
        """Append the steps to `path`, one JSON line each."""
        with path.open("a", encoding="utf-8") as out:
            for step in self.steps(goal_reached):
                out.write(step.model_dump_json() + "\n")


def _routes_from_settings() -> RouteApi:
    from atlas.runner.client import HttpRouteApi
    from atlas.settings import Settings

    settings = Settings()
    key = settings.atlas_api_key.get_secret_value() if settings.atlas_api_key else None
    return HttpRouteApi(settings.atlas_api_url, key)


def main(argv: Sequence[str], routes: RouteApi | None = None) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: python -m atlas.import_trace FILE.jsonl\n")
        return EXIT_FORMAT_ERROR
    try:
        with open(argv[0], encoding="utf-8") as handle:
            steps = list(parse_trace(handle))  # the whole file is valid before anything is imported
    except TraceFormatError as error:
        sys.stderr.write(f"trace format error: {error}\n")
        return EXIT_FORMAT_ERROR
    except (OSError, UnicodeDecodeError) as error:
        sys.stderr.write(f"cannot read trace file: {type(error).__name__}\n")
        return EXIT_FORMAT_ERROR
    api = routes if routes is not None else _routes_from_settings()
    try:
        summary = import_trace(api, steps)
    except AtlasError as error:
        sys.stderr.write(f"import failed: {error.code}\n")
        return EXIT_IMPORT_FAILED
    finally:
        close = getattr(api, "close", None)
        if routes is None and callable(close):
            close()
    sys.stdout.write(json.dumps(summary.model_dump(mode="json")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
