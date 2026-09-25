"""External effects, journaled before they run (design sections 5 and 10).

An effect is something that leaves a trace outside the worker's sandbox: a
commit, a push, a pull request, an issue, a comment, a render. Only the trusted
runner performs one, as `tac-bot`, and only after re-checking it: the effect is
in policy.toml `runner_only` and never in `owner_only`, and where the active
profile says `approvals = "external-effects"`, an effect that leaves the machine
names an approval item whose host-signed record `approvals.verify_signed`
accepts, with the ids already acted on read from the controller store, where
this one is then appended with the run and the stage it was spent for (the M2
hand-off, section 7). So an approval is used once: another run or stage that
presents it is refused, while the stage it was spent for may present it again
to finish effects a failure interrupted, which the journal keeps from running
twice.

Every effect is journaled before it runs, under an idempotency key over the
repository, the run, the stage, the effect and its arguments, then marked done
or failed with what was observed. After a crash, `reconcile` looks at every
entry still pending and marks done what already happened (a push whose remote
branch holds the sha, a commit already on the branch, a pull request already
open), so a retry never publishes twice. Each effect performed gets a receipt
of kind effect, signed by the runner over what it observed itself.

In M3 the runner performs commit, push and open-pr. open-issue, comment and
render are journaled and refused until M4 performs them.

The runner's git never runs a hook, an fsmonitor or the owner's global config,
and reaches GitHub only through the `tac runner credential` helper, which reads
the tac-bot token from the keychain; `gh` gets the token as GH_TOKEN in its own
environment and nowhere else.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from tac import human
from tac.approvals import verify_signed
from tac.config import Config
from tac.github import agent_identity
from tac.keychain import BOT_TOKEN, Keychain, KeychainError
from tac.receipts import ID_PATTERN, ORDER_PATTERN, Binding, policy_hash
from tac.runner import UNIX_PERMS_STORE, Runner, RunnerError

EFFECTS_DIR = "effects"
CONSUMED = "approvals/consumed.jsonl"
PERFORMED = frozenset({"commit", "push", "open-pr"})
FROM_M4 = frozenset({"open-issue", "comment", "render"})
# What leaves the machine, and so waits for an approval under external-effects;
# a commit and a render stay in the worktree until a push takes them out.
EXTERNAL = frozenset({"push", "open-pr", "open-issue", "comment"})
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
PR_URL = re.compile(r"/pull/(\d+)\s*$")
EFFECT_TRAILER = "Tac-Effect"
GIT_TIMEOUT_S = 300
# Whatever the owner's global or system git config says (a credential helper
# holding the owner's token, hooks, aliases), the runner's git reads none of it.
GIT_ISOLATION = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}
GIT_FLAGS = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")

State = Literal["pending", "done", "failed"]


class EffectError(Exception):
    """An effect is refused or failed: the message says why."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EffectRequest(_Model):
    effect: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    arguments: dict[str, JsonValue]
    run_id: str = Field(pattern=ID_PATTERN)
    stage: str = Field(pattern=ID_PATTERN)
    order_id: str | None = Field(default=None, pattern=ORDER_PATTERN)
    # The approval item in the owner's queue that authorises it, by id.
    approval_id: str | None = Field(default=None, pattern=human.ITEM_ID)


class JournalEntry(_Model):
    key: str
    state: State
    repository: str
    effect: str
    arguments: dict[str, JsonValue]
    run_id: str
    stage: str
    order_id: str | None
    created: str
    modified: str
    observed: dict[str, JsonValue] = Field(default_factory=dict)
    error: str | None = None


def utc_text() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def idempotency_key(
    repository: str, run_id: str, stage: str, effect: str, arguments: Mapping
) -> str:
    body = json.dumps(
        [repository, run_id, stage, effect, arguments],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def action(requests: Sequence[EffectRequest]) -> tuple[str, dict[str, JsonValue]]:
    """What an approval item binds for these effects: its tool and arguments."""
    tool = "+".join(r.effect for r in requests)
    arguments: dict[str, JsonValue] = {
        "effects": [{"effect": r.effect, "arguments": r.arguments} for r in requests]
    }
    return tool, arguments


def _text(arguments: Mapping[str, JsonValue], name: str, effect: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value or "\0" in value:
        raise EffectError(f"{effect} needs a text argument {name}")
    return value


def _branch(arguments: Mapping[str, JsonValue], effect: str) -> str:
    branch = _text(arguments, "branch", effect)
    if not BRANCH.match(branch) or ".." in branch or branch.endswith((".lock", "/")):
        raise EffectError(f"{effect}: {branch!r} is not a branch name the runner uses")
    return branch


def _sha(arguments: Mapping[str, JsonValue], effect: str) -> str:
    sha = _text(arguments, "sha", effect)
    if not SHA.match(sha):
        raise EffectError(f"{effect}: {sha!r} is not a commit sha")
    return sha


@dataclass(frozen=True, slots=True)
class Effects:
    """The effects one runner performs for one repository."""

    runner: Runner
    config: Config
    # The login keychain, the tac-bot token's only source; None refuses a push
    # or a pull request, never falls back to another credential.
    keychain: Keychain | None
    # The interpreter that runs `tac runner credential` for git: the runner's own.
    python: str = sys.executable

    # -- where things live

    @property
    def journal(self) -> Path:
        folder = self.runner.store / EFFECTS_DIR
        folder.mkdir(parents=True, exist_ok=True, mode=UNIX_PERMS_STORE)
        return folder

    def entry(self, key: str) -> JournalEntry | None:
        path = self.journal / f"{key}.json"
        if not path.is_file():
            return None
        return JournalEntry.model_validate_json(path.read_text(encoding="utf-8"))

    def _write(self, entry: JournalEntry) -> None:
        # Written whole and renamed, so a crash leaves the old entry or the new.
        path = self.journal / f"{entry.key}.json"
        temp = path.with_suffix(".json.tmp")
        temp.write_text(entry.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temp.replace(path)

    # -- checking

    def check_policy(self, effect: str) -> None:
        policy = self.config.policy.effects
        if effect in policy.owner_only:
            raise EffectError(
                f"{effect} is the owner's own action (policy.toml owner_only), "
                "never the runner's"
            )
        if effect not in policy.runner_only:
            raise EffectError(f"{effect} is not in policy.toml runner_only")

    def consumed_path(self) -> Path:
        path = self.runner.store / CONSUMED
        path.parent.mkdir(parents=True, exist_ok=True, mode=UNIX_PERMS_STORE)
        return path

    def _consumed_records(self) -> list[dict[str, str]]:
        path = self.consumed_path()
        if not path.is_file():
            return []
        found: list[dict[str, str]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                found.append({k: str(v) for k, v in record.items()})
            except (ValueError, AttributeError):
                continue
        return [r for r in found if "approval_id" in r]

    def consumed(self, spent_for: tuple[str, str] | None = None) -> set[str]:
        """Approval ids already acted on; with `spent_for` (a run and a stage),
        less the ones spent for exactly that stage of that run, which it may
        present again to finish what they authorised."""
        return {
            r["approval_id"]
            for r in self._consumed_records()
            if spent_for is None or (r.get("run_id"), r.get("stage")) != spent_for
        }

    def needs_approval(self, requests: Sequence[EffectRequest]) -> bool:
        return self.config.profile.approvals == "external-effects" and any(
            r.effect in EXTERNAL for r in requests
        )

    def authorize(
        self,
        requests: Sequence[EffectRequest],
        approval_id: str | None,
        head: str | None,
    ) -> None:
        """Policy for each effect, then, where the profile asks, one verified
        approval for all of them, unused or spent for this same run and stage;
        it is marked used here, before the effects run, so no other run or
        stage can present it while they do."""
        for request in requests:
            self.check_policy(request.effect)
        if not self.needs_approval(requests):
            return
        if approval_id is None:
            raise EffectError(
                "the profile asks for an approval before an external effect "
                '(approvals = "external-effects"), and the request names none'
            )
        paths = human.human_paths(self.runner.root)
        try:
            item = human.load_item(paths, approval_id)
        except human.HumanError as exc:
            raise EffectError(str(exc)) from exc
        tool, arguments = action(requests)
        if item.tool != tool or item.arguments != arguments:
            raise EffectError(
                f"approval {approval_id} is for {item.tool}, not for these effects "
                "with these arguments"
            )
        bindings = {(r.run_id, r.stage) for r in requests}
        spent_for = next(iter(bindings)) if len(bindings) == 1 else None
        seen = self.consumed(spent_for)
        # Read before the verdict, which adds the id it judged to `seen`.
        mine = self.consumed() - seen
        verdict = verify_signed(
            item,
            self.runner.private.public_key(),
            repository=self.runner.repository,
            head=head,
            agent_identity=agent_identity(self.runner.root),
            now=human.utc_now(),
            consumed=seen,
        )
        if not verdict.approved:
            raise EffectError(
                f"approval {approval_id}: {verdict.state}, {verdict.reason}"
            )
        assert item.approval is not None
        approval = item.approval.approval.approval_id
        if approval in mine:
            # Spent for this stage already: a resume after a failure.
            return
        with self.consumed_path().open("a", encoding="utf-8") as handle:
            record = {
                "approval_id": approval,
                "item_id": item.id,
                "at": utc_text(),
            }
            if spent_for is not None:
                record["run_id"], record["stage"] = spent_for
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    # -- performing

    def key(self, request: EffectRequest) -> str:
        return idempotency_key(
            self.runner.repository,
            request.run_id,
            request.stage,
            request.effect,
            request.arguments,
        )

    def perform(self, request: EffectRequest) -> dict[str, JsonValue]:
        """Journal, then perform once: an entry already done is answered from
        the journal, and a pending one is reconciled before anything reruns."""
        key = self.key(request)
        found = self.entry(key)
        if found is not None and found.state == "pending":
            found = self.reconcile_entry(found)
        if found is not None and found.state == "done":
            return dict(found.observed)
        now = utc_text()
        entry = JournalEntry(
            key=key,
            state="pending",
            repository=self.runner.repository,
            effect=request.effect,
            arguments=request.arguments,
            run_id=request.run_id,
            stage=request.stage,
            order_id=request.order_id,
            created=found.created if found else now,
            modified=now,
        )
        self._write(entry)
        try:
            if request.effect in FROM_M4:
                raise EffectError(
                    f"{request.effect} is journaled but not performed until M4; "
                    "the request stays in the journal"
                )
            if request.effect not in PERFORMED:
                raise EffectError(f"{request.effect} is not an effect the runner knows")
            observed = self._run(request, key)
        except (EffectError, RunnerError, KeychainError, OSError) as exc:
            self._write(
                entry.model_copy(
                    update={
                        "state": "failed",
                        "modified": utc_text(),
                        "error": str(exc),
                    }
                )
            )
            raise EffectError(str(exc)) from exc
        observed["receipt_id"] = self._receipt(request, key, observed)
        self._write(
            entry.model_copy(
                update={"state": "done", "modified": utc_text(), "observed": observed}
            )
        )
        return observed

    def _run(self, request: EffectRequest, key: str) -> dict[str, JsonValue]:
        args = request.arguments
        if request.effect == "commit":
            return self.commit(
                _branch(args, "commit"), _text(args, "message", "commit"), key
            )
        if request.effect == "push":
            return self.push(_branch(args, "push"), _sha(args, "push"))
        return self.open_pr(
            _branch(args, "open-pr"),
            _text(args, "base", "open-pr"),
            _text(args, "title", "open-pr"),
            _text(args, "body", "open-pr"),
        )

    def _receipt(
        self, request: EffectRequest, key: str, observed: Mapping[str, JsonValue]
    ) -> str:
        sha = observed.get("sha")
        revision = sha if isinstance(sha, str) and SHA.match(sha) else None
        if revision is None:
            revision = self._git(self.runner.root, "rev-parse", "HEAD").strip()
        binding = Binding(
            repository=self.runner.repository,
            revision=revision,
            run_id=request.run_id,
            stage=request.stage,
            policy_hash=policy_hash(self.runner.root, revision),
            order_id=request.order_id,
        )
        signed = self.runner.issue(
            "effect",
            binding,
            {
                "effect": request.effect,
                "key": key,
                "arguments_sha256": hashlib.sha256(
                    json.dumps(request.arguments, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "observed": dict(observed),
            },
        )
        receipt = signed["receipt"]
        assert isinstance(receipt, dict)
        inner = receipt["receipt"]
        assert isinstance(inner, dict)
        return str(inner["receipt_id"])

    # -- git and gh, as the runner runs them

    def _git_env(self) -> dict[str, str]:
        return {
            "PATH": self.runner.search_path,
            "HOME": str(self.runner.store),
            "LANG": "C",
            **GIT_ISOLATION,
        }

    def _git(self, where: Path, *args: str, extra: Sequence[str] = ()) -> str:
        argv = [self.runner.which("git"), "-C", str(where), *GIT_FLAGS, *extra, *args]
        try:
            done = subprocess.run(
                argv,
                env=self._git_env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EffectError(f"git {args[0]} did not finish") from exc
        if done.returncode != 0:
            raise EffectError(f"git {args[0]} failed: {done.stderr.strip()[-400:]}")
        return done.stdout

    def worktree(self, branch: str) -> Path:
        """The checkout of `branch` among this repository's worktrees; a path is
        never taken from a request."""
        listing = self._git(self.runner.root, "worktree", "list", "--porcelain")
        path: str | None = None
        for line in listing.splitlines():
            if line.startswith("worktree "):
                path = line.removeprefix("worktree ")
            elif line == f"branch refs/heads/{branch}" and path is not None:
                return Path(path)
        raise EffectError(f"no worktree of this repository has {branch} checked out")

    def _identity(self) -> list[str]:
        name = agent_identity(self.runner.root)
        return [
            "-c",
            f"user.name={name}",
            "-c",
            f"user.email={name}@users.noreply.github.com",
        ]

    def commit(self, branch: str, message: str, key: str) -> dict[str, JsonValue]:
        where = self.worktree(branch)
        self._git(where, "add", "-A")
        staged = subprocess.run(
            [
                self.runner.which("git"),
                "-C",
                str(where),
                *GIT_FLAGS,
                "diff",
                "--cached",
                "--quiet",
            ],
            env=self._git_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        if staged.returncode == 0:
            head = self._git(where, "rev-parse", "HEAD").strip()
            return {"sha": head, "branch": branch, "committed": False}
        body = f"{message.rstrip()}\n\n{EFFECT_TRAILER}: {key}\n"
        self._commit(where, body)
        head = self._git(where, "rev-parse", "HEAD").strip()
        return {"sha": head, "branch": branch, "committed": True}

    def _commit(self, where: Path, body: str) -> None:
        argv = [
            self.runner.which("git"),
            "-C",
            str(where),
            *GIT_FLAGS,
            *self._identity(),
            "commit",
            "--no-verify",
            "-q",
            "-F",
            "-",
        ]
        done = subprocess.run(
            argv,
            input=body,
            env=self._git_env(),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
        if done.returncode != 0:
            raise EffectError(f"git commit failed: {done.stderr.strip()[-400:]}")

    def credential_helper(self) -> list[str]:
        """git -c options that drop every configured helper and add the runner's."""
        if self.keychain is None:
            raise EffectError(
                "no keychain holds the tac-bot token; run `just runner-keychain` "
                "on the host"
            )
        helper = shlex.join(
            [
                self.python,
                "-m",
                "tac",
                "runner",
                "credential",
                "--slug",
                self.keychain.slug,
                "--security",
                self.keychain.security,
            ]
        )
        return ["-c", "credential.helper=", "-c", f"credential.helper=!{helper}"]

    def push(self, branch: str, sha: str) -> dict[str, JsonValue]:
        where = self.worktree(branch)
        self._git(
            where,
            "push",
            "--no-verify",
            "-q",
            "origin",
            f"{sha}:refs/heads/{branch}",
            extra=self.credential_helper(),
        )
        return {"sha": sha, "branch": branch, "pushed": True}

    def _gh(self, *args: str) -> str:
        if self.keychain is None:
            raise EffectError(
                "no keychain holds the tac-bot token; run `just runner-keychain` "
                "on the host"
            )
        token = self.keychain.read(BOT_TOKEN)
        # The token goes into this one child's environment and nowhere else.
        env = {
            "PATH": self.runner.search_path,
            "HOME": str(self.runner.store),
            "GH_CONFIG_DIR": str(self.runner.store / "gh"),
            "GH_TOKEN": token,
            "GH_PROMPT_DISABLED": "1",
            "NO_COLOR": "1",
            "LANG": "C",
        }
        if not self.config.runtime.env.gh_telemetry:
            env["GH_TELEMETRY"] = "false"
        try:
            done = subprocess.run(
                [self.runner.which("gh"), *args],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EffectError(f"gh {args[0]} did not finish") from exc
        if done.returncode != 0:
            raise EffectError(
                f"gh {' '.join(args[:2])} failed: {done.stderr.strip()[-400:]}"
            )
        return done.stdout

    def open_prs(self, branch: str) -> list[dict[str, JsonValue]]:
        out = self._gh(
            "pr",
            "list",
            "--repo",
            self.runner.repository,
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number,url,headRefOid",
        )
        try:
            found = json.loads(out or "[]")
        except json.JSONDecodeError as exc:
            raise EffectError("gh pr list answered something that is not JSON") from exc
        if not isinstance(found, list):
            raise EffectError("gh pr list answered something that is not a list")
        return [p for p in found if isinstance(p, dict)]

    def open_pr(
        self, branch: str, base: str, title: str, body: str
    ) -> dict[str, JsonValue]:
        if not BRANCH.match(base):
            raise EffectError(f"open-pr: {base!r} is not a base branch")
        existing = self.open_prs(branch)
        if existing:
            first = existing[0]
            return {
                "number": first.get("number"),
                "url": first.get("url"),
                "branch": branch,
            }
        body_file = self.runner.store / EFFECTS_DIR / f"body-{os.getpid()}.md"
        body_file.write_text(body, encoding="utf-8")
        try:
            out = self._gh(
                "pr",
                "create",
                "--repo",
                self.runner.repository,
                "--head",
                branch,
                "--base",
                base,
                "--title",
                title,
                "--body-file",
                str(body_file),
            )
        finally:
            body_file.unlink(missing_ok=True)
        url = out.strip().splitlines()[-1] if out.strip() else ""
        match = PR_URL.search(url)
        if match is None:
            raise EffectError(f"gh pr create printed no pull request url: {url!r}")
        return {"number": int(match.group(1)), "url": url, "branch": branch}

    # -- after a crash

    def reconcile_entry(self, entry: JournalEntry) -> JournalEntry:
        """A pending entry marked done when its effect already happened; left
        pending when nothing shows it did."""
        observed: dict[str, JsonValue] | None = None
        args = entry.arguments
        try:
            if entry.effect == "push":
                branch, sha = _branch(args, "push"), _sha(args, "push")
                where = self.worktree(branch)
                out = self._git(
                    where,
                    "ls-remote",
                    "origin",
                    f"refs/heads/{branch}",
                    extra=self.credential_helper() if self.keychain else (),
                )
                if out.split() and out.split()[0] == sha:
                    observed = {"sha": sha, "branch": branch, "pushed": True}
            elif entry.effect == "commit":
                branch = _branch(args, "commit")
                where = self.worktree(branch)
                out = self._git(
                    where,
                    "log",
                    "--format=%H",
                    "-n",
                    "1",
                    f"--grep={EFFECT_TRAILER}: {entry.key}",
                    "--fixed-strings",
                    "HEAD",
                )
                if out.strip():
                    observed = {"sha": out.strip(), "branch": branch, "committed": True}
            elif entry.effect == "open-pr":
                existing = self.open_prs(_branch(args, "open-pr"))
                if existing:
                    first = existing[0]
                    observed = {
                        "number": first.get("number"),
                        "url": first.get("url"),
                        "branch": _branch(args, "open-pr"),
                    }
        except (EffectError, KeychainError, RunnerError):
            return entry
        if observed is None:
            return entry
        observed["reconciled"] = True
        done = entry.model_copy(
            update={"state": "done", "modified": utc_text(), "observed": observed}
        )
        self._write(done)
        return done

    def reconcile(self) -> list[JournalEntry]:
        """Every pending entry re-examined; the ones found done are returned."""
        settled = []
        for path in sorted(self.journal.glob("*.json")):
            try:
                entry = JournalEntry.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValidationError):
                continue
            if entry.state != "pending":
                continue
            after = self.reconcile_entry(entry)
            if after.state == "done":
                settled.append(after)
        return settled
