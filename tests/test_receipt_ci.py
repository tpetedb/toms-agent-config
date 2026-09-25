"""Build condition C3 in CI: scripts/ci_verify_receipts.sh judges the candidate's
receipts with the base revision's own verifier and key, so a pull request that
brings its own key, its own verifier, or both, still has its forgeries refused."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tac.receipts import Binding, SignedReceipt, policy_hash, sign
from tac.runner import pub_file_text
from tests._gitrepo import (
    REPOSITORY,
    commit_all,
    copy_toolchain,
    expectations,
    git,
    make_repo,
    write,
)

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ci_verify_receipts.sh"
# A candidate verifier that passes everything; it must never be the one that runs.
LENIENT = """

def verify_committed(root, files, *args):
    return [TreeResult(str(p), True, "accepted") for p in files]
"""


@pytest.fixture
def runner_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def base_repo(tmp_path: Path, key: Ed25519PrivateKey, verifier: bool) -> Path:
    root = tmp_path / "demo"
    make_repo(root, runner_pub=pub_file_text(key))
    if verifier:
        copy_toolchain(root)
        commit_all(root, "stamp the toolchain")
    git(root, "branch", "base")
    git(root, "checkout", "-q", "-b", "candidate")
    write(root, "src/app.py", "print('observed')\n")
    commit_all(root, "candidate work")
    return root


def receipt(root: Path, key: Ed25519PrivateKey) -> SignedReceipt:
    revision = git(root, "rev-parse", "HEAD")
    bound = Binding(
        repository=REPOSITORY,
        revision=revision,
        # The acceptance gate expects an order's receipts under its own run.
        run_id="demo",
        stage="verify",
        policy_hash=policy_hash(root, revision),
        order_id="demo",
    )
    return sign(key, "gate", bound, {"argv": ["just", "verify"], "exit": 0})


def commit_receipt(root: Path, signed: SignedReceipt, order: str = "demo") -> None:
    name = f"work/orders/{order}/receipts/{signed.receipt.receipt_id}.json"
    write(root, name, signed.to_json())
    commit_all(root, "receipt")


def run_ci(root: Path) -> tuple[int, str]:
    environ = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    done = subprocess.run(
        ["bash", str(SCRIPT), "base", REPOSITORY],
        cwd=root,
        env=environ,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout + done.stderr


def test_no_receipts_pass_without_building_anything(
    tmp_path: Path, runner_key: Ed25519PrivateKey
) -> None:
    root = base_repo(tmp_path, runner_key, verifier=False)
    code, output = run_ci(root)
    assert code == 0, output
    assert "no committed receipts" in output


def test_a_base_without_a_verifier_refuses_every_receipt(
    tmp_path: Path, runner_key: Ed25519PrivateKey
) -> None:
    root = base_repo(tmp_path, runner_key, verifier=False)
    commit_receipt(root, receipt(root, runner_key))
    code, output = run_ci(root)
    assert code == 1
    assert "has no receipt verifier" in output


def test_a_valid_receipt_passes_with_the_base_verifier(
    tmp_path: Path, runner_key: Ed25519PrivateKey
) -> None:
    root = base_repo(tmp_path, runner_key, verifier=True)
    commit_receipt(root, receipt(root, runner_key))
    code, output = run_ci(root)
    assert code == 0, output
    assert "gate receipt verified" in output


def test_a_candidate_key_and_verifier_change_nothing(
    tmp_path: Path, runner_key: Ed25519PrivateKey
) -> None:
    root = base_repo(tmp_path, runner_key, verifier=True)
    forger = Ed25519PrivateKey.generate()
    write(root, ".agents/config/runner.pub", pub_file_text(forger))
    lenient = root / ".agents/lib/tac/src/tac/receipts.py"
    lenient.write_text(lenient.read_text("utf-8") + LENIENT, encoding="utf-8")
    commit_all(root, "bring my own key and verifier")
    commit_receipt(root, receipt(root, forger))
    code, output = run_ci(root)
    assert code == 1, output
    assert "not the trusted runner key" in output
    assert "accepted" not in output


def test_a_receipt_moved_into_another_order_is_refused(
    tmp_path: Path, runner_key: Ed25519PrivateKey
) -> None:
    root = base_repo(tmp_path, runner_key, verifier=True)
    signed = receipt(root, runner_key)
    commit_receipt(root, signed)
    git(root, "branch", "-f", "base")
    name = f"{signed.receipt.receipt_id}.json"
    (root / "work/orders/other/receipts").mkdir(parents=True)
    git(root, "mv", f"work/orders/demo/receipts/{name}", "work/orders/other/receipts/")
    commit_all(root, "move the receipt to another order")
    code, output = run_ci(root)
    assert code == 1, output
    assert "bound to order demo, committed under other" in output


def test_a_receipt_for_a_revision_already_on_the_base_is_refused(
    tmp_path: Path, runner_key: Ed25519PrivateKey
) -> None:
    root = base_repo(tmp_path, runner_key, verifier=True)
    git(root, "branch", "-f", "base")
    signed = receipt(root, runner_key)
    write(root, "src/later.py", "print('later')\n")
    commit_all(root, "later work")
    commit_receipt(root, signed)
    code, output = run_ci(root)
    assert code == 1, output
    assert "is already on the base" in output


def test_a_receipt_followed_by_more_code_is_refused_by_the_base_verifier(
    tmp_path: Path, runner_key: Ed25519PrivateKey
) -> None:
    root = base_repo(tmp_path, runner_key, verifier=True)
    commit_receipt(root, receipt(root, runner_key))
    write(root, "src/app.py", "print('changed after the gate')\n")
    commit_all(root, "more code after the receipt")
    code, output = run_ci(root)
    assert code == 1, output
    assert "src/app.py changed since" in output


def test_a_required_receipt_cannot_be_dodged_by_committing_none(
    tmp_path: Path, runner_key: Ed25519PrivateKey
) -> None:
    root = tmp_path / "demo"
    make_repo(root, runner_pub=pub_file_text(runner_key))
    copy_toolchain(root)
    write(root, ".agents/config/receipts.toml", expectations("verify"))
    commit_all(root, "stamp the toolchain and require a verify receipt")
    git(root, "branch", "base")
    git(root, "checkout", "-q", "-b", "candidate")
    write(root, "work/orders/demo/order.toml", "v = 1\n")
    commit_all(root, "an order with no receipt")
    code, output = run_ci(root)
    assert code == 1, output
    assert "work/orders/demo: no verified verify receipt" in output
