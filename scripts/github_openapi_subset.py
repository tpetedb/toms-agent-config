"""Cut the ruleset schemas out of GitHub's published OpenAPI description.

`tac github apply` is never run live, so its request body and the recorded
responses are checked against the description GitHub publishes instead. This
writes the few schemas those tests need, dereferenced, with OpenAPI's
`nullable` turned into JSON Schema, into tests/fixtures/github/. The repository
and collaborator fixtures started from the published examples of the same
description (components/examples).

Usage, on a machine with network access:

    curl -sSLo /tmp/api.github.com.json https://raw.githubusercontent.com/\
github/rest-api-description/<commit>/descriptions/api.github.com/api.github.com.json
    uv run --frozen python scripts/github_openapi_subset.py \
        /tmp/api.github.com.json <commit>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DESCRIPTION = "Cut the ruleset schemas out of GitHub's OpenAPI description."
OUT = Path("tests/fixtures/github/openapi_rulesets.json")
SOURCE = "github/rest-api-description, descriptions/api.github.com"
# Documentation fields: they change often and a validator ignores them.
DROPPED = frozenset({"description", "example", "examples", "title", "x-github"})
RULESETS = "/repos/{owner}/{repo}/rulesets"
RULESET = "/repos/{owner}/{repo}/rulesets/{ruleset_id}"
PERMISSION = "/repos/{owner}/{repo}/collaborators/{username}/permission"
REPOSITORY = "/repos/{owner}/{repo}"


def resolve(doc: dict[str, Any], node: Any, trail: tuple[str, ...] = ()) -> Any:
    """Inline every $ref; a reference back into its own trail stays open."""
    if isinstance(node, list):
        return [resolve(doc, item, trail) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        ref = node["$ref"]
        if ref in trail:
            return {}
        target: Any = doc
        for part in ref.removeprefix("#/").split("/"):
            target = target[part]
        return resolve(doc, target, (*trail, ref))
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            # Property names are data here, even one called description.
            out[key] = {name: resolve(doc, v, trail) for name, v in value.items()}
        elif key not in DROPPED:
            out[key] = resolve(doc, value, trail)
    if out.pop("nullable", False):
        kind = out.get("type")
        if isinstance(kind, str):
            out["type"] = [kind, "null"]
        elif "type" not in out:
            out = {"anyOf": [out, {"type": "null"}]}
    return out


def body(doc: dict[str, Any], path: str, method: str) -> Any:
    schema = doc["paths"][path][method]["requestBody"]["content"]["application/json"]
    return resolve(doc, schema["schema"])


def reply(doc: dict[str, Any], path: str, status: str = "200") -> Any:
    content = doc["paths"][path]["get"]["responses"][status]["content"]
    return resolve(doc, content["application/json"]["schema"])


def subset(doc: dict[str, Any], commit: str) -> dict[str, Any]:
    return {
        "_source": SOURCE,
        "_commit": commit,
        "_licence": "MIT, Copyright (c) GitHub",
        "_api_version": doc["info"]["version"],
        "schemas": {
            "ruleset_create": body(doc, RULESETS, "post"),
            "ruleset_update": body(doc, RULESET, "put"),
            "ruleset": reply(doc, RULESET),
            "ruleset_list": reply(doc, RULESETS),
            "collaborator_permission": reply(doc, PERMISSION),
            "repository": reply(doc, REPOSITORY),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("description", type=Path, help="api.github.com.json")
    parser.add_argument("commit", help="the rest-api-description commit it came from")
    args = parser.parse_args()
    doc = json.loads(args.description.read_text(encoding="utf-8"))
    OUT.write_text(
        json.dumps(subset(doc, args.commit), indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
