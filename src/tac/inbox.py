"""The worker inbox: effect requests the runner re-checks and performs (section 10).

The chief or a pipeline asks for an effect by appending one JSON line to
`inbox.jsonl` in the worker store; it never performs one itself. The runner
reads the new lines past its cursor, which lives in the controller store where
no agent writes, validates each request against its schema, the policy, the
approval and the host's lease budget, performs it through the effect journal,
and appends the outcome to `inbox.done.jsonl` beside the inbox. Everything read
from the inbox is agent output, so it is data: a line that is not a request is
answered as refused, never acted on.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from tac import human, leases
from tac.effects import EffectError, EffectRequest, Effects, utc_text
from tac.handoff import worker_store
from tac.receipts import ID_PATTERN, ORDER_PATTERN
from tac.runner import RunnerError

INBOX = "inbox.jsonl"
DONE = "inbox.done.jsonl"
CURSOR = "inbox.cursor.json"
INBOX_STAGE = "inbox"
# One line may not be larger than this; a longer one is refused unread.
MAX_LINE = 64 * 1024


class InboxRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    effect: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    arguments: dict[str, JsonValue]
    order_id: str | None = Field(default=None, pattern=ORDER_PATTERN)
    run_id: str = Field(pattern=ID_PATTERN)
    requested_by: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
    approval_id: str | None = Field(default=None, pattern=human.ITEM_ID)
    ts: str


@dataclass(frozen=True, slots=True)
class Outcome:
    id: str | None
    outcome: str
    reason: str
    observed: Mapping[str, JsonValue]

    def line(self) -> str:
        body = {
            "id": self.id,
            "outcome": self.outcome,
            "reason": self.reason,
            "observed": dict(self.observed),
            "ts": utc_text(),
        }
        return json.dumps(body, sort_keys=True) + "\n"


def _cursor(store: Path) -> int:
    path = store / CURSOR
    try:
        return int(json.loads(path.read_text(encoding="utf-8"))["offset"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def _save_cursor(store: Path, offset: int) -> None:
    path = store / CURSOR
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps({"offset": offset}) + "\n", encoding="utf-8")
    temp.replace(path)


def handle(effects: Effects, raw: bytes, environ: Mapping[str, str]) -> Outcome:
    """One inbox line, judged and, when it holds, performed."""
    try:
        request = InboxRequest.model_validate_json(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "request"
        return Outcome(None, "refused", f"not a request at {where}: {first['msg']}", {})
    effect = EffectRequest(
        effect=request.effect,
        arguments=request.arguments,
        run_id=request.run_id,
        stage=INBOX_STAGE,
        order_id=request.order_id,
        approval_id=request.approval_id,
    )
    sha = request.arguments.get("sha")
    try:
        effects.authorize(
            [effect], request.approval_id, sha if isinstance(sha, str) else None
        )
        lease = leases.acquire(
            environ,
            effects.config.knobs.teams.max_local_agents,
            repository=effects.runner.repository,
            run_id=request.run_id,
            stage=INBOX_STAGE,
            role=request.requested_by,
        )
    except (EffectError, RunnerError) as exc:
        return Outcome(request.id, "refused", str(exc), {})
    try:
        observed = effects.perform(effect)
    except EffectError as exc:
        return Outcome(request.id, "failed", str(exc), {})
    finally:
        leases.release(lease)
    return Outcome(request.id, "done", "", observed)


def watch_once(effects: Effects, environ: Mapping[str, str]) -> list[Outcome]:
    """Every request past the cursor, in order; the cursor moves past each line
    once its outcome is written, so a crash repeats at most the line it was on,
    and the effect journal keeps that from acting twice."""
    folder = worker_store(effects.runner.root, effects.config)
    inbox = folder / INBOX
    done = folder / DONE
    store = effects.runner.store
    outcomes: list[Outcome] = []
    if not inbox.is_file():
        return outcomes
    offset = _cursor(store)
    size = inbox.stat().st_size
    if offset > size:
        # The inbox was replaced by a shorter file: start it again.
        offset = 0
    with inbox.open("rb") as stream:
        stream.seek(offset)
        while True:
            raw = stream.readline(MAX_LINE + 1)
            if not raw:
                break
            if not raw.endswith(b"\n"):
                if len(raw) <= MAX_LINE:
                    # A line still being written: read it on the next pass.
                    break
                outcome = Outcome(
                    None, "refused", "the line is larger than the inbox reads", {}
                )
                stream.readline()
            elif not raw.strip():
                offset = stream.tell()
                _save_cursor(store, offset)
                continue
            else:
                outcome = handle(effects, raw.strip(), environ)
            outcomes.append(outcome)
            with done.open("a", encoding="utf-8") as out:
                out.write(outcome.line())
            offset = stream.tell()
            _save_cursor(store, offset)
    return outcomes
