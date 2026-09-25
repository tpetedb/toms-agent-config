"""Signed receipts: what the runner observed, bound to where and when it looked.

A receipt binds the repository, the revision, the run, the stage and the policy
hash, and is signed with the runner's ed25519 key. Verification takes the public
key from a trusted source only: the base revision's `.agents/config/runner.pub`
or a separately provisioned value, never the tree being judged (build condition
C3 in docs/DESIGN.md). What CI holds a committed receipt to comes from the base
as well: `.agents/config/receipts.toml` names each stage the acceptance gate
knows, the command and the result it must record, and which stages an order
needs; the run is the order, and the revision is the head less its receipts.
Everything here fails closed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import tomllib
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
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    model_validator,
)

from tac.draft07 import draft07

SCHEMA_VERSION = 1
RUNNER_PUB = ".agents/config/runner.pub"
# The files whose committed bytes make up the policy a receipt was taken under.
POLICY_PATHS = (".agents/config.toml", ".agents/config", ".agents/standards.floor.toml")
RECEIPTS_GLOB = "work/orders/*/receipts/*.json"
# What the acceptance gate expects of a committed receipt, read at the base.
EXPECTATIONS = ".agents/config/receipts.toml"
ORDERS = "work/orders"
# Stands for the order id in an expected command or result.
ORDER_TOKEN = "{order}"
# Separates receipt signatures from any other use of the same key.
DOMAIN = b"tac-receipt-v1\n"
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
SHA_PATTERN = r"^[0-9a-f]{40}([0-9a-f]{24})?$"
# The order id rule of `tac work`; a committed receipt sits in its order's folder.
ORDER_PATTERN = r"^[a-z0-9][a-z0-9-]{2,48}$"

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
    # Signed, so a receipt cannot be moved into another order's folder; None for
    # a receipt that stays in the controller store.
    order_id: str | None = Field(default=None, pattern=ORDER_PATTERN)


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
    return draft07(SignedReceipt)


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


# ---- what the acceptance gate expects, from the base revision


class StageExpectation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Kind
    # Each field the receipt's observation must hold exactly: the command it
    # ran and the result that passes. ORDER_TOKEN stands for the order id.
    observed: dict[str, JsonValue] = Field(min_length=1)

    def for_order(self, order: str) -> dict[str, JsonValue]:
        return {k: _fill(v, order) for k, v in self.observed.items()}


class ReceiptPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    # The stages every order a pull request changes must carry a receipt for.
    required: tuple[str, ...] = ()
    stages: dict[str, StageExpectation]

    @model_validator(mode="after")
    def _required_are_stages(self) -> ReceiptPolicy:
        unknown = [name for name in self.required if name not in self.stages]
        if unknown:
            raise ValueError(f"required names unknown stages: {', '.join(unknown)}")
        return self


def _fill(value: JsonValue, order: str) -> JsonValue:
    if isinstance(value, str):
        return value.replace(ORDER_TOKEN, order)
    if isinstance(value, list):
        return [_fill(item, order) for item in value]
    return value


def receipt_policy(root: Path, base: str) -> ReceiptPolicy | None:
    """receipts.toml as committed at the base, or None when the base has none."""
    done = git(root, "show", f"{resolve(root, base)}:{EXPECTATIONS}")
    if done.returncode != 0:
        return None
    try:
        return ReceiptPolicy.model_validate(tomllib.loads(done.stdout))
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ReceiptError(f"{EXPECTATIONS} at the base is invalid: {exc}") from exc


def expectation_problem(receipt: Receipt, order: str, policy: ReceiptPolicy) -> str:
    """Why a verified receipt is not one the acceptance gate asked for, or ''.

    The stage names the expectation; the run is the order; the observation
    holds the expected command and a passing result. None of it is taken from
    the receipt's own say.
    """
    stage = receipt.binding.stage
    expected = policy.stages.get(stage)
    if expected is None:
        known = ", ".join(sorted(policy.stages))
        return f"stage {stage} is not one the acceptance gate expects ({known})"
    if receipt.kind != expected.kind:
        return f"stage {stage} takes a {expected.kind} receipt, not {receipt.kind}"
    if receipt.binding.run_id != order:
        return f"run {receipt.binding.run_id} is not the order's run {order}"
    for name, want in expected.for_order(order).items():
        got = receipt.observed.get(name)
        if got != want:
            return f"observed {name} is {got!r}; stage {stage} expects {want!r}"
    return ""


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


def order_of(path: str) -> str:
    """The order folder a committed receipt sits in, from its repository path."""
    parts = path.split("/")
    if len(parts) != 5 or parts[:2] != ["work", "orders"] or parts[3] != "receipts":
        raise ReceiptError("not under work/orders/<order>/receipts/")
    return parts[2]


def is_receipt_path(path: str) -> bool:
    try:
        order_of(path)
    except ReceiptError:
        return False
    return path.endswith(".json")


def changed_besides_receipts(root: Path, revision: str, head: str) -> list[str]:
    """Paths that differ between a receipt's revision and the head, receipts
    aside: empty when the head is exactly what the runner judged."""
    done = git(root, "diff", "--name-only", "--no-renames", revision, head)
    if done.returncode != 0:
        raise ReceiptError(f"cannot diff {revision[:12]}..{head[:12]}")
    return [p for p in done.stdout.splitlines() if p and not is_receipt_path(p)]


def orders_changed(root: Path, base: str, head: str) -> list[str]:
    """The orders whose folder this change touches, less reference examples and
    orders it removes: each needs the required receipts."""
    done = git(root, "diff", "--name-only", "--no-renames", f"{base}...{head}")
    if done.returncode != 0:
        raise ReceiptError(f"cannot diff {base[:12]}...{head[:12]}")
    found: set[str] = set()
    for path in done.stdout.splitlines():
        parts = path.split("/")
        if len(parts) >= 3 and "/".join(parts[:2]) == ORDERS:
            found.add(parts[2])
    orders: list[str] = []
    for order in sorted(found):
        shown = git(root, "show", f"{head}:{ORDERS}/{order}/order.toml")
        if shown.returncode != 0:
            continue
        try:
            reference = tomllib.loads(shown.stdout).get("reference", False)
        except tomllib.TOMLDecodeError:
            reference = False
        if reference is not True:
            orders.append(order)
    return orders


def unchanged_since(root: Path, base: str, path: str, data: bytes) -> bool:
    """Whether the base revision already holds exactly these bytes at this path."""
    done = subprocess.run(
        ["git", "-C", str(root), "cat-file", "blob", f"{base}:{path}"],
        capture_output=True,
        check=False,
    )
    return done.returncode == 0 and done.stdout == data


def verify_committed(
    root: Path,
    files: Iterable[Path],
    trusted: Ed25519PublicKey | None,
    repository: str,
    base: str,
    head: str,
) -> list[TreeResult]:
    """Judge receipts committed in a candidate tree against where they sit.

    The signature seals the binding; what CI holds it to is recomputed or read
    from the base: the repository, the order folder the receipt is committed
    in, a revision this pull request brought whose tree is the head's less its
    receipts (so a later code commit makes it stale), the policy hash at that
    revision, and the stage, run, command and passing result the base's
    receipts.toml expects. A receipt the base already holds at the same path,
    byte for byte, landed with the base and keeps its old revision; it counts
    toward no requirement. Every order the change touches needs a verified
    receipt for each required stage.
    """
    base_sha = resolve(root, base)
    head_sha = resolve(root, head)
    policy = receipt_policy(root, base_sha)
    seen: set[str] = set()
    results: list[TreeResult] = []
    current: set[tuple[str, str]] = set()
    for path in sorted(files):
        name = path.relative_to(root).as_posix() if path.is_absolute() else str(path)
        try:
            if trusted is None:
                raise MissingTrustRoot("no trusted runner key")
            data = path.read_bytes()
            signed = parse(data.decode("utf-8"))
            claimed = signed.receipt.binding
            if claimed.repository != repository:
                raise ReceiptError(
                    f"bound to {claimed.repository}, this is {repository}"
                )
            order = order_of(name)
            if claimed.order_id != order:
                raise ReceiptError(
                    f"bound to order {claimed.order_id}, committed under {order}"
                )
            if not is_ancestor(root, claimed.revision, head_sha):
                raise ReceiptError(
                    f"revision {claimed.revision[:12]} is not in the head's history"
                )
            landed = unchanged_since(root, base_sha, name, data)
            if not landed:
                if is_ancestor(root, claimed.revision, base_sha):
                    raise ReceiptError(
                        f"revision {claimed.revision[:12]} is already on the base; "
                        "a new receipt must name a revision this change brought"
                    )
                moved = changed_besides_receipts(root, claimed.revision, head_sha)
                if moved:
                    more = f" and {len(moved) - 1} more" if len(moved) > 1 else ""
                    raise ReceiptError(
                        f"the head is not revision {claimed.revision[:12]}: "
                        f"{moved[0]}{more} changed since; a receipt judges the "
                        "head less its receipts"
                    )
                if policy is None:
                    raise ReceiptError(
                        f"the base has no {EXPECTATIONS}, so nothing says what "
                        "a receipt must show"
                    )
            expected = claimed.model_copy(
                update={"policy_hash": policy_hash(root, claimed.revision)}
            )
            receipt = verify(signed, trusted, expected, seen)
            if path.stem != receipt.receipt_id:
                raise ReceiptError("file name is not the receipt id")
            if not landed and policy is not None:
                problem = expectation_problem(receipt, order, policy)
                if problem:
                    raise ReceiptError(problem)
                current.add((order, claimed.stage))
            results.append(TreeResult(name, True, f"{receipt.kind} receipt verified"))
        except (ReceiptError, UnicodeDecodeError) as exc:
            results.append(TreeResult(name, False, str(exc)))
    required = policy.required if policy is not None else ()
    if required:
        for order in orders_changed(root, base_sha, head_sha):
            missing = [stage for stage in required if (order, stage) not in current]
            results.extend(
                TreeResult(
                    f"{ORDERS}/{order}",
                    False,
                    f"no verified {stage} receipt for the head of this change",
                )
                for stage in missing
            )
    return results
