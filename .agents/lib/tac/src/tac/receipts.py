"""Signed receipts: what the runner observed, bound to where and when it looked.

A receipt binds the repository, the revision, the run, the stage and the policy
hash, and is signed with the runner's ed25519 key. Verification takes the public
key from a trusted source only: the base revision's `.agents/config/runner.pub`
or a separately provisioned value, never the tree being judged (build condition
C3 in docs/DESIGN.md). Everything here fails closed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

SCHEMA_VERSION = 1
RUNNER_PUB = ".agents/config/runner.pub"
# The files whose committed bytes make up the policy a receipt was taken under.
POLICY_PATHS = (".agents/config.toml", ".agents/config", ".agents/standards.floor.toml")
RECEIPTS_GLOB = "work/orders/*/receipts/*.json"
# Separates receipt signatures from any other use of the same key.
DOMAIN = b"tac-receipt-v1\n"
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
SHA_PATTERN = r"^[0-9a-f]{40}([0-9a-f]{24})?$"

Kind = Literal["gate", "probe", "dispatch", "effect"]


class ReceiptError(Exception):
    """A receipt, or the key to judge it, cannot be trusted."""


class MissingTrustRoot(ReceiptError):
    """No trusted public key: nothing can be verified."""


class Binding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    revision: str = Field(pattern=SHA_PATTERN)
    run_id: str = Field(pattern=ID_PATTERN)
    stage: str = Field(pattern=ID_PATTERN)
    policy_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class Receipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    receipt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: Kind
    binding: Binding
    observed: dict[str, JsonValue]
    created: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    key_id: str = Field(pattern=r"^[0-9a-f]{16}$")


class SignedReceipt(BaseModel):
    # extra="forbid" also refuses a receipt that tries to carry its own key.
    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt: Receipt
    signature: str

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def receipt_json_schema() -> dict[str, JsonValue]:
    """The signed receipt as JSON Schema draft-07, generated from the models."""
    schema = SignedReceipt.model_json_schema(ref_template="#/definitions/{model}")
    definitions = schema.pop("$defs", {})
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        **schema,
        "definitions": definitions,
    }


def canonical_bytes(receipt: Receipt) -> bytes:
    body = json.dumps(
        receipt.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return DOMAIN + body.encode("utf-8")


def key_id(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def public_pem(public: Ed25519PublicKey) -> str:
    return public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")


def load_public_key(text: str) -> Ed25519PublicKey:
    """The ed25519 key in a PEM block; comments around it are allowed."""
    match = re.search(
        r"-----BEGIN PUBLIC KEY-----.+?-----END PUBLIC KEY-----", text, re.DOTALL
    )
    if match is None:
        raise MissingTrustRoot("runner.pub holds no public key")
    try:
        key = serialization.load_pem_public_key(match.group(0).encode("ascii"))
    except ValueError as exc:
        raise MissingTrustRoot(f"runner.pub does not parse: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise MissingTrustRoot("runner.pub is not an ed25519 key")
    return key


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sign(
    private: Ed25519PrivateKey,
    kind: Kind,
    binding: Binding,
    observed: dict[str, JsonValue],
) -> SignedReceipt:
    receipt = Receipt(
        receipt_id=uuid.uuid4().hex,
        kind=kind,
        binding=binding,
        observed=observed,
        created=utc_now(),
        key_id=key_id(private.public_key()),
    )
    signature = private.sign(canonical_bytes(receipt))
    return SignedReceipt(
        receipt=receipt, signature=base64.b64encode(signature).decode("ascii")
    )


def parse(text: str) -> SignedReceipt:
    try:
        return SignedReceipt.model_validate_json(text, strict=False)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "receipt"
        raise ReceiptError(f"malformed receipt at {where}: {first['msg']}") from exc


def check_signature(signed: SignedReceipt, trusted: Ed25519PublicKey) -> Receipt:
    receipt = signed.receipt
    if receipt.key_id != key_id(trusted):
        raise ReceiptError(
            f"signed by key {receipt.key_id}, not the trusted runner key "
            f"{key_id(trusted)}"
        )
    try:
        signature = base64.b64decode(signed.signature, validate=True)
        trusted.verify(signature, canonical_bytes(receipt))
    except (InvalidSignature, ValueError) as exc:
        raise ReceiptError("signature does not match the receipt") from exc
    return receipt


def verify(
    signed: SignedReceipt,
    trusted: Ed25519PublicKey | None,
    expected: Binding,
    seen: set[str] | None = None,
) -> Receipt:
    """The receipt, if the trusted key signed it for exactly this binding.

    `seen` collects receipt ids across one verification pass, so the same
    receipt presented twice is a replay.
    """
    if trusted is None:
        raise MissingTrustRoot("no trusted runner key")
    receipt = check_signature(signed, trusted)
    if receipt.binding != expected:
        diff = [
            name
            for name in Binding.model_fields
            if getattr(receipt.binding, name) != getattr(expected, name)
        ]
        raise ReceiptError(f"receipt is bound elsewhere: {', '.join(diff)} differ")
    if seen is not None:
        if receipt.receipt_id in seen:
            raise ReceiptError(f"receipt {receipt.receipt_id} presented twice")
        seen.add(receipt.receipt_id)
    return receipt


# ---- git: the only place a trusted key or a policy hash is read from


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def resolve(root: Path, ref: str) -> str:
    done = git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if done.returncode != 0:
        raise ReceiptError(f"revision {ref!r} is not a commit here")
    return done.stdout.strip()


def trusted_key_from_revision(root: Path, base: str) -> Ed25519PublicKey:
    """runner.pub as committed at the base revision, never the working tree."""
    revision = resolve(root, base)
    done = git(root, "show", f"{revision}:{RUNNER_PUB}")
    if done.returncode != 0:
        raise MissingTrustRoot(f"{RUNNER_PUB} is missing at the base revision")
    return load_public_key(done.stdout)


def policy_hash(root: Path, revision: str) -> str:
    """sha256 over the committed blob ids of the policy files at a revision."""
    commit = resolve(root, revision)
    done = git(root, "ls-tree", "-r", "--full-tree", commit, "--", *POLICY_PATHS)
    if done.returncode != 0:
        raise ReceiptError(f"cannot list the policy at {commit}: {done.stderr.strip()}")
    return "sha256:" + hashlib.sha256(done.stdout.encode("utf-8")).hexdigest()


def is_ancestor(root: Path, revision: str, head: str) -> bool:
    return git(root, "merge-base", "--is-ancestor", revision, head).returncode == 0


@dataclass(frozen=True, slots=True)
class TreeResult:
    path: str
    ok: bool
    detail: str


def verify_committed(
    root: Path,
    files: Iterable[Path],
    trusted: Ed25519PublicKey | None,
    repository: str,
    head: str,
) -> list[TreeResult]:
    """Judge receipts committed in a candidate tree.

    The run and stage are sealed by the signature; what can be recomputed is:
    the repository, that the revision is in the head's history, and the policy
    hash at that revision.
    """
    head_sha = resolve(root, head)
    seen: set[str] = set()
    results: list[TreeResult] = []
    for path in sorted(files):
        name = path.relative_to(root).as_posix() if path.is_absolute() else str(path)
        try:
            if trusted is None:
                raise MissingTrustRoot("no trusted runner key")
            signed = parse(path.read_text(encoding="utf-8"))
            claimed = signed.receipt.binding
            if claimed.repository != repository:
                raise ReceiptError(
                    f"bound to {claimed.repository}, this is {repository}"
                )
            if not is_ancestor(root, claimed.revision, head_sha):
                raise ReceiptError(
                    f"revision {claimed.revision[:12]} is not in the head's history"
                )
            expected = claimed.model_copy(
                update={"policy_hash": policy_hash(root, claimed.revision)}
            )
            receipt = verify(signed, trusted, expected, seen)
            if path.stem != receipt.receipt_id:
                raise ReceiptError("file name is not the receipt id")
            results.append(TreeResult(name, True, f"{receipt.kind} receipt verified"))
        except ReceiptError as exc:
            results.append(TreeResult(name, False, str(exc)))
    return results
