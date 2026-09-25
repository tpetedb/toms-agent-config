"""The hook guard: what every rendered Claude and Codex hook runs (DESIGN 9, 10).

Stdlib only and Python 3.9 compatible, since a hook starts from the system
interpreter and must not depend on the venv existing. The rendered command is

    /usr/bin/python3 -I <root>/.agents/hooks/run.py --client claude --event Stop

with a fixed environment. Claude runs hooks outside its Bash sandbox, so what
this file loads is pinned here rather than by a sandbox (build condition C2):
it refuses to run unless Python is isolated, no module it loaded came from the
checkout or the working folder, and every file beside it matches the hash
generated.lock records. It then runs the full checker, `tac hook run`, from
the absolute path of the non-editable `.agents/.venv` interpreter, isolated,
with an environment built from an allowlist, and refuses a checker that
imported `tac` from anywhere but that venv.

Deny-class events (PreToolUse) fail closed: a tampered guard, a missing venv, a
checker that crashes, times out or answers garbage all refuse the tool call
with exit 2. Every other event cannot stop what already happened, so the same
failures there are reported and exit 0.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

DESCRIPTION = "The hook guard every rendered Claude and Codex hook runs."
LOCK = os.path.join(".agents", "generated.lock")
LOCK_VERSION = 1
VENV = os.path.join(".agents", ".venv")
VENV_PYTHON = os.path.join(VENV, "bin", "python")
# The events each client fires that the guard answers, as its docs name them:
# https://code.claude.com/docs/en/hooks and https://developers.openai.com/codex/hooks
EVENTS = {
    "claude": ("PreToolUse", "PostToolUse", "Stop", "SubagentStop", "SessionStart"),
    "codex": ("PreToolUse", "PostToolUse", "Stop", "SessionStart"),
}
# Only a PreToolUse refusal stops anything; every other event is reminder-class.
DENY_CLASS = frozenset({"PreToolUse"})
STOPS = frozenset({"Stop", "SubagentStop"})
DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# What the checker inherits besides PATH. Never PYTHON*, UV_*, VIRTUAL_ENV,
# GIT_* or a token: each of those can change what the checker loads or reaches.
PASS_ENV = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")
VERDICTS = frozenset({"allow", "deny", "remind"})
MAX_INPUT = 8 * 1024 * 1024
SECTION = re.compile(r"^\[([A-Za-z0-9_-]+)\]$")
ENTRY = re.compile(r'^(?:"((?:[^"\\]|\\.)*)"|([A-Za-z0-9_-]+)) = (.+)$')
DIGEST = re.compile(r'^"([0-9a-f]{64})"$')


class Refusal(Exception):
    """The guard cannot vouch for this call; the message says why."""


def _inside(path: str, folder: str) -> bool:
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


# ---------------------------------------------------------------- self-check


def locate(script: str) -> tuple[str, str, str]:
    """(root, the guard's folder relative to root, its own path relative to
    root). The stamped copy lives in <root>/.agents/hooks/, the source in
    <root>/hooks/; anywhere else is refused."""
    if not os.path.isabs(script):
        raise Refusal(f"started by the relative path {script}; hooks name it whole")
    if os.path.islink(script):
        raise Refusal(f"{script} is a link; the guard runs only from its own file")
    here = os.path.realpath(script)
    folder = os.path.dirname(here)
    parent = os.path.dirname(folder)
    if os.path.basename(folder) != "hooks":
        raise Refusal(f"{here} is not in a hooks folder")
    if os.path.basename(parent) == ".agents":
        root, prefix = os.path.dirname(parent), ".agents/hooks"
    else:
        root, prefix = parent, "hooks"
    return root, prefix, prefix + "/" + os.path.basename(here)


def check_interpreter(root: str, script: str) -> None:
    """Python is isolated and nothing it imported came from a writable place,
    the guard's own file aside, whose bytes the lock check vouches for."""
    if not sys.flags.isolated:
        raise Refusal("Python was not started with -I; the hook command must be")
    if sys.prefix != sys.base_prefix:
        raise Refusal(f"running inside the virtual environment {sys.prefix}")
    stdlib = os.path.dirname(os.path.realpath(os.__file__))
    if not _inside(stdlib, sys.base_prefix):
        raise Refusal(f"os was imported from {stdlib}, outside {sys.base_prefix}")
    if _inside(stdlib, root):
        raise Refusal(f"the interpreter lives inside the checkout, at {stdlib}")
    # A working folder that holds the interpreter itself, such as /, is not a
    # place anyone plants a module the standard library would shadow.
    cwd = os.getcwd()
    near = [root] if _inside(stdlib, cwd) else [root, cwd]
    for entry in sys.path:
        if not entry or any(_inside(entry, place) for place in near):
            raise Refusal(f"sys.path holds {entry or 'the working folder'}")
    for name in ("argparse", "hashlib", "json", "re", "subprocess"):
        where = getattr(sys.modules[name], "__file__", None) or ""
        if not _inside(where, stdlib):
            raise Refusal(f"{name} was imported from {where}, not the standard library")
    own = os.path.realpath(script)
    for name, module in list(sys.modules.items()):
        where = getattr(module, "__file__", None)
        if not where or os.path.realpath(where) == own:
            continue
        if any(_inside(where, place) for place in near):
            raise Refusal(f"{name} was imported from {where}")


def read_lock(root: str) -> dict[str, dict[str, str]]:
    """The lock's hash tables. Only the shape `tac sync` writes is read, and
    anything else in the file is taken as tampering."""
    path = os.path.join(root, LOCK)
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as e:
        raise Refusal(f"{LOCK} cannot be read ({e.strerror}); run tac sync") from None
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    version = None
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        header = SECTION.match(line)
        if header:
            if header.group(1) in sections:
                raise Refusal(f"{LOCK}:{number}: [{header.group(1)}] twice")
            current = sections[header.group(1)] = {}
            continue
        entry = ENTRY.match(line)
        if entry is None:
            raise Refusal(f"{LOCK}:{number}: not a line tac sync writes")
        quoted, bare, value = entry.groups()
        name = json.loads('"' + quoted + '"') if quoted is not None else bare
        if current is None:
            if name != "schema_version" or version is not None:
                raise Refusal(f"{LOCK}:{number}: only schema_version sits on top")
            version = value
            continue
        digest = DIGEST.match(value)
        if digest is None or name in current:
            raise Refusal(f"{LOCK}:{number}: not a single sha256 for {name}")
        current[name] = digest.group(1)
    if version != str(LOCK_VERSION):
        raise Refusal(f"{LOCK}: schema_version {version}, the guard reads 1")
    return sections


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def check_own_files(root: str, prefix: str, own: str) -> None:
    """Every file in the guard's folder is one the lock records, with its hash,
    and none is a link; a file planted beside the guard is refused too."""
    recorded = read_lock(root).get("hooks")
    if not recorded:
        raise Refusal(f"{LOCK} records no hooks; run tac sync")
    expected = {k: v for k, v in recorded.items() if k.startswith(prefix + "/")}
    if own not in expected:
        raise Refusal(f"{LOCK} does not record {own}; run tac sync")
    found: dict[str, str] = {}
    top = os.path.join(root, *prefix.split("/"))
    for folder, dirs, files in os.walk(top):
        for name in dirs + files:
            if os.path.islink(os.path.join(folder, name)):
                rel = os.path.relpath(os.path.join(folder, name), root)
                raise Refusal(f"{rel} is a link")
        # Bytecode caches are never imported from here: -I keeps this folder
        # off sys.path, and the check above refuses a module loaded from it.
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            path = os.path.join(folder, name)
            found[os.path.relpath(path, root).replace(os.sep, "/")] = _sha256(path)
    for rel in sorted(set(found) | set(expected)):
        if rel not in expected:
            raise Refusal(f"{rel} is not in {LOCK}")
        if rel not in found:
            raise Refusal(f"{rel} is in {LOCK} but missing")
        if found[rel] != expected[rel]:
            raise Refusal(f"{rel} does not match {LOCK}")


# ---------------------------------------------------------------- the checker


class NoVenv(Refusal):
    """The checker's environment is not built yet."""


def checker_env(path: str) -> dict[str, str]:
    env = {"PATH": path}
    for name in PASS_ENV:
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def run_checker(
    root: str, client: str, event: str, raw: bytes, deadline: int, path: str
) -> dict[str, str]:
    """The verdict of `tac hook run` from the deployed venv, or a Refusal."""
    python = os.path.join(root, VENV_PYTHON)
    if not os.path.exists(python):
        raise NoVenv(f"the checker's environment {VENV} is missing; run tac init")
    argv = [python, "-I", "-B", "-m", "tac", "hook", "run"]
    argv += ["--client", client, "--event", event, "--root", root]
    try:
        done = subprocess.run(
            argv,
            input=raw,
            capture_output=True,
            cwd=root,
            env=checker_env(path),
            timeout=deadline,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise Refusal(f"the checker gave no answer within {deadline}s") from None
    except OSError as e:
        raise Refusal(f"the checker did not start: {e.strerror}") from None
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", "replace").strip()[-400:]
        raise Refusal(f"the checker exited {done.returncode}: {tail}")
    try:
        verdict = json.loads(done.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Refusal("the checker answered something that is not JSON") from None
    fields = ("verdict", "check", "reason", "module")
    if (
        not isinstance(verdict, dict)
        or set(verdict) != set(fields)
        or not all(isinstance(verdict[f], str) for f in fields)
        or verdict["verdict"] not in VERDICTS
    ):
        raise Refusal("the checker answered something that is not a verdict")
    if not _inside(verdict["module"], os.path.join(root, VENV)):
        raise Refusal(f"the checker imported tac from {verdict['module']}, not {VENV}")
    return verdict


# ---------------------------------------------------------------- answering


def _emit(data: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(data) + "\n")


def _context(event: str, text: str) -> None:
    _emit({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})


def answer(
    client: str, event: str, verdict: str, reason: str, payload: dict[str, object]
) -> int:
    """Say the verdict the way the client reads it; the exit code.

    Both clients read JSON on stdout at exit 0 and 2 and show stderr at exit 0
    only in a debug log, so what the model must hear goes into JSON or rides
    on exit 2 (https://code.claude.com/docs/en/hooks#exit-code-output and
    https://developers.openai.com/codex/hooks)."""
    if verdict == "allow":
        if reason and event == "SessionStart":
            _context(event, reason)
        return 0
    message = "tac guard: " + reason
    sys.stderr.write(message + "\n")
    if event == "PreToolUse" and verdict == "deny":
        # Exit 2 refuses on both clients whatever stdout says; the JSON is the
        # structured form each documents, for a client that reads it.
        _emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "permissionDecision": "deny",
                    "permissionDecisionReason": message,
                }
            }
        )
        return 2
    if event in STOPS:
        # Sent back once: a stop that is already a continuation always ends.
        return 0 if payload.get("stop_hook_active") else 2
    if event == "PostToolUse" and client == "codex" and verdict == "deny":
        # Codex feeds the reason to the model in place of the tool result.
        return 2
    # A reminder before or after a tool, and anything at a session start,
    # cannot refuse; the reason goes into the model's context instead.
    _context(event, message)
    return 0


def fail(client: str, event: str, why: str, payload: dict[str, object]) -> int:
    """A failure of the guard or its checker: fail closed where a refusal
    counts, report it everywhere else."""
    if event in DENY_CLASS:
        return answer(client, event, "deny", why, payload)
    if event in STOPS:
        # Never sent back for the guard's own failure, or a checker that cannot
        # answer in time would hold every stop. systemMessage shows it to the
        # person on both clients, and Codex takes only JSON on a stop's stdout.
        message = "tac guard: " + why
        _emit({"systemMessage": message})
        sys.stderr.write(message + "\n")
        return 0
    return answer(client, event, "remind", why, payload)


def read_payload(event: str) -> tuple[bytes, dict[str, object]]:
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise Refusal("the event on stdin is larger than the guard reads")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Refusal("the event on stdin is not JSON") from None
    if not isinstance(payload, dict):
        raise Refusal("the event on stdin is not a JSON object")
    named = payload.get("hook_event_name")
    if named is not None and named != event:
        raise Refusal(f"the event on stdin is {named}, the hook was wired for {event}")
    return raw, payload


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="run.py", description=DESCRIPTION)
    parser.add_argument("--client", required=True, choices=sorted(EVENTS))
    parser.add_argument("--event", required=True)
    parser.add_argument("--deadline-s", type=int, default=8)
    parser.add_argument("--path", default=DEFAULT_PATH)
    args = parser.parse_args(argv)
    if args.event not in EVENTS[args.client]:
        parser.error(f"{args.client} fires no {args.event} the guard answers")
    if not 1 <= args.deadline_s <= 600:
        parser.error("--deadline-s is between 1 and 600")
    if not all(os.path.isabs(p) for p in args.path.split(":")):
        parser.error("--path holds absolute folders only")
    return args


def main(argv: list[str]) -> int:
    args = parse(argv)
    payload: dict[str, object] = {}
    try:
        root, prefix, own = locate(sys.argv[0])
        check_interpreter(root, sys.argv[0])
        check_own_files(root, prefix, own)
        raw, payload = read_payload(args.event)
        verdict = run_checker(
            root, args.client, args.event, raw, args.deadline_s, args.path
        )
    except Refusal as e:
        return fail(args.client, args.event, str(e), payload)
    except Exception as e:  # an unforeseen failure is still a failure
        return fail(args.client, args.event, f"{type(e).__name__}: {e}", payload)
    reason = verdict["reason"]
    if verdict["verdict"] != "allow" and verdict["check"]:
        reason = f"[{verdict['check']}] {reason}"
    return answer(args.client, args.event, verdict["verdict"], reason, payload)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
