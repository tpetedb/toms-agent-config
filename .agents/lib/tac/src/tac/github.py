"""The GitHub gate: render and apply the default-branch ruleset, and judge it.

Every call goes through a `Transport`, so the tests drive the same code with
recorded responses and nothing here reaches GitHub unless the owner runs it.
The owner's token is used only through the owner's own `gh`, on the host; an
agent session gets the anonymous transport, which can read a public list and
never carries a credential.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
ACCEPT = "application/vnd.github+json"
RULESET_NAME = "tac-default-branch"
# The jobs of .github/workflows/ci.yml; a test keeps the two in step.
REQUIRED_CHECKS = ("verify", "work")
# The GitHub Actions app: pinning the source stops a commit status posted by any
# other integration from satisfying a required check.
GITHUB_ACTIONS_APP_ID = 15368
# Push rights without the right to edit rulesets or repository settings.
BOT_ROLES = ("write",)
CODEOWNERS = ".github/CODEOWNERS"
DEFAULT_BRANCH_REFS = ("~DEFAULT_BRANCH", "~ALL")
CALL_TIMEOUT_S = 30
OWNER_STEP = (
    "the owner's step (TODO.HUMAN.md, after Q14): run `tac github apply` in your "
    "own terminal on the host, then `tac doctor`"
)
HOST_STEP = (
    "bypass_actors is returned only to an admin token; run `tac doctor` in your "
    "own terminal on the host"
)


class ApiError(Exception):
    """GitHub answered with an error, or could not be reached."""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status


class Transport(Protocol):
    """One REST call: returns the decoded JSON body or raises ApiError."""

    @property
    def authenticated(self) -> bool: ...

    def call(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> Any: ...


HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")


@dataclass(frozen=True, slots=True)
class GhTransport:
    """The owner's own `gh`, so tac never sees or stores the token."""

    gh: str
    authenticated: bool = True

    def call(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> Any:
        argv = [
            self.gh,
            "api",
            "--method",
            method,
            "-H",
            f"Accept: {ACCEPT}",
            "-H",
            f"X-GitHub-Api-Version: {API_VERSION}",
            path,
        ]
        if body is not None:
            argv += ["--input", "-"]
        env = {**os.environ, "GH_TELEMETRY": "false", "GH_PROMPT_DISABLED": "1"}
        try:
            done = subprocess.run(
                argv,
                input=json.dumps(body) if body is not None else None,
                capture_output=True,
                text=True,
                env=env,
                timeout=CALL_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ApiError(None, f"gh did not run: {exc.__class__.__name__}") from exc
        if done.returncode != 0:
            match = HTTP_STATUS.search(done.stderr)
            status = int(match.group(1)) if match else None
            first = (done.stderr.strip().splitlines() or ["no message"])[0]
            raise ApiError(status, f"{method} {path}: {first}")
        return json.loads(done.stdout) if done.stdout.strip() else None


@dataclass(frozen=True, slots=True)
class AnonymousTransport:
    """Reads public resources with no credential at all; never writes."""

    authenticated: bool = False

    def call(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> Any:
        if method != "GET" or body is not None:
            raise ApiError(None, "the anonymous transport only reads")
        request = urllib.request.Request(
            f"{API_ROOT}/{path}",
            headers={
                "Accept": ACCEPT,
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "tac-doctor",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=CALL_TIMEOUT_S) as reply:
                return json.loads(reply.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, f"GET {path}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError(None, f"GET {path}: {exc.__class__.__name__}") from exc


def gh_transport() -> GhTransport | None:
    gh = shutil.which("gh")
    return GhTransport(gh) if gh else None


# ---- the ruleset tac applies


def desired_ruleset(checks: Sequence[str] = REQUIRED_CHECKS) -> dict[str, Any]:
    """Bypass off for everyone, code-owner review, required checks, linear history.

    Squash is the only merge method, so linear history and the owner's
    `gh pr merge --squash --match-head-commit` agree.
    """
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "required_linear_history"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 1,
                    "require_code_owner_review": True,
                    "dismiss_stale_reviews_on_push": True,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": True,
                    "allowed_merge_methods": ["squash"],
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "do_not_enforce_on_create": False,
                    "required_status_checks": [
                        {"context": name, "integration_id": GITHUB_ACTIONS_APP_ID}
                        for name in checks
                    ],
                },
            },
        ],
    }


# ---- judging a ruleset (build condition C5)


def _rules(ruleset: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    rules = ruleset.get("rules") or []
    return [r for r in rules if isinstance(r, Mapping) and r.get("type") == kind]


def _describe_actor(actor: Any) -> str:
    if not isinstance(actor, Mapping):
        return "?"
    return f"{actor.get('actor_type', '?')}:{actor.get('actor_id', '?')}"


def ruleset_problems(ruleset: Mapping[str, Any]) -> list[str]:
    """What keeps one fetched ruleset from holding; empty when it holds.

    A missing bypass_actors key is a problem in its own right: the API omits it
    for a token without admin rights, so its absence proves nothing.
    """
    problems: list[str] = []
    if "bypass_actors" not in ruleset:
        problems.append("bypass_actors missing from the response")
    elif ruleset["bypass_actors"]:
        actors = ", ".join(_describe_actor(a) for a in ruleset["bypass_actors"])
        problems.append(f"bypass_actors is not empty ({actors})")
    review = any(
        (r.get("parameters") or {}).get("require_code_owner_review") is True
        for r in _rules(ruleset, "pull_request")
    )
    if not review:
        problems.append("no pull_request rule requires code-owner review")
    checks = any(
        (r.get("parameters") or {}).get("required_status_checks")
        for r in _rules(ruleset, "required_status_checks")
    )
    if not checks:
        problems.append("no required status checks")
    return problems


def covers_branch(ruleset: Mapping[str, Any], branch: str) -> bool:
    ref_name = (ruleset.get("conditions") or {}).get("ref_name") or {}
    include = ref_name.get("include") or []
    exclude = ref_name.get("exclude") or []
    names = {*DEFAULT_BRANCH_REFS, f"refs/heads/{branch}"}
    return any(i in names for i in include) and not any(e in names for e in exclude)


@dataclass(frozen=True, slots=True)
class RulesetReport:
    """ok is None when the check could not decide, as without an admin token."""

    ok: bool | None
    lines: tuple[str, ...]
    fetched: tuple[int, ...] = field(default=())

    @property
    def detail(self) -> str:
        return "; ".join(self.lines)


def judge_rulesets(transport: Transport, repo: str) -> RulesetReport:
    """Fetch each active branch ruleset by id and judge it (design section 8).

    The list endpoint omits bypass_actors, so a count over the list proves
    nothing; every active branch ruleset has to hold, and one of them has to
    cover the default branch. A tag or push ruleset does not count.
    """
    try:
        listed = transport.call("GET", f"repos/{repo}/rulesets?per_page=100")
    except ApiError as exc:
        return RulesetReport(None, (f"cannot list rulesets of {repo}: {exc}",))
    listed = [r for r in listed or [] if isinstance(r, Mapping)]
    active = [r for r in listed if r.get("enforcement") == "active"]
    branch = [r for r in active if r.get("target") == "branch"]
    if not branch:
        others = sorted({str(r.get("target")) for r in active})
        note = f" (only {', '.join(others)} rulesets, which do not count)"
        return RulesetReport(
            False,
            (f"no active branch ruleset on {repo}{note if others else ''}", OWNER_STEP),
        )
    try:
        default = transport.call("GET", f"repos/{repo}")["default_branch"]
    except (ApiError, KeyError, TypeError) as exc:
        return RulesetReport(None, (f"cannot read the default branch: {exc}",))
    lines: list[str] = []
    fetched: list[int] = []
    failed = unknown = False
    covered = False
    for summary in branch:
        rid = summary.get("id")
        try:
            full = transport.call("GET", f"repos/{repo}/rulesets/{rid}")
        except ApiError as exc:
            lines.append(f"ruleset {rid}: cannot fetch ({exc})")
            unknown = True
            continue
        if isinstance(rid, int):
            fetched.append(rid)
        problems = ruleset_problems(full)
        name = full.get("name", rid)
        hidden = problems == ["bypass_actors missing from the response"]
        if hidden and not transport.authenticated:
            lines.append(f"ruleset {rid} {name!r}: {HOST_STEP}")
            unknown = True
        elif problems:
            lines.append(f"ruleset {rid} {name!r} does not hold: {'; '.join(problems)}")
            failed = True
        else:
            lines.append(f"ruleset {rid} {name!r} holds")
        if (not problems or hidden) and covers_branch(full, default):
            covered = True
    if not covered and not unknown:
        lines.append(f"no holding ruleset covers the default branch {default!r}")
        failed = True
    if failed:
        lines.append(OWNER_STEP)
        return RulesetReport(False, tuple(lines), tuple(fetched))
    return RulesetReport(None if unknown else True, tuple(lines), tuple(fetched))


# ---- the machine identity and the apply


def identity_problems(transport: Transport, repo: str, bot: str) -> list[str]:
    """The agent identity may push and may not administer (Q14)."""
    try:
        reply = transport.call("GET", f"repos/{repo}/collaborators/{bot}/permission")
    except ApiError as exc:
        if exc.status == 404:
            return [
                f"{bot} is not a collaborator on {repo}; answer Q14 in TODO.HUMAN.md "
                "and invite the machine account with write access"
            ]
        return [f"cannot read {bot}'s role on {repo}: {exc}"]
    role = (reply or {}).get("role_name") or (reply or {}).get("permission")
    if role == "admin":
        return [f"{bot} has admin on {repo}; give it write access, never admin"]
    if role not in BOT_ROLES:
        return [f"{bot} has role {role!r} on {repo}; it needs write access"]
    return []


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    ok: bool
    lines: tuple[str, ...]
    ruleset_id: int | None = None


def apply_ruleset(
    transport: Transport, repo: str, bot: str, body: Mapping[str, Any]
) -> ApplyOutcome:
    """Check the identity, create or update tac's ruleset, then judge it by id."""
    problems = identity_problems(transport, repo, bot)
    if problems:
        return ApplyOutcome(False, (*problems, "no ruleset was written"))
    lines = [f"{bot} has write access and no admin on {repo}"]
    try:
        listed = transport.call("GET", f"repos/{repo}/rulesets?per_page=100") or []
        ours = [
            r
            for r in listed
            if r.get("name") == body["name"] and r.get("target") == body["target"]
        ]
        if ours:
            rid = int(ours[0]["id"])
            transport.call("PUT", f"repos/{repo}/rulesets/{rid}", body)
            lines.append(f"updated ruleset {rid} {body['name']!r}")
        else:
            created = transport.call("POST", f"repos/{repo}/rulesets", body)
            rid = int(created["id"])
            lines.append(f"created ruleset {rid} {body['name']!r}")
    except (ApiError, KeyError, TypeError, ValueError) as exc:
        return ApplyOutcome(False, (*lines, f"the ruleset was not applied: {exc}"))
    report = judge_rulesets(transport, repo)
    lines.extend(report.lines)
    return ApplyOutcome(report.ok is True, tuple(lines), rid)
