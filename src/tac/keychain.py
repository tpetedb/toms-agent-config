"""The runner's two secrets in the macOS login keychain (build condition C6).

The runner holds exactly two secrets: its ed25519 signing key and the `tac-bot`
token. Both live in the login keychain, read by the runner at start outside
any sandbox, and never the owner's own token, which would let the runner
approve as the owner. `KEYCHAIN_ENTRIES` names the two, and `Keychain.read`
refuses any other name before it starts a process, so the runner can name no
third secret even by mistake.

The `security` tool is taken by absolute path, never from PATH, since a PATH
an agent shaped could put its own `security` first. A secret is written through
`security -i`, which reads its command from stdin, so the secret never sits on
a command line where `ps` shows it; the value is therefore limited to
characters that need no quoting there (hex for the key, the token's own
alphabet).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

SECURITY = "/usr/bin/security"
# `security find-generic-password` exits 44 when no such item exists.
NOT_FOUND = 44
TIMEOUT_S = 30
# What a secret may hold: it is written on `security -i`'s stdin inside one
# command line, so it must need no quoting there.
SECRET = re.compile(r"^[A-Za-z0-9_.-]{16,4096}$")
SLUG = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class KeychainError(Exception):
    """The keychain refuses, or an entry is missing: the message says why."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One keychain item: its service (namespaced per store), account and use."""

    service: str
    account: str
    purpose: str


SIGNING_KEY = Entry(
    service="tac-runner-signing-key",
    account="tac-runner",
    purpose="the runner's ed25519 signing key, raw private bytes as hex",
)
BOT_TOKEN = Entry(
    service="tac-bot-token",
    account="tac-bot",
    purpose="the tac-bot GitHub token the runner's own git and gh use",
)
# Exactly these two, and nothing else: the owner's token is never here.
KEYCHAIN_ENTRIES: tuple[Entry, Entry] = (SIGNING_KEY, BOT_TOKEN)


@dataclass(frozen=True, slots=True)
class Keychain:
    """The login keychain through `security`, for one controller store."""

    slug: str
    security: str = SECURITY

    def __post_init__(self) -> None:
        if not os.path.isabs(self.security):
            raise KeychainError(
                f"security must be named by absolute path, not {self.security!r}"
            )
        if not SLUG.match(self.slug):
            raise KeychainError(f"not a store slug: {self.slug!r}")

    def service(self, entry: Entry) -> str:
        """The item's service name, namespaced by the store it belongs to."""
        return f"{entry.service}.{self.slug}"

    def _known(self, entry: Entry) -> None:
        # Checked before any process starts: the runner names two items only.
        if entry not in KEYCHAIN_ENTRIES:
            raise KeychainError(
                f"{entry.service} is not one of the runner's two keychain entries "
                "(its signing key and the tac-bot token); it names no other"
            )

    def _run(
        self, args: list[str], stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.security, *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
                env={"PATH": "/usr/bin:/bin"},
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KeychainError(f"{self.security} did not run: {exc}") from exc

    def exists(self, entry: Entry) -> bool:
        self._known(entry)
        done = self._run(
            [
                "find-generic-password",
                *("-s", self.service(entry), "-a", entry.account),
            ]
        )
        if done.returncode == NOT_FOUND:
            return False
        if done.returncode != 0:
            raise KeychainError(
                f"security could not look up {self.service(entry)}: "
                f"{done.stderr.strip()}"
            )
        return True

    def read(self, entry: Entry) -> str:
        self._known(entry)
        done = self._run(
            [
                "find-generic-password",
                *("-s", self.service(entry), "-a", entry.account, "-w"),
            ]
        )
        if done.returncode == NOT_FOUND:
            raise KeychainError(f"no keychain item {self.service(entry)}")
        if done.returncode != 0:
            raise KeychainError(
                f"security could not read {self.service(entry)}: {done.stderr.strip()}"
            )
        return done.stdout.strip()

    def write(self, entry: Entry, secret: str) -> None:
        self._known(entry)
        if not SECRET.match(secret):
            raise KeychainError(
                f"the value for {self.service(entry)} holds characters the "
                "keychain import does not pass; expected letters, digits, "
                "`_`, `.` or `-`"
            )
        # -U updates an existing item; the command reaches security on stdin.
        command = (
            f"add-generic-password -U -s {self.service(entry)} "
            f"-a {entry.account} -w {secret}\n"
        )
        done = self._run(["-i"], stdin=command)
        if done.returncode != 0:
            raise KeychainError(
                f"security could not write {self.service(entry)}: {done.stderr.strip()}"
            )


# Set by pytest for every test: a test run never reaches the owner's real login
# keychain, whatever a test forgets to fake.
UNDER_TEST = "PYTEST_CURRENT_TEST"


def system_keychain(slug: str) -> Keychain | None:
    """The login keychain on macOS, or None where there is none to use, and
    always None under a test run."""
    if os.environ.get(UNDER_TEST) or not os.access(SECURITY, os.X_OK):
        return None
    return Keychain(slug)
