"""The diagram inventory, the house-subset lint and the pinned render loop
(docs/DESIGN.md section 14, ADR 0004).

`docs/diagrams/` holds one Mermaid source per lifecycle `github.toml`
`[diagrams] lifecycles` names, `lifecycle-<name>.mmd`, and, while
`per_pipeline` is true, one per pipeline `[pipelines] enabled` names,
`pipeline-<name>.mmd`. Every fenced mermaid block in a Markdown file under
`docs/` is a source too.

`render` runs the mermaid-cli version `mise.toml` pins under `[tools]` on every
source, into a scratch folder, and stops at the first failure. Renders are not
byte-stable across versions and machines, so no SVG is committed: the render
writes `docs/diagrams/render.lock`, the pinned version and the sha256 of every
source it rendered, and `check` holds the sources to that lock without node.
Rendering proves syntax only; the reviewer checks the meaning.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tac.config import Config, load_config
from tac.work import Bad

DOCS_DIR = "docs"
DIAGRAMS_DIR = "docs/diagrams"
LOCK_FILE = "docs/diagrams/render.lock"
MISE_FILE = "mise.toml"
# The key of the pin in mise.toml [tools]: mise's npm backend and the package.
MISE_KEY = "npm:@mermaid-js/mermaid-cli"
PACKAGE = "@mermaid-js/mermaid-cli"
FENCE_OPEN = re.compile(r"^(?P<fence>`{3,}|~{3,})\s*mermaid\s*$")
EM_DASH = "—"

# The house subset of ISO 5807: each shape's opening bracket, its class and the
# classDef every diagram pastes verbatim (docs/diagrams/README.md).
CLASS_DEFS = {
    "term": "fill:#00A86B,stroke:#00D084,color:#000000",
    "proc": "fill:#0067A5,stroke:#0088CC,color:#FFFFFF",
    "dec": "fill:#FFBF00,stroke:#FFD500,color:#000000",
    "io": "fill:#FF8C1A,stroke:#FFA94D,color:#000000",
    "store": "fill:#9A2A2A,stroke:#F04923,color:#FFFFFF",
    "stop": "fill:#D32F2F,stroke:#F04923,color:#FFFFFF",
}
LEGEND = {
    "term": "green terminator",
    "proc": "blue process",
    "dec": "yellow decision",
    "io": "orange input or output",
    "store": "dim red data store",
    "stop": "red stop",
}
# The opening bracket of a node's shape, followed by its quoted label.
SHAPES = {
    "([": ("stadium", ("term",)),
    "[(": ("cylinder", ("store",)),
    "[/": ("parallelogram", ("io", "stop")),
    "{": ("rhombus", ("dec",)),
    "[": ("rectangle", ("proc",)),
}
# Every opening bracket Mermaid gives a flowchart node, longest first, with the
# closings that end it. `@{` is the newer `id@{ shape: ... }` syntax.
OPENERS = {
    "@{": ("}",),
    "(((": (")))",),
    "([": ("])",),
    "[(": (")]",),
    "[/": ("/]", "\\]"),
    "[\\": ("\\]", "/]"),
    "[[": ("]]",),
    "((": ("))",),
    "{{": ("}}",),
    "(": (")",),
    "[": ("]",),
    "{": ("}",),
    ">": ("]",),
}
QUOTED = re.compile(r'"[^"]*"')
NODE_ID = re.compile(r"[A-Za-z0-9_]\w*(?:-\w+)*")
EDGE = re.compile(
    r"\s*(?:<?(?:-{2,}|={2,}|-\.+-)(?:>|[ox](?!\w)|-)?|~~~)(?:\s*\|[^|]*\|)?"
)
INLINE_CLASS = re.compile(r":::(?P<cls>[\w-]+)")
CLASS_LINE = re.compile(r"^\s*class\s+(?P<ids>[\w,\s-]+?)\s+(?P<cls>[\w-]+)\s*;?\s*$")
CLASS_DEF = re.compile(r"^\s*classDef\s+(?P<cls>[\w-]+)\s+(?P<style>\S+)\s*;?\s*$")
STYLE_LINE = re.compile(r"^\s*(style|linkStyle)\b")
SKIP_LINE = re.compile(r"^\s*(%%|subgraph\b|end\b|direction\b|flowchart\b|graph\b)")


@dataclass(frozen=True, slots=True)
class Node:
    """One node as a line names it: its id, the opening bracket of its shape
    (None when the line only refers to it), whether its label is one quoted
    string, and the class a `:::` suffix gives it."""

    id: str
    opener: str | None
    quoted: bool
    cls: str | None


def read_nodes(line: str) -> tuple[list[Node], str | None]:
    """The nodes of one statement line (`a["x"] --> b & c:::cls`), and why the
    line cannot be read as nodes and edges, or None. Quoted text is blanked
    first, so a bracket inside a label is never taken for a shape."""
    text = QUOTED.sub('""', line).rstrip()
    nodes: list[Node] = []
    i = 0
    want_node = True
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        if want_node:
            found = NODE_ID.match(text, i)
            if not found:
                return nodes, f"cannot read {text[i:].strip()!r} as a node"
            node_id, i = found.group(), found.end()
            opener = next((o for o in OPENERS if text.startswith(o, i)), None)
            quoted = False
            if opener is not None:
                start = i + len(opener)
                ends = [
                    (at, close)
                    for close in OPENERS[opener]
                    if (at := text.find(close, start)) >= 0
                ]
                if not ends:
                    return nodes, f"node {node_id} opens {opener!r} and never closes"
                at, close = min(ends)
                quoted = text[start:at].strip() == '""'
                # `[/x\]` is Mermaid's trapezoid, not the parallelogram: a
                # closing other than the first names a shape of its own.
                if close != OPENERS[opener][0]:
                    opener = f"{opener}...{close}"
                i = at + len(close)
            cls = None
            if inline := INLINE_CLASS.match(text, i):
                cls, i = inline["cls"], inline.end()
            nodes.append(Node(node_id, opener, quoted, cls))
            want_node = False
        elif text[i] in "&;":
            want_node = text[i] == "&"
            i += 1
        elif edge := EDGE.match(text, i):
            i = edge.end()
            want_node = True
        else:
            return nodes, f"cannot read {text[i:].strip()!r} as an edge"
    if want_node and nodes:
        return nodes, "the line ends in an edge or '&' with no node after it"
    return nodes, None


@dataclass(frozen=True, slots=True)
class Source:
    """One diagram: a `.mmd` file, or a fenced block inside a Markdown file."""

    id: str
    path: str
    text: str
    block: int | None = None

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- inventory


def expected(config: Config) -> list[str]:
    """The file names the inventory asks for, in the order github.toml gives."""
    inventory = config.github.diagrams
    names = [f"lifecycle-{name}.mmd" for name in inventory.lifecycles]
    if inventory.per_pipeline:
        names += [f"pipeline-{name}.mmd" for name in config.knobs.pipelines.enabled]
    return names


def inventory(config: Config, root: Path) -> tuple[list[str], list[str]]:
    """The expected files that are missing, and the `.mmd` files nobody expects."""
    want = expected(config)
    have = sorted(p.name for p in (root / DIAGRAMS_DIR).glob("*.mmd"))
    missing = [name for name in want if name not in have]
    extra = [name for name in have if name not in want]
    return missing, extra


# ---------------------------------------------------------------- sources


def fenced_blocks(text: str) -> list[str]:
    """The body of every fenced mermaid block, in order."""
    blocks: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        opened = FENCE_OPEN.match(lines[i])
        i += 1
        if not opened:
            continue
        fence = opened["fence"]
        body: list[str] = []
        while i < len(lines) and not lines[i].strip().startswith(fence):
            body.append(lines[i])
            i += 1
        i += 1
        blocks.append("\n".join(body) + "\n")
    return blocks


def sources(root: Path) -> list[Source]:
    """Every `.mmd` under docs/diagrams/ and every fenced mermaid block in a
    Markdown file under docs/, each with a stable id: the path, and for a block
    `#mermaid-<n>`, counted from 1 in the file."""
    found = [
        Source(p.relative_to(root).as_posix(), p.relative_to(root).as_posix(), text)
        for p in sorted((root / DIAGRAMS_DIR).glob("*.mmd"))
        for text in [p.read_text(encoding="utf-8")]
    ]
    docs = root / DOCS_DIR
    for path in sorted(docs.rglob("*.md")) if docs.is_dir() else []:
        rel = path.relative_to(root).as_posix()
        blocks = fenced_blocks(path.read_text(encoding="utf-8"))
        for n, body in enumerate(blocks, start=1):
            found.append(Source(f"{rel}#mermaid-{n}", rel, body, n))
    return found


# ---------------------------------------------------------------- the house subset


def house_problems(source: Source) -> list[str]:
    """Where a source leaves the house subset: a line that is not nodes and
    edges, a shape outside the six, an unquoted label, a node that is only ever
    referred to, a node without a class or with the wrong one for its shape, an
    inline style, a classDef that is not the palette's, no legend comment
    naming each class used, an em dash."""
    where = source.id
    problems: list[str] = []
    shapes: dict[str, str] = {}
    drawn: set[str] = set()
    named: set[str] = set()
    classes: dict[str, str] = {}
    defs: dict[str, str] = {}
    legend = ""
    for number, line in enumerate(source.text.splitlines(), start=1):
        if EM_DASH in line:
            problems.append(f"{where}:{number}: an em dash")
        if line.strip().startswith("%% Legend:"):
            legend = line
        if not line.strip() or SKIP_LINE.match(line):
            continue
        if m := CLASS_DEF.match(line):
            defs[m["cls"]] = m["style"]
            continue
        if m := CLASS_LINE.match(line):
            for node in re.split(r"[,\s]+", m["ids"].strip()):
                classes[node] = m["cls"]
            continue
        if STYLE_LINE.match(line):
            problems.append(f"{where}:{number}: an inline style; use a class")
            continue
        nodes, unread = read_nodes(line)
        if unread:
            problems.append(f"{where}:{number}: {unread}")
        for node in nodes:
            named.add(node.id)
            if node.cls is not None:
                classes[node.id] = node.cls
            if node.opener is None:
                continue
            drawn.add(node.id)
            if node.opener not in SHAPES:
                problems.append(
                    f"{where}:{number}: node {node.id} uses '{node.opener}', "
                    "a shape outside the house subset"
                )
                continue
            if not node.quoted:
                problems.append(
                    f"{where}:{number}: node {node.id} has an unquoted label"
                )
            shapes.setdefault(node.id, node.opener)
    for cls, style in sorted(defs.items()):
        if CLASS_DEFS.get(cls) != style:
            problems.append(f"{where}: classDef {cls} is not the house palette's")
    for node in sorted(named - drawn):
        problems.append(
            f"{where}: node {node} is never drawn with a shape and a quoted label"
        )
    for node in sorted(named):
        cls = classes.get(node)
        if cls is None:
            problems.append(f"{where}: node {node} has no class")
            continue
        if node not in shapes:
            continue
        shape, allowed = SHAPES[shapes[node]]
        if cls not in allowed:
            problems.append(f"{where}: node {node} is a {shape} with class {cls}")
        elif cls not in defs:
            problems.append(f"{where}: class {cls} is used but has no classDef")
    for node in sorted(set(classes) - named):
        problems.append(f"{where}: class names {node}, which no line draws")
    if not legend:
        problems.append(f"{where}: no %% Legend: comment line")
    else:
        for cls in sorted(set(classes.values()) & set(LEGEND)):
            if LEGEND[cls] not in legend:
                problems.append(f"{where}: the legend does not say {LEGEND[cls]!r}")
    return problems


# ---------------------------------------------------------------- the pin and the lock


def pinned_version(root: Path) -> str:
    """The mermaid-cli version mise.toml pins under [tools]."""
    path = root / MISE_FILE
    try:
        tools = tomllib.loads(path.read_text(encoding="utf-8")).get("tools", {})
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise Bad(f"{MISE_FILE}: cannot read the mermaid-cli pin: {e}") from None
    version = tools.get(MISE_KEY) if isinstance(tools, dict) else None
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise Bad(f'{MISE_FILE}: [tools] "{MISE_KEY}" must pin an exact X.Y.Z version')
    return version


def lock_text(version: str, found: list[Source]) -> str:
    data = {
        "mermaid_cli": version,
        "sources": {s.id: s.digest for s in sorted(found, key=lambda s: s.id)},
    }
    return json.dumps(data, indent=2) + "\n"


def read_lock(root: Path) -> dict[str, object] | None:
    path = root / LOCK_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Bad(f"{LOCK_FILE}: not valid JSON: {e}") from None
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("mermaid_cli"), str)
        or not isinstance(data.get("sources"), dict)
    ):
        raise Bad(f"{LOCK_FILE}: needs mermaid_cli and a sources map")
    return data


# ---------------------------------------------------------------- check


def check(root: Path, config: Config | None = None) -> list[str]:
    """Every way the diagrams differ from the inventory, the house subset and the
    lock of the last render; empty is clean. Needs no node."""
    config = config or load_config(root)
    problems: list[str] = []
    missing, extra = inventory(config, root)
    problems += [
        f"{DIAGRAMS_DIR}/{name}: missing; github.toml [diagrams] expects it"
        for name in missing
    ]
    problems += [
        f"{DIAGRAMS_DIR}/{name}: not in the inventory of github.toml [diagrams]"
        for name in extra
    ]
    found = sources(root)
    for source in found:
        if source.block is None:
            problems += house_problems(source)
    try:
        version = pinned_version(root)
        lock = read_lock(root)
    except Bad as e:
        return [*problems, str(e)]
    if lock is None:
        return [*problems, f"{LOCK_FILE}: missing; run just diagrams-render"]
    if lock["mermaid_cli"] != version:
        problems.append(
            f"{LOCK_FILE}: rendered with mermaid-cli {lock['mermaid_cli']}, "
            f"{MISE_FILE} pins {version}; run just diagrams-render"
        )
    recorded = lock["sources"]
    assert isinstance(recorded, dict)
    for source in found:
        if source.id not in recorded:
            problems.append(
                f"{source.id}: never rendered since it was added; "
                "run just diagrams-render"
            )
        elif recorded[source.id] != source.digest:
            problems.append(
                f"{source.id}: changed since the last render; run just diagrams-render"
            )
    ids = {s.id for s in found}
    problems += [
        f"{LOCK_FILE}: {rel} is gone since the last render; run just diagrams-render"
        for rel in sorted(set(recorded) - ids)
    ]
    return problems


# ---------------------------------------------------------------- render


def _safe(source_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", source_id)


def render(
    root: Path,
    out_dir: Path,
    *,
    echo: Callable[[str], None] = print,
) -> list[Path]:
    """Render every source with the pinned mermaid-cli through npx into
    `out_dir`, one SVG each, stopping at the first failure; then write the lock.
    Returns the SVGs written."""
    version = pinned_version(root)
    npx = shutil.which("npx")
    if npx is None:
        raise Bad("npx not found; mermaid-cli needs node 22.13 or later on PATH")
    out_dir.mkdir(parents=True, exist_ok=True)
    found = sources(root)
    if not found:
        raise Bad(f"no diagram under {DIAGRAMS_DIR} or in docs/; nothing to render")
    written: list[Path] = []
    for source in found:
        name = _safe(source.id)
        if source.block is None:
            src = root / source.path
        else:
            src = out_dir / f"{name}.mmd"
            src.write_text(source.text, encoding="utf-8")
        svg = out_dir / f"{name}.svg"
        done = subprocess.run(
            [npx, "--yes", f"{PACKAGE}@{version}", "-i", str(src), "-o", str(svg)],
            capture_output=True,
            text=True,
            check=False,
            cwd=root,
        )
        if done.returncode != 0 or not svg.is_file() or svg.stat().st_size == 0:
            tail = (done.stderr or done.stdout).strip()[-800:]
            raise Bad(
                f"{source.id}: mermaid-cli {version} failed "
                f"(exit {done.returncode}): {tail}"
            )
        echo(f"rendered {source.id} -> {svg}")
        written.append(svg)
    (root / LOCK_FILE).write_text(lock_text(version, found), encoding="utf-8")
    echo(f"wrote {LOCK_FILE}: mermaid-cli {version}, {len(found)} sources")
    return written
