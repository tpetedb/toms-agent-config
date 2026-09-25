"""`tac usage`: how much of each provider's usage window is spent (design section 4).

A reading comes from the client's own statusline, at no token cost: Claude Code
hands a statusline command a JSON object on stdin whose `rate_limits` holds the
five-hour and seven-day windows, each `used_percentage` (0 to 100) and
`resets_at` (Unix seconds), and a spend limit behind a gateway
(https://code.claude.com/docs/en/statusline). `rate_limits` is absent before the
first API response and for accounts without a subscription window, and a window
is dropped once it resets. `tac usage record` keeps those fields as a sample in
the worker store; any other shape of `rate_limits` is refused, never guessed at,
since a misread key would make every reading unavailable and pause every spawn.

Every dispatch asks `spawn_state` first. A provider is `ok`, `slow` once the
seven-day window passes `slow_at`, `stop` once any window passes `stop_at`, or
`unavailable` with no sample, a sample older than `usage.max_age_s`, or one
taken before the first API response; `usage.unavailable = "pause"` makes
unavailable pause spawns as stop does.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tac.config import Config
from tac.handoff import worker_store

Provider = Literal["claude", "openai"]
PROVIDERS: tuple[Provider, ...] = ("claude", "openai")
# The usage table is keyed by provider family; the harness is how it runs.
HARNESS_PROVIDER: dict[str, Provider] = {"claude": "claude", "codex": "openai"}
USAGE_DIR = "usage"
State = Literal["ok", "slow", "stop", "unavailable"]
PAUSED: frozenset[State] = frozenset({"stop", "unavailable"})


class UsageError(Exception):
    """A statusline payload or a sample tac cannot read: the message says why."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Window(_Model):
    used_percentage: float = Field(ge=0)
    resets_at: int


class RateLimits(_Model):
    five_hour: Window | None = None
    seven_day: Window | None = None
    spend_limit: Window | None = None


class Sample(_Model):
    sample_version: Literal[1] = 1
    provider: Provider
    ts: int
    # None: the statusline had no rate_limits yet, so nothing can be judged.
    rate_limits: RateLimits | None


@dataclass(frozen=True, slots=True)
class Reading:
    provider: Provider
    state: State
    reason: str

    @property
    def paused(self) -> bool:
        return self.state in PAUSED


def parse_statusline(text: str) -> RateLimits | None:
    """The usage windows of one Claude Code statusline payload, or None before
    the first API response; any other shape is refused."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"the statusline input is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise UsageError("the statusline input is not a JSON object")
    limits = data.get("rate_limits")
    if limits is None:
        return None
    try:
        return RateLimits.model_validate(limits)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "rate_limits"
        raise UsageError(
            f"rate_limits has a shape tac does not know (at {where}: "
            f"{first['msg']}); refusing to guess"
        ) from exc


def usage_dir(root: Path, config: Config) -> Path:
    return worker_store(root, config) / USAGE_DIR


def record(
    root: Path, config: Config, provider: str, text: str, now: float | None = None
) -> Sample:
    """Write the provider's sample from a statusline payload."""
    if provider != "claude":
        raise UsageError(
            f"no documented statusline shape carries {provider}'s usage windows "
            "yet, so tac records none and its reading stays unavailable"
        )
    sample = Sample(
        provider="claude",
        ts=int(time.time() if now is None else now),
        rate_limits=parse_statusline(text),
    )
    folder = usage_dir(root, config)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{provider}.json"
    temp = target.with_suffix(".json.tmp")
    temp.write_text(sample.model_dump_json() + "\n", encoding="utf-8")
    temp.replace(target)
    return sample


def load_sample(root: Path, config: Config, provider: Provider) -> Sample | None:
    path = usage_dir(root, config) / f"{provider}.json"
    if not path.is_file():
        return None
    try:
        return Sample.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise UsageError(f"{path.name} is not a usage sample: {exc}") from exc


def _share(window: Window | None, now: float) -> float | None:
    """The window's used share, 0 once it has reset, None when absent."""
    if window is None:
        return None
    if window.resets_at <= now:
        return 0.0
    return window.used_percentage / 100


def judge(
    provider: Provider, sample: Sample | None, config: Config, now: float
) -> Reading:
    limits = config.knobs.usage
    threshold = getattr(limits, provider)
    if sample is None:
        return Reading(provider, "unavailable", "no sample recorded")
    age = now - sample.ts
    if age > limits.max_age_s:
        return Reading(
            provider,
            "unavailable",
            f"the sample is {int(age)}s old, over usage.max_age_s = {limits.max_age_s}",
        )
    if sample.rate_limits is None:
        return Reading(provider, "unavailable", "no API response yet in the session")
    rl = sample.rate_limits
    shares = {
        name: _share(getattr(rl, name), now)
        for name in ("five_hour", "seven_day", "spend_limit")
    }
    present = {k: v for k, v in shares.items() if v is not None}
    if not present:
        return Reading(provider, "unavailable", "the sample holds no usage window")
    over = [k for k, v in present.items() if v >= threshold.stop_at]
    shown = ", ".join(f"{k} {v:.0%}" for k, v in present.items())
    if over:
        return Reading(
            provider,
            "stop",
            f"{shown}; {', '.join(over)} at stop_at {threshold.stop_at}",
        )
    week = present.get("seven_day")
    if week is not None and week >= threshold.slow_at:
        return Reading(
            provider, "slow", f"{shown}; seven_day at slow_at {threshold.slow_at}"
        )
    return Reading(provider, "ok", shown)


def enforced_providers(config: Config) -> tuple[Provider, ...]:
    """The usage providers of the harnesses this project enforces."""
    found: list[Provider] = [
        HARNESS_PROVIDER[h]
        for h in config.knobs.harnesses.enforced
        if h in HARNESS_PROVIDER
    ]
    return tuple(dict.fromkeys(found))


def readings(
    root: Path,
    config: Config,
    providers: Iterable[Provider] | None = None,
    now: float | None = None,
) -> list[Reading]:
    when = time.time() if now is None else now
    out = []
    for provider in providers if providers is not None else enforced_providers(config):
        try:
            sample = load_sample(root, config, provider)
        except UsageError as exc:
            out.append(Reading(provider, "unavailable", str(exc)))
            continue
        out.append(judge(provider, sample, config, when))
    return out


def spawn_state(
    root: Path, config: Config, harness: str, now: float | None = None
) -> Reading:
    """The reading every dispatch checks before it starts an agent on `harness`."""
    provider = HARNESS_PROVIDER.get(harness)
    if provider is None:
        raise UsageError(f"no usage provider reads the {harness} harness")
    return readings(root, config, [provider], now)[0]
