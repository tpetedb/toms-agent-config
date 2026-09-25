"""What counts as the owner's answer (design section 7, C3): a record signed on
the host by `tac approve` with the runner key, or an approving review by the
owner's own account on the pull request, fetched from recorded GitHub replies.

Each way an approval can be faked is refused: a forged file, an unknown key, a
self-authored approval, another head, another item, a replay, and a timeout,
which is never consent."""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import tac.human_cli
from tac import approvals, human
from tac.approvals import Verdict, sign_approval, verify_github, verify_signed
from tac.cli import cli
from tac.receipts import Binding, sign
from tac.runner import (
    controller_store,
    create_key,
    ensure_store,
    load_key,
    pub_file_text,
)
from tests._github_fixtures import REPO as GH_REPO
from tests._github_fixtures import FixtureTransport, Reply, load
from tests._gitrepo import REPOSITORY, commit_all, git, make_repo

REPO = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.UTC)
BOT = "tac-bot"
OWNER = "example"
PR = 1347
PR_HEAD = "6dcb09b5b57875f334f61aebed695e2e4193db5e"
OTHER = "0" * 40


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests may run under an agent client or its sandbox; the refusals
    have their own tests with the markers set back."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    monkeypatch.setattr("tac.runner.ancestor_commands", lambda _pid: [])
    monkeypatch.setattr("tac.approvals.inside_sandbox", lambda: False)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "demo"
    make_repo(root)
    (root / ".human").mkdir()
    shutil.copy(REPO / human.TODO_SOURCE, root / human.TODO_SOURCE)
    monkeypatch.setenv("TAC_STATE_HOME", str(tmp_path / "state"))
    return root


@pytest.fixture
def store(project: Path) -> Path:
    path = ensure_store(controller_store(project, os.environ))
    create_key(path)
    return path


def head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD").strip()


def approval_item(root: Path, **changes: object) -> human.Item:
    body: dict[str, object] = {
        "id": "Q30",
        "kind": "approval",
        "topic": "approvals",
        "rank": 10,
        "title": "push",
        "question": "Push tac/m2 as the machine account?",
        "recommendation": "yes.",
        "blocks": ["the pull request"],
        "waits": "M2",
        "pipeline": "order",
        "stage": "land",
        "agent": "builder-1",
        "tool": "git push",
        "arguments": {"branch": "tac/m2"},
        "revision": head(root),
        "asked_at": "2026-09-25T11:00:00Z",
        "expires_at": "2026-09-27T11:00:00Z",
    }
    body |= changes
    item = human.Item.model_validate(body)
    human.write_item(human.human_paths(root), item)
    return item


def signed_item(
    item: human.Item, private: Ed25519PrivateKey, **changes: object
) -> human.Item:
    signed = sign_approval(
        private,
        item,
        repository=REPOSITORY,
        decision="approved",
        decided_by=str(changes.pop("decided_by", OWNER)),
        now=NOW,
    )
    return item.model_copy(
        update={
            "approval": signed,
            "status": "approved",
            "decided_at": signed.approval.decided_at,
            "decided_by": signed.approval.decided_by,
            **changes,
        }
    )


def judge(item: human.Item, key: Ed25519PrivateKey, **kw: object) -> Verdict:
    args: dict[str, object] = {
        "repository": REPOSITORY,
        "head": item.revision,
        "agent_identity": BOT,
        "now": NOW,
    }
    args |= kw
    return verify_signed(item, key.public_key(), **args)  # type: ignore[arg-type]


def run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, list(args))
    return result.exit_code, result.output


# ---------------------------------------------------------------- tac approve


def test_approve_signs_on_the_host_and_the_signature_verifies(
    project: Path, store: Path
) -> None:
    approval_item(project)
    code, out = run("approve", "Q30", "--root", str(project))
    assert code == 0, out
    item = human.load_item(human.human_paths(project), "Q30")
    assert item.status == "approved" and item.approval is not None
    record = item.approval.approval
    assert record.decided_by == OWNER and record.revision == head(project)
    assert record.repository == REPOSITORY and record.item_id == "Q30"
    # Never past the item's own expiry.
    assert record.expires_at <= "2026-09-27T11:00:00Z"
    key = load_key(store)
    now = human.parse_utc(record.decided_at)
    verdict = judge(item, key, now=now)
    assert verdict == Verdict("approved", verdict.reason, "host-signed")
    copies = list((store / "approvals").glob("Q30-*.json"))
    assert len(copies) == 1 and copies[0].stat().st_mode & 0o077 == 0
    page = (project / "TODO.HUMAN.md").read_text()
    assert "- [x] Q30 push:" in page and "signed on the host with key" in page


def test_approve_signs_a_rejection_too(project: Path, store: Path) -> None:
    approval_item(project)
    assert run("approve", "Q30", "--reject", "--root", str(project))[0] == 0
    item = human.load_item(human.human_paths(project), "Q30")
    now = human.parse_utc(item.approval.approval.decided_at)  # type: ignore[union-attr]
    assert judge(item, load_key(store), now=now).state == "rejected"


def test_approve_is_refused_inside_a_sandbox(
    project: Path, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_item(project)
    monkeypatch.setattr("tac.approvals.inside_sandbox", lambda: True)
    code, out = run("approve", "Q30", "--root", str(project))
    assert code == 1 and "inside a sandbox" in out
    assert human.load_item(human.human_paths(project), "Q30").status == "pending"
    assert not (store / "approvals").exists()


@pytest.mark.parametrize("marker", ["CLAUDECODE", "CODEX_SANDBOX"])
def test_approve_is_refused_inside_an_agent_session(
    project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    approval_item(project)
    monkeypatch.setenv(marker, "1")
    code, out = run("approve", "Q30", "--root", str(project))
    assert code == 1 and "agent session" in out
    assert human.load_item(human.human_paths(project), "Q30").approval is None


def test_approve_needs_the_runner_key(project: Path) -> None:
    approval_item(project)
    code, out = run("approve", "Q30", "--root", str(project))
    assert code == 1 and "no signing key" in out


def test_approve_takes_only_a_pending_approval_item(project: Path, store: Path) -> None:
    approval_item(project)
    assert run("approve", "Q30", "--root", str(project))[0] == 0
    code, out = run("approve", "Q30", "--root", str(project))
    assert code == 1 and "already approved" in out
    approval_item(project, id="Q31", kind="question", tool=None)
    code, out = run("approve", "Q31", "--root", str(project))
    assert code == 1 and "tac human answer" in out


def test_a_late_approval_approves_nothing(project: Path, store: Path) -> None:
    approval_item(project, expires_at="2026-01-01T00:00:00Z")
    code, out = run("approve", "Q30", "--root", str(project))
    assert code == 1 and "expired" in out and "ask again" in out


def test_approve_refuses_the_agent_identity_as_the_decider(
    project: Path, store: Path
) -> None:
    approval_item(project)
    with pytest.raises(approvals.ApprovalError, match="agent identity"):
        approvals.approve(
            human.human_paths(project),
            "Q30",
            private=load_key(store),
            store=store,
            repository=REPOSITORY,
            decided_by=BOT,
            agent_identity=BOT,
            decision="approved",
            now=NOW,
        )


# ---------------------------------------------------------------- forged and foreign


def test_a_forged_file_is_refused(project: Path) -> None:
    item = approval_item(project).model_copy(
        update={"status": "approved", "decided_at": "2026-09-25T11:30:00Z"}
    )
    item = item.model_copy(update={"decided_by": OWNER})
    verdict = judge(item, Ed25519PrivateKey.generate())
    assert verdict.state == "refused" and "a ticked box is a hint" in verdict.reason


def test_a_forged_signature_is_refused(project: Path) -> None:
    key = Ed25519PrivateKey.generate()
    item = signed_item(approval_item(project), key)
    assert item.approval is not None
    fake = item.approval.model_copy(
        update={"signature": base64.b64encode(b"\0" * 64).decode("ascii")}
    )
    verdict = judge(item.model_copy(update={"approval": fake}), key)
    assert verdict.state == "refused" and "does not match" in verdict.reason


def test_a_record_changed_after_signing_is_refused(project: Path) -> None:
    key = Ed25519PrivateKey.generate()
    item = signed_item(approval_item(project), key)
    assert item.approval is not None
    record = item.approval.approval.model_copy(
        update={"expires_at": "2099-01-01T00:00:00Z"}
    )
    tampered = item.approval.model_copy(update={"approval": record})
    verdict = judge(item.model_copy(update={"approval": tampered}), key)
    assert verdict.state == "refused" and "does not match" in verdict.reason


def test_an_approval_signed_by_an_unknown_key_is_refused(project: Path) -> None:
    runner_key, stranger = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    item = signed_item(approval_item(project), stranger)
    verdict = judge(item, runner_key)
    assert verdict.state == "refused" and "not the trusted runner key" in verdict.reason
    assert (
        verify_signed(
            item, None, repository=REPOSITORY, head=None, agent_identity=BOT, now=NOW
        ).state
        == "refused"
    )


def test_a_receipt_signature_is_not_an_approval(project: Path) -> None:
    """The same runner key signs receipts; the domain keeps the two apart."""
    key = Ed25519PrivateKey.generate()
    item = signed_item(approval_item(project), key)
    assert item.approval is not None
    binding = Binding(
        repository=REPOSITORY,
        revision=head(project),
        run_id="run-1",
        stage="verify",
        policy_hash="sha256:" + "0" * 64,
    )
    receipt = sign(key, "gate", binding, item.approval.approval.model_dump(mode="json"))
    swapped = item.approval.model_copy(update={"signature": receipt.signature})
    verdict = judge(item.model_copy(update={"approval": swapped}), key)
    assert verdict.state == "refused"


@pytest.mark.parametrize("who", [BOT, "builder-1"])
def test_a_self_authored_approval_is_refused(project: Path, who: str) -> None:
    key = Ed25519PrivateKey.generate()
    item = signed_item(approval_item(project), key, decided_by=who)
    verdict = judge(item, key)
    assert verdict.state == "refused" and "self-authored" in verdict.reason


def test_an_approval_for_a_different_head_is_refused(project: Path) -> None:
    key = Ed25519PrivateKey.generate()
    item = signed_item(approval_item(project), key)
    (project / "later.txt").write_text("x")
    moved = commit_all(project, "later")
    verdict = judge(item, key, head=moved)
    assert verdict.state == "refused" and "not the head" in verdict.reason
    # Nor can the item be pointed at the new head after the owner signed.
    repointed = item.model_copy(update={"revision": moved})
    verdict = judge(repointed, key, head=moved)
    assert verdict.state == "refused" and "revision" in verdict.reason


@pytest.mark.parametrize(
    "change",
    [
        {"id": "Q31"},
        {"arguments": {"branch": "main"}},
        {"tool": "gh pr merge"},
        {"stage": "release"},
    ],
    ids=["other-item", "other-arguments", "other-tool", "other-stage"],
)
def test_an_approval_moved_to_another_action_is_refused(
    project: Path, change: dict[str, object]
) -> None:
    key = Ed25519PrivateKey.generate()
    item = signed_item(approval_item(project), key)
    verdict = judge(item.model_copy(update=change), key)
    assert verdict.state == "refused"


def test_an_approval_for_another_repository_is_refused(project: Path) -> None:
    key = Ed25519PrivateKey.generate()
    item = signed_item(approval_item(project), key)
    verdict = judge(item, key, repository="example/other")
    assert verdict.state == "refused" and "example/other" in verdict.reason


def test_a_replayed_approval_is_refused(project: Path) -> None:
    key = Ed25519PrivateKey.generate()
    item = signed_item(approval_item(project), key)
    used: set[str] = set()
    assert judge(item, key, consumed=used).approved
    verdict = judge(item, key, consumed=used)
    assert verdict.state == "refused" and "used before" in verdict.reason


# ---------------------------------------------------------------- time


def test_a_timeout_is_never_consent(project: Path) -> None:
    item = approval_item(project, expires_at="2026-09-25T11:30:00Z")
    verdict = judge(item, Ed25519PrivateKey.generate())
    assert verdict.state == "waiting" and not verdict.approved
    assert "a timeout is not consent" in verdict.reason
    long_after = judge(
        item, Ed25519PrivateKey.generate(), now=NOW + dt.timedelta(days=365)
    )
    assert not long_after.approved


def test_an_expired_approval_is_refused(project: Path) -> None:
    key = Ed25519PrivateKey.generate()
    item = signed_item(approval_item(project), key)
    verdict = judge(item, key, now=NOW + dt.timedelta(days=30))
    assert verdict.state == "refused" and "expired" in verdict.reason


def test_the_knob_file_cannot_make_a_timeout_consent() -> None:
    from pydantic import ValidationError

    from tac.config_schema import Human

    base = {
        "todo": "TODO.HUMAN.md",
        "recap_dir": ".human/recap",
        "approvals_dir": ".human/approvals",
        "verify": ["host-signed"],
    }
    Human.model_validate(base | {"timeout_is_consent": False})
    with pytest.raises(ValidationError):
        Human.model_validate(base | {"timeout_is_consent": True})


# ---------------------------------------------------------------- tac human verify


def test_verify_reads_the_trusted_key_and_exits_by_verdict(
    project: Path, store: Path, tmp_path: Path
) -> None:
    key = load_key(store)
    pub = tmp_path / "runner.pub"
    pub.write_text(pub_file_text(key))
    approval_item(project)
    args = ("human", "verify", "Q30", "--root", str(project), "--pub", str(pub))
    code, out = run(*args)
    assert code == approvals_exit("waiting"), out
    assert run("approve", "Q30", "--root", str(project))[0] == 0
    code, out = run(*args)
    assert code == 0 and "Q30: approved" in out
    stranger = tmp_path / "stranger.pub"
    stranger.write_text(pub_file_text(Ed25519PrivateKey.generate()))
    code, out = run(
        "human", "verify", "Q30", "--root", str(project), "--pub", str(stranger)
    )
    assert code == 1 and "refused" in out


def test_verify_takes_the_key_from_the_base_revision_only(
    project: Path, store: Path
) -> None:
    approval_item(project)
    assert run("approve", "Q30", "--root", str(project))[0] == 0
    # No runner.pub at the base: nothing can be verified.
    code, out = run("human", "verify", "Q30", "--root", str(project), "--base", "HEAD")
    assert code == 1 and "runner.pub is missing" in out
    # A key only in the working tree is the candidate's and never trusted.
    pub = project / ".agents/config/runner.pub"
    pub.write_text(pub_file_text(load_key(store)))
    code, out = run("human", "verify", "Q30", "--root", str(project), "--base", "HEAD")
    assert code == 1 and "runner.pub is missing" in out


def approvals_exit(state: str) -> int:
    return {"approved": 0, "waiting": 3, "rejected": 4}.get(state, 1)


# ---------------------------------------------------------------- GitHub reviews

GET = "GET"


def reviews_page(page: int) -> str:
    return f"repos/{GH_REPO}/pulls/{PR}/reviews?per_page=100&page={page}"


def github(
    pull: dict | None = None,
    reviews: list | None = None,
    repo_body: dict | None = None,
) -> FixtureTransport:
    return FixtureTransport(
        {
            ("GET", f"repos/{GH_REPO}"): [Reply(200, repo_body or load("repo"))],
            ("GET", f"repos/{GH_REPO}/pulls/{PR}"): [Reply(200, pull or load("pull"))],
            (GET, reviews_page(1)): [
                Reply(200, load("pull_reviews") if reviews is None else reviews)
            ],
        }
    )


def pr_item(project: Path, **changes: object) -> human.Item:
    return approval_item(
        project, **({"pull_request": PR, "revision": PR_HEAD} | changes)
    )


def by_github(item: human.Item, transport: FixtureTransport, **kw: object) -> Verdict:
    args: dict[str, object] = {
        "repository": GH_REPO,
        "head": PR_HEAD,
        "agent_identity": BOT,
        "now": NOW,
    }
    args |= kw
    return verify_github(item, transport, **args)  # type: ignore[arg-type]


def review(**changes: object) -> dict:
    body = load("pull_reviews")[0]
    user = changes.pop("login", None)
    if user is not None:
        body["user"]["login"] = user
    body.update(changes)
    return body


def test_the_owners_review_on_the_head_approves(project: Path) -> None:
    transport = github()
    verdict = by_github(pr_item(project), transport)
    assert verdict == Verdict("approved", verdict.reason, "github-review")
    assert transport.writes() == []


def test_a_review_by_the_agent_identity_is_self_authored(project: Path) -> None:
    verdict = by_github(pr_item(project), github(reviews=[review(login=BOT)]))
    assert verdict.state == "refused" and "self-authored" in verdict.reason


def test_a_pull_request_the_owner_opened_is_self_authored(project: Path) -> None:
    pull = load("pull")
    pull["user"]["login"] = OWNER
    verdict = by_github(pr_item(project), github(pull=pull))
    assert verdict.state == "refused" and "author" in verdict.reason


def test_a_review_of_an_older_head_is_refused(project: Path) -> None:
    transport = github(reviews=[review(commit_id=OTHER)])
    verdict = by_github(pr_item(project), transport)
    assert verdict.state == "refused" and "not the head" in verdict.reason


def test_a_pull_request_that_moved_past_the_item_is_refused(project: Path) -> None:
    item = pr_item(project, revision=OTHER)
    verdict = by_github(item, github(), head=None)
    assert verdict.state == "refused" and "the approval is for" in verdict.reason
    verdict = by_github(pr_item(project), github(), head=OTHER)
    assert verdict.state == "refused" and "not the head" in verdict.reason


def test_the_latest_decisive_review_of_the_owner_decides(project: Path) -> None:
    later = review(
        id=81, state="CHANGES_REQUESTED", submitted_at="2019-11-18T09:00:00Z"
    )
    verdict = by_github(pr_item(project), github(reviews=[review(), later]))
    assert verdict.state == "rejected"
    dismissed = review(id=82, state="DISMISSED", submitted_at="2019-11-19T09:00:00Z")
    verdict = by_github(pr_item(project), github(reviews=[review(), dismissed]))
    assert verdict.state == "waiting"
    comment = review(id=83, state="COMMENTED", submitted_at="2019-11-20T09:00:00Z")
    assert by_github(pr_item(project), github(reviews=[review(), comment])).approved


def test_another_accounts_approval_is_not_the_owners(project: Path) -> None:
    verdict = by_github(pr_item(project), github(reviews=[review(login="someone")]))
    assert verdict.state == "waiting"


def test_a_bot_that_carries_the_owners_login_is_not_the_owner(project: Path) -> None:
    """Only a review by a user account counts; an app or bot never does, even
    when GitHub reports it under the owner's login."""
    bot = review()
    bot["user"]["type"] = "Bot"
    assert bot["user"]["login"] == OWNER
    verdict = by_github(pr_item(project), github(reviews=[bot]))
    assert verdict.state == "waiting", verdict.reason


def test_a_repository_the_agent_identity_owns_is_refused(project: Path) -> None:
    """When the agent identity owns the repository, the owner's review would be
    the agent's own, so nothing it says is an approval."""
    verdict = by_github(pr_item(project), github(), agent_identity=OWNER)
    assert verdict.state == "refused"
    assert f"the repository owner {OWNER} is the agent identity" in verdict.reason


def test_no_review_is_waiting_even_after_the_timeout(project: Path) -> None:
    item = pr_item(project, expires_at="2026-09-25T11:30:00Z")
    verdict = by_github(item, github(reviews=[]))
    assert verdict.state == "waiting" and "a timeout is not consent" in verdict.reason


def test_a_review_after_the_item_expired_is_refused(project: Path) -> None:
    item = pr_item(project, expires_at="2019-11-17T00:00:00Z")
    verdict = by_github(item, github())
    assert verdict.state == "refused" and "expired" in verdict.reason


def test_every_page_of_reviews_is_read(project: Path) -> None:
    """The owner's approval on page two decides; page one alone would wait."""
    others = [review(id=1000 + n, login=f"reader{n}") for n in range(100)]
    transport = github(reviews=others)
    transport.replies[(GET, reviews_page(2))] = [Reply(200, [review()])]
    verdict = by_github(pr_item(project), transport)
    assert verdict.approved, verdict.reason
    asked = [path for _, path, _ in transport.calls if "/reviews" in path]
    assert asked == [reviews_page(1), reviews_page(2)]


def test_a_pull_request_with_too_many_reviews_is_refused_not_half_read(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "MAX_REVIEW_PAGES", 2)
    full = [review(id=2000 + n, login=f"reader{n}") for n in range(100)]
    transport = github(reviews=full)
    transport.replies[(GET, reviews_page(2))] = [Reply(200, full)]
    verdict = by_github(pr_item(project), transport)
    assert verdict.state == "refused" and "too many" in verdict.reason


def test_an_organisation_repository_counts_no_review(project: Path) -> None:
    body = load("repo")
    body["owner"]["type"] = "Organization"
    verdict = by_github(pr_item(project), github(repo_body=body))
    assert verdict.state == "refused" and "organisation" in verdict.reason


@pytest.mark.parametrize(
    ("reviews", "code", "word"),
    [
        ("approved", 0, "approved"),
        ("none", 3, "waiting"),
        ("changes", 4, "rejected"),
        ("bot", 1, "refused"),
    ],
)
def test_verify_asks_github_through_the_owners_gh_and_exits_by_verdict(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    reviews: str,
    code: int,
    word: str,
) -> None:
    # The recorded pull request, moved onto this repository's own head.
    sha = head(project)
    pull = load("pull")
    pull["head"]["sha"] = sha
    listed = {
        "approved": [review(commit_id=sha)],
        "none": [],
        "changes": [review(commit_id=sha, state="CHANGES_REQUESTED")],
        "bot": [review(commit_id=sha, login=BOT)],
    }[reviews]
    approval_item(project, pull_request=PR)
    transport = github(pull=pull, reviews=listed)
    monkeypatch.setattr(tac.human_cli, "gh_transport", lambda: transport)
    got, out = run("human", "verify", "Q30", "--root", str(project), "--repo", GH_REPO)
    assert got == code and f"Q30: {word}" in out, out
    assert transport.writes() == []


def test_the_item_file_records_nothing_the_verifier_trusts(project: Path) -> None:
    """Whatever the file says, the verdict comes from the signature or GitHub."""
    item = pr_item(project)
    data = json.loads(item.to_json())
    data |= {"status": "approved", "decided_at": "2026-09-25T11:10:00Z"}
    data |= {"decided_by": OWNER}
    forged = human.Item.model_validate(data)
    assert by_github(forged, github(reviews=[])).state == "waiting"
