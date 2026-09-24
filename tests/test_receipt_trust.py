"""Build condition C3: a receipt counts only when the base's runner key signed it
for exactly this repository, revision, run, stage, order and policy; CI holds a
committed receipt to the order folder it sits in and a revision the change brought."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tac.cli import cli
from tac.receipts import (
    ORDER_PATTERN,
    RUNNER_PUB,
    Binding,
    MissingTrustRoot,
    ReceiptError,
    SignedReceipt,
    load_public_key,
    parse,
    policy_hash,
    receipt_json_schema,
    sign,
    trusted_key_from_revision,
    verify,
)
from tac.runner import pub_file_text
from tac.work import ID as WORK_ORDER_ID
from tests._gitrepo import REPOSITORY, commit_all, git, make_repo, write

RUN = "run-1"
STAGE = "verify"
ORDER = "demo"


@pytest.fixture
def runner_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def attacker_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def repo(tmp_path: Path, runner_key: Ed25519PrivateKey) -> Path:
    root = tmp_path / "demo"
    make_repo(root, runner_pub=pub_file_text(runner_key))
    write(root, "src/app.py", "print('observed')\n")
    commit_all(root, "candidate work")
    return root


def head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD")


def binding(root: Path, **changes: str) -> Binding:
    revision = changes.pop("revision", head(root))
    fields = {
        "repository": REPOSITORY,
        "revision": revision,
        "run_id": RUN,
        "stage": STAGE,
        "policy_hash": policy_hash(root, revision),
        "order_id": ORDER,
    }
    fields.update(changes)
    return Binding(**fields)


def gate_receipt(key: Ed25519PrivateKey, bound: Binding) -> SignedReceipt:
    return sign(key, "gate", bound, {"argv": ["just", "verify"], "exit": 0})


def commit_receipt(root: Path, signed: SignedReceipt, order: str = ORDER) -> Path:
    relative = f"work/orders/{order}/receipts/{signed.receipt.receipt_id}.json"
    path = write(root, relative, signed.to_json())
    commit_all(root, "receipt")
    return path


def verify_tree(root: Path, base: str, *extra: str) -> tuple[int, str]:
    result = CliRunner().invoke(
        cli,
        ["receipt", "verify-tree", "--base", base, "--repository", REPOSITORY,
         "--repo", str(root), *extra],
    )  # fmt: skip
    return result.exit_code, result.output


# ---- the one that passes


def test_a_valid_receipt_verifies_against_the_base_key(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    bound = binding(repo)
    signed = gate_receipt(runner_key, bound)
    trusted = trusted_key_from_revision(repo, "HEAD~1")
    receipt = verify(parse(signed.to_json()), trusted, bound)
    assert receipt.observed["exit"] == 0
    assert receipt.binding == bound


def test_a_valid_committed_receipt_passes_the_ci_verifier(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    base = git(repo, "rev-parse", "HEAD~1")
    commit_receipt(repo, gate_receipt(runner_key, binding(repo)))
    code, output = verify_tree(repo, base)
    assert code == 0, output
    assert "gate receipt verified" in output


def test_a_tree_without_receipts_needs_no_key(tmp_path: Path) -> None:
    root = tmp_path / "bare"
    make_repo(root, runner_pub=None)
    code, output = verify_tree(root, "HEAD")
    assert code == 0
    assert "no committed receipts" in output


# ---- replay: a genuine signature, presented where it was not taken


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "example/other"),
        ("run_id", "run-2"),
        ("stage", "review"),
        ("order_id", "other-order"),
    ],
)
def test_a_receipt_replayed_into_another_context_is_rejected(
    repo: Path, runner_key: Ed25519PrivateKey, field: str, value: str
) -> None:
    signed = gate_receipt(runner_key, binding(repo))
    trusted = trusted_key_from_revision(repo, "HEAD~1")
    with pytest.raises(ReceiptError, match=f"bound elsewhere: {field}"):
        verify(signed, trusted, binding(repo, **{field: value}))


def test_a_receipt_for_another_revision_is_rejected(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    older = git(repo, "rev-parse", "HEAD~1")
    signed = gate_receipt(runner_key, binding(repo, revision=older))
    trusted = trusted_key_from_revision(repo, "HEAD~1")
    with pytest.raises(ReceiptError, match="revision"):
        verify(signed, trusted, binding(repo))


def test_the_same_receipt_twice_in_one_pass_is_a_replay(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    bound = binding(repo)
    signed = gate_receipt(runner_key, bound)
    trusted = trusted_key_from_revision(repo, "HEAD~1")
    seen: set[str] = set()
    verify(signed, trusted, bound, seen)
    with pytest.raises(ReceiptError, match="presented twice"):
        verify(signed, trusted, bound, seen)


def test_a_receipt_copied_into_a_second_order_is_rejected_by_ci(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    base = git(repo, "rev-parse", "HEAD~1")
    signed = gate_receipt(runner_key, binding(repo))
    commit_receipt(repo, signed)
    commit_receipt(repo, signed, order="second")
    code, output = verify_tree(repo, base)
    assert code == 1
    assert f"ok   work/orders/{ORDER}/" in output
    assert "FAIL work/orders/second/" in output
    assert f"bound to order {ORDER}, committed under second" in output


# ---- where a committed receipt sits: CI judges it there, not by its own claim


def test_a_receipt_moved_into_another_order_is_rejected_by_ci(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    signed = gate_receipt(runner_key, binding(repo, order_id="first"))
    moved = commit_receipt(repo, signed, order="first")
    base = git(repo, "rev-parse", "HEAD")
    target = f"work/orders/second/receipts/{moved.name}"
    (repo / target).parent.mkdir(parents=True)
    git(repo, "mv", str(moved.relative_to(repo)), target)
    commit_all(repo, "move the receipt to another order")
    code, output = verify_tree(repo, base)
    assert code == 1
    assert "bound to order first, committed under second" in output


def test_a_receipt_whose_order_is_not_its_folder_is_rejected_by_ci(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    base = git(repo, "rev-parse", "HEAD~1")
    commit_receipt(
        repo, gate_receipt(runner_key, binding(repo, order_id="first")), "second"
    )
    code, output = verify_tree(repo, base)
    assert code == 1
    assert "bound to order first, committed under second" in output


def test_a_receipt_bound_to_no_order_is_rejected_by_ci(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    base = git(repo, "rev-parse", "HEAD~1")
    unbound = binding(repo).model_copy(update={"order_id": None})
    commit_receipt(repo, gate_receipt(runner_key, unbound))
    code, output = verify_tree(repo, base)
    assert code == 1
    assert f"bound to order None, committed under {ORDER}" in output


def test_a_new_receipt_for_a_revision_already_on_the_base_is_rejected_by_ci(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    # A genuine receipt for an older revision of main, replayed into a new change.
    base = git(repo, "rev-parse", "HEAD")
    old = gate_receipt(runner_key, binding(repo, revision=base))
    write(repo, "src/later.py", "print('later')\n")
    commit_all(repo, "later work")
    commit_receipt(repo, old)
    code, output = verify_tree(repo, base)
    assert code == 1
    assert "is already on the base" in output


def test_a_receipt_the_base_already_holds_unchanged_still_passes(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    commit_receipt(repo, gate_receipt(runner_key, binding(repo)))
    base = git(repo, "rev-parse", "HEAD")
    write(repo, "src/later.py", "print('later')\n")
    commit_all(repo, "later work")
    code, output = verify_tree(repo, base)
    assert code == 0, output
    assert "gate receipt verified" in output


def test_a_receipt_the_base_holds_but_the_candidate_rewrote_is_rejected_by_ci(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    path = commit_receipt(repo, gate_receipt(runner_key, binding(repo)))
    base = git(repo, "rev-parse", "HEAD")
    path.write_text(path.read_text("utf-8").replace("\n", "\n\n"), "utf-8")
    commit_all(repo, "reformat the receipt")
    code, output = verify_tree(repo, base)
    assert code == 1
    assert "is already on the base" in output


def test_a_receipt_from_another_repository_is_rejected_by_ci(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    base = git(repo, "rev-parse", "HEAD~1")
    commit_receipt(
        repo, gate_receipt(runner_key, binding(repo, repository="example/other"))
    )
    code, output = verify_tree(repo, base)
    assert code == 1
    assert "bound to example/other" in output


def test_a_receipt_for_a_revision_outside_the_history_is_rejected_by_ci(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    base = git(repo, "rev-parse", "HEAD~1")
    git(repo, "switch", "-q", "-c", "elsewhere", base)
    write(repo, "src/other.py", "print('elsewhere')\n")
    elsewhere = commit_all(repo, "a side branch")
    signed = gate_receipt(runner_key, binding(repo, revision=elsewhere))
    git(repo, "switch", "-q", "main")
    commit_receipt(repo, signed)
    code, output = verify_tree(repo, base)
    assert code == 1
    assert "not in the head's history" in output


# ---- an altered field


def altered(signed: SignedReceipt, edit: str) -> SignedReceipt:
    data = json.loads(signed.to_json())
    receipt = data["receipt"]
    if edit == "exit":
        receipt["observed"]["exit"] = 1
    elif edit == "argv":
        receipt["observed"]["argv"] = ["true"]
    elif edit == "created":
        receipt["created"] = "2020-01-01T00:00:00Z"
    elif edit == "kind":
        receipt["kind"] = "effect"
    elif edit == "signature":
        data["signature"] = data["signature"][:-4] + "AAA="
    return parse(json.dumps(data))


@pytest.mark.parametrize("edit", ["exit", "argv", "created", "kind", "signature"])
def test_an_altered_field_is_rejected(
    repo: Path, runner_key: Ed25519PrivateKey, edit: str
) -> None:
    bound = binding(repo)
    signed = altered(gate_receipt(runner_key, bound), edit)
    trusted = trusted_key_from_revision(repo, "HEAD~1")
    with pytest.raises(ReceiptError, match="signature"):
        verify(signed, trusted, bound)


def test_an_altered_binding_is_rejected_even_when_it_matches_the_expectation(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    signed = gate_receipt(runner_key, binding(repo))
    data = json.loads(signed.to_json())
    data["receipt"]["binding"]["stage"] = "release"
    trusted = trusted_key_from_revision(repo, "HEAD~1")
    with pytest.raises(ReceiptError, match="signature"):
        verify(parse(json.dumps(data)), trusted, binding(repo, stage="release"))


def test_an_altered_committed_receipt_is_rejected_by_ci(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    base = git(repo, "rev-parse", "HEAD~1")
    signed = altered(gate_receipt(runner_key, binding(repo)), "exit")
    commit_receipt(repo, signed)
    code, output = verify_tree(repo, base)
    assert code == 1
    assert "signature does not match" in output


def test_a_receipt_under_a_changed_policy_is_rejected_by_ci(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    base = git(repo, "rev-parse", "HEAD~1")
    stale = binding(repo).model_copy(update={"policy_hash": "sha256:" + "0" * 64})
    commit_receipt(repo, gate_receipt(runner_key, stale))
    code, output = verify_tree(repo, base)
    assert code == 1
    assert "policy_hash differ" in output


# ---- a key the candidate supplies


def test_a_candidate_supplied_key_is_rejected(
    repo: Path, runner_key: Ed25519PrivateKey, attacker_key: Ed25519PrivateKey
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    # The candidate replaces runner.pub and signs its own receipt with its key.
    write(repo, RUNNER_PUB, pub_file_text(attacker_key))
    commit_all(repo, "candidate swaps the runner key")
    forged = gate_receipt(attacker_key, binding(repo))
    with pytest.raises(ReceiptError, match="not the trusted runner key"):
        verify(forged, trusted_key_from_revision(repo, base), binding(repo))
    commit_receipt(repo, forged)
    code, output = verify_tree(repo, base)
    assert code == 1
    assert "not the trusted runner key" in output


def test_an_uncommitted_key_in_the_working_tree_is_never_read(
    repo: Path, runner_key: Ed25519PrivateKey, attacker_key: Ed25519PrivateKey
) -> None:
    write(repo, RUNNER_PUB, pub_file_text(attacker_key))
    trusted = trusted_key_from_revision(repo, "HEAD")
    assert trusted.public_bytes_raw() == runner_key.public_key().public_bytes_raw()


def test_a_receipt_that_carries_its_own_key_is_malformed(
    repo: Path, attacker_key: Ed25519PrivateKey
) -> None:
    data = json.loads(gate_receipt(attacker_key, binding(repo)).to_json())
    data["public_key"] = pub_file_text(attacker_key)
    with pytest.raises(ReceiptError, match="malformed"):
        parse(json.dumps(data))


def test_a_provisioned_key_is_used_instead_of_the_base_one(
    repo: Path, attacker_key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = git(repo, "rev-parse", "HEAD~1")
    provisioned = Ed25519PrivateKey.generate()
    commit_receipt(repo, gate_receipt(provisioned, binding(repo)))
    monkeypatch.setenv("TAC_RUNNER_PUB", pub_file_text(provisioned))
    code, output = verify_tree(repo, base, "--key-env", "TAC_RUNNER_PUB")
    assert code == 0, output


# ---- no key at all


def test_a_base_without_runner_pub_is_a_missing_trust_root(tmp_path: Path) -> None:
    root = tmp_path / "keyless"
    make_repo(root, runner_pub=None)
    with pytest.raises(MissingTrustRoot, match="missing at the base revision"):
        trusted_key_from_revision(root, "HEAD")


def test_the_unprovisioned_placeholder_is_a_missing_trust_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "placeholder"
    make_repo(root, runner_pub="# Not provisioned yet.\n")
    with pytest.raises(MissingTrustRoot, match="holds no public key"):
        trusted_key_from_revision(root, "HEAD")


def test_verify_without_a_key_rejects(
    repo: Path, runner_key: Ed25519PrivateKey
) -> None:
    bound = binding(repo)
    with pytest.raises(MissingTrustRoot):
        verify(gate_receipt(runner_key, bound), None, bound)


def test_ci_rejects_every_receipt_when_the_base_has_no_key(
    tmp_path: Path, runner_key: Ed25519PrivateKey
) -> None:
    root = tmp_path / "keyless"
    base = make_repo(root, runner_pub=None)
    commit_receipt(root, gate_receipt(runner_key, binding(root, revision=base)))
    code, output = verify_tree(root, base)
    assert code == 1
    assert "missing at the base revision" in output
    assert "FAIL" in output


def test_the_committed_runner_pub_is_a_key_or_holds_none() -> None:
    text = (Path(__file__).resolve().parents[1] / RUNNER_PUB).read_text("utf-8")
    try:
        load_public_key(text)
    except MissingTrustRoot as exc:
        assert "holds no public key" in str(exc)


def test_the_receipt_contract_matches_the_models() -> None:
    contract = Path(__file__).resolve().parents[1] / "contracts/receipt.schema.json"
    committed = json.loads(contract.read_text("utf-8"))
    assert committed == receipt_json_schema(), "run: tac receipt schema > " + str(
        contract.relative_to(contract.parents[1])
    )
    assert committed["$schema"] == "http://json-schema.org/draft-07/schema#"
    assert committed["additionalProperties"] is False


def test_the_receipt_order_rule_is_the_work_order_rule() -> None:
    assert WORK_ORDER_ID.pattern == ORDER_PATTERN
