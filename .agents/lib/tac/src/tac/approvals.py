"""What counts as the owner's answer: a host-signed record or a GitHub review.

Design section 7. An item's status in `.human/approvals/<id>.json` is a hint;
the runner believes an approval only when it can verify it:

- a record signed by `tac approve`, which runs only on the host, outside every
  sandbox and agent session, with the runner's ed25519 key, judged against the
  trusted public key (the base revision's runner.pub, never the candidate's);
- an approving review on the pull request by the owner's own account, fetched
  through the GitHub API, on exactly the head the approval is for.

A forged file, a record signed by an unknown key, an approval written by the
agent identity or by the pull request's author, an approval for another head,
another item or another repository, an expired record and a replay are each
refused. No answer is waiting, never consent, however long it has been.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from tac.github import ApiError, Transport, repo_owner
from tac.human import (
    ApprovalRecord,
    Decision,
    HumanPaths,
    Item,
    SignedApproval,
    load_item,
    parse_utc,
    utc_text,
    write_item,
)
from tac.receipts import key_id
from tac.runner import UNIX_PERMS_KEY, UNIX_PERMS_STORE, agent_session, inside_sandbox

# Separates approval signatures from receipts made with the same runner key, so
# a receipt can never be passed off as an approval or the other way round.
DOMAIN = b"tac-approval-v1\n"
DEFAULT_TTL_HOURS = 72
# A review in one of these states is a decision; COMMENTED and PENDING are not.
DECISIVE = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
REVIEWS_PAGE = 100
# 30 pages is 3000 reviews; a pull request past that is refused, not half read,
# since the owner's latest decision could sit on a page never fetched.
MAX_REVIEW_PAGES = 30

State = Literal["approved", "rejected", "waiting", "refused"]


class ApprovalError(Exception):
    """tac approve refuses: the message says why."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the verifier concluded. Only `approved` lets a paused stage resume."""

    state: State
    reason: str
    basis: str | None = None

    @property
    def approved(self) -> bool:
        return self.state == "approved"


def action_sha256(item: Item) -> str:
    """The action an approval is for: pipeline, stage, tool, arguments and pull
    request, canonical JSON, so changing any of them voids the signature."""
    body = {
        "pipeline": item.pipeline,
        "stage": item.stage,
        "tool": item.tool,
        "arguments": item.arguments,
        "pull_request": item.pull_request,
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_bytes(record: ApprovalRecord) -> bytes:
    body = json.dumps(
        record.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return DOMAIN + body.encode("utf-8")


# ---------------------------------------------------------------- signing, host only


def host_refusal(
    environ: Mapping[str, str], ancestors: list[str] | None = None
) -> str | None:
    """Why this process may not sign an approval, or None on the owner's host.

    Inside a sandbox the signer would be an agent's process; in an agent
    session, an agent could ask for the owner's consent to be forged. Both are
    tripwires in front of the real boundary, the key an agent cannot read.
    """
    if inside_sandbox():
        return "this process runs inside a sandbox"
    reason = agent_session(environ, ancestors)
    if reason is not None:
        return f"this is an agent session ({reason})"
    return None


def sign_approval(
    private: Ed25519PrivateKey,
    item: Item,
    *,
    repository: str,
    decision: Decision,
    decided_by: str,
    now: dt.datetime,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> SignedApproval:
    expires = now + dt.timedelta(hours=ttl_hours)
    if item.expires_at is not None:
        expires = min(expires, parse_utc(item.expires_at))
    record = ApprovalRecord(
        approval_id=uuid.uuid4().hex,
        repository=repository,
        item_id=item.id,
        decision=decision,
        action_sha256=action_sha256(item),
        revision=item.revision,
        decided_by=decided_by,
        decided_at=utc_text(now),
        expires_at=utc_text(expires),
        key_id=key_id(private.public_key()),
    )
    signature = private.sign(canonical_bytes(record))
    return SignedApproval(
        approval=record, signature=base64.b64encode(signature).decode("ascii")
    )


def approve(
    paths: HumanPaths,
    item_id: str,
    *,
    private: Ed25519PrivateKey,
    store: Path,
    repository: str,
    decided_by: str,
    agent_identity: str,
    decision: Decision,
    now: dt.datetime,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> Item:
    """Sign the owner's decision on a pending approval item and record it.

    The caller has already refused a sandbox and an agent session. The signed
    record goes into the item and a copy into the controller store.
    """
    item = load_item(paths, item_id)
    if item.kind != "approval":
        raise ApprovalError(
            f"{item_id} is a {item.kind}; answer it with `tac human answer`"
        )
    if not item.open:
        raise ApprovalError(f"{item_id} is already {item.status}; ask again")
    if decided_by.lower() == agent_identity.lower():
        raise ApprovalError(
            f"{decided_by} is the agent identity; an approval is the owner's own"
        )
    if item.expires_at is not None and parse_utc(item.expires_at) <= now:
        raise ApprovalError(
            f"{item_id} expired at {item.expires_at}; a late answer approves "
            "nothing, ask again"
        )
    signed = sign_approval(
        private,
        item,
        repository=repository,
        decision=decision,
        decided_by=decided_by,
        now=now,
        ttl_hours=ttl_hours,
    )
    changed = Item.model_validate(
        {
            **item.model_dump(mode="json"),
            "status": decision,
            "decided_at": signed.approval.decided_at,
            "decided_by": decided_by,
            "approval": signed.model_dump(mode="json"),
        }
    )
    write_item(paths, changed)
    folder = store / "approvals"
    folder.mkdir(parents=True, exist_ok=True, mode=UNIX_PERMS_STORE)
    target = folder / f"{item_id}-{signed.approval.approval_id}.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, UNIX_PERMS_KEY)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(signed.model_dump(mode="json"), indent=2) + "\n")
    return changed


# ---------------------------------------------------------------- verification


def _pending(item: Item, now: dt.datetime) -> Verdict:
    if item.expires_at is not None and parse_utc(item.expires_at) <= now:
        return Verdict(
            "waiting",
            f"no verified answer, and the item timed out at {item.expires_at}; "
            "a timeout is not consent",
        )
    return Verdict("waiting", "no verified answer yet")


def verify_signed(
    item: Item,
    trusted: Ed25519PublicKey | None,
    *,
    repository: str,
    head: str | None,
    agent_identity: str,
    now: dt.datetime,
    consumed: set[str] | None = None,
) -> Verdict:
    """The item's host-signed record, judged against the trusted key.

    `head` is the revision the caller is about to act on; `consumed` collects
    approval ids already used, so the same record presented twice is a replay.
    """
    signed = item.approval
    if signed is None:
        if item.status != "pending":
            return Verdict(
                "refused",
                f"the file says {item.status} but carries no signed record: a "
                "ticked box is a hint, not an answer",
            )
        return _pending(item, now)
    if trusted is None:
        return Verdict("refused", "no trusted runner key to judge the signature")
    record = signed.approval
    if record.key_id != key_id(trusted):
        return Verdict(
            "refused",
            f"signed by key {record.key_id}, not the trusted runner key "
            f"{key_id(trusted)}",
        )
    try:
        raw = base64.b64decode(signed.signature, validate=True)
        trusted.verify(raw, canonical_bytes(record))
    except (InvalidSignature, ValueError):
        return Verdict("refused", "the signature does not match the record")
    problems: list[str] = []
    if record.item_id != item.id:
        problems.append(f"it approves item {record.item_id}, not {item.id}")
    if record.repository != repository:
        problems.append(f"it is for {record.repository}, this is {repository}")
    if record.action_sha256 != action_sha256(item):
        problems.append("the action or its arguments changed since it was signed")
    if record.revision != item.revision:
        problems.append(
            f"it is for revision {record.revision}, the item asks about {item.revision}"
        )
    if head is not None and record.revision != head:
        problems.append(
            f"it is for revision {str(record.revision)[:12]}, not the head {head[:12]}"
        )
    if record.decided_by.lower() in {
        agent_identity.lower(),
        (item.agent or "").lower(),
    }:
        problems.append(
            f"it is self-authored: {record.decided_by} asked or acts for the agents"
        )
    if parse_utc(record.expires_at) <= now:
        problems.append(f"it expired at {record.expires_at}")
    if problems:
        return Verdict("refused", "; ".join(problems))
    if consumed is not None:
        if record.approval_id in consumed:
            return Verdict("refused", f"approval {record.approval_id} was used before")
        consumed.add(record.approval_id)
    if record.decision == "rejected":
        return Verdict("rejected", "the owner rejected it on the host", "host-signed")
    return Verdict("approved", "signed on the host by the runner key", "host-signed")


def _login(user: Any) -> str:
    return str((user or {}).get("login") or "")


def list_reviews(transport: Transport, repository: str, number: int) -> list[Any]:
    """Every review on the pull request, page by page, oldest first.

    A short page is the last one. Raises ApiError when the list runs past
    MAX_REVIEW_PAGES or a page is not a list.
    """
    reviews: list[Any] = []
    for page in range(1, MAX_REVIEW_PAGES + 1):
        body = transport.call(
            "GET",
            f"repos/{repository}/pulls/{number}/reviews"
            f"?per_page={REVIEWS_PAGE}&page={page}",
        )
        if not isinstance(body, list):
            raise ApiError(None, f"page {page} of the reviews is not a list")
        reviews += body
        if len(body) < REVIEWS_PAGE:
            return reviews
    raise ApiError(
        None,
        f"pull request #{number} has more than "
        f"{MAX_REVIEW_PAGES * REVIEWS_PAGE} reviews; too many to read them all",
    )


def verify_github(
    item: Item,
    transport: Transport,
    *,
    repository: str,
    head: str | None,
    agent_identity: str,
    now: dt.datetime,
) -> Verdict:
    """An approving review by the owner's own account on the item's pull
    request, on the head the item is for.

    The owner is the account that owns the repository, as GitHub says; the
    latest decisive review of that account decides. A review by the agent
    identity or by the pull request's author is never the owner's approval.
    """
    number = item.pull_request
    if number is None:
        return Verdict("refused", f"{item.id} names no pull request")
    try:
        owner, owner_type = repo_owner(transport, repository)
        if owner_type != "User":
            return Verdict(
                "refused",
                f"{repository} belongs to an organisation; which account is the "
                "owner is not settled, so no review can count",
            )
        pull = transport.call("GET", f"repos/{repository}/pulls/{number}")
        reviews = list_reviews(transport, repository, number)
    except (ApiError, KeyError, TypeError) as exc:
        return Verdict("refused", f"GitHub did not answer: {exc}")
    if owner.lower() == agent_identity.lower():
        return Verdict("refused", f"the repository owner {owner} is the agent identity")
    pr_head = str(((pull or {}).get("head") or {}).get("sha") or "")
    author = _login((pull or {}).get("user"))
    if item.revision is not None and pr_head != item.revision:
        return Verdict(
            "refused",
            f"pull request #{number} is at {pr_head[:12]}, the approval is for "
            f"{item.revision[:12]}",
        )
    if head is not None and pr_head != head:
        return Verdict(
            "refused",
            f"pull request #{number} is at {pr_head[:12]}, not the head {head[:12]}",
        )
    if author.lower() == owner.lower():
        return Verdict(
            "refused",
            f"the owner opened pull request #{number}; an author's review never "
            "counts, so the owner's edits travel as agent pull requests",
        )
    listed = [r for r in reviews if isinstance(r, Mapping)]
    mine = [
        r
        for r in listed
        if _login(r.get("user")).lower() == owner.lower()
        and (r.get("user") or {}).get("type") == "User"
        and r.get("state") in DECISIVE
    ]
    if not mine:
        selfish = [
            r
            for r in listed
            if r.get("state") == "APPROVED"
            and _login(r.get("user")).lower()
            in {agent_identity.lower(), author.lower(), (item.agent or "").lower()}
        ]
        if selfish:
            who = _login(selfish[-1].get("user"))
            return Verdict(
                "refused",
                f"the only approval is self-authored, by {who}; only the owner's "
                "own account counts",
            )
        return _pending(item, now)
    latest = max(
        mine, key=lambda r: (str(r.get("submitted_at") or ""), r.get("id") or 0)
    )
    state = latest.get("state")
    commit = str(latest.get("commit_id") or "")
    if state == "DISMISSED":
        return _pending(item, now)
    if item.expires_at is not None:
        submitted = str(latest.get("submitted_at") or "")
        if submitted > item.expires_at or parse_utc(item.expires_at) <= now:
            return Verdict(
                "refused",
                f"the item expired at {item.expires_at}; a review after that, or "
                "an approval used after it, approves nothing: ask again",
            )
    if commit != pr_head:
        return Verdict(
            "refused",
            f"the owner's review is on {commit[:12]}, not the head {pr_head[:12]}; "
            "approve the current head",
        )
    if state == "CHANGES_REQUESTED":
        return Verdict("rejected", f"{owner} requested changes", "github-review")
    return Verdict("approved", f"{owner} approved the head", "github-review")
