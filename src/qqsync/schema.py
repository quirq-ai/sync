"""The manifest schemas and validation against them.

Validation has two layers. The JSON Schema (schema/quirq-repo-1.schema.json) fixes the shape: which
keys exist, their types and the digest format. The checks below cover what a JSON Schema cannot:
target names are unique, every target dep names a target, and the target graph has no cycle.
Kinds are opaque strings; pass `known_kinds` to also require each kind to be one infra-config lists.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from functools import cache
from importlib import resources

SCHEMA_FILES = {"quirq-repo/1": "quirq-repo-1.schema.json"}
CURRENT = "quirq-repo/1"


@cache
def schema_for(version: str) -> dict:
    """The JSON Schema for one manifest schema version."""
    try:
        name = SCHEMA_FILES[version]
    except KeyError:
        known = ", ".join(sorted(SCHEMA_FILES))
        raise ValueError(f"unknown manifest schema {version!r}; this qqsync knows {known}") from None
    return json.loads(resources.files("qqsync").joinpath("schema", name).read_text())


_OCI_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"  # the OCI distribution spec's name grammar
OCI_SOURCE = re.compile(r"^oci://(?P<registry>[A-Za-z0-9.-]+(?::[0-9]+)?|\[::1\](?::[0-9]+)?)"
                        rf"/(?P<repository>{_OCI_COMPONENT}(?:/{_OCI_COMPONENT})*)"
                        r"(?:@(?P<manifest>sha256:[0-9a-f]{64}))?$")
OCI_PIN_RULE = ("an oci:// pin names the image manifest in its source (oci://REGISTRY/REPO@sha256:<manifest>) "
                "and pins the layer's bytes in digest (sha256:<layer>, a different value)")


def validate(data: dict, known_kinds: Iterable[str] | None = None) -> list[str]:
    """Every problem with a parsed manifest, as 'location: message' strings. Empty means valid."""
    from jsonschema import Draft202012Validator

    if not isinstance(data, dict) or "schema" not in data:
        return ["schema: missing; the first line of a manifest is schema = \"" + CURRENT + "\""]
    version = data["schema"]
    if not isinstance(version, str):
        return [f"schema: must be a string such as \"{CURRENT}\", not {version!r}"]
    if version not in SCHEMA_FILES:
        known = ", ".join(sorted(SCHEMA_FILES))
        return [f"schema: {version!r} is not a schema this qqsync knows ({known}); "
                "upgrade the pinned qqsync or fix the version"]

    validator = Draft202012Validator(schema_for(version))
    errors = sorted(validator.iter_errors(data), key=lambda e: [(isinstance(p, int), p) for p in e.absolute_path])
    # A value of the wrong type also fails every oneOf branch; report only the type.
    wrong_type = {tuple(e.absolute_path) for e in errors if e.validator == "type"}
    problems = [f"{_where(e.absolute_path)}: {_message(e)}" for e in errors
                if not (e.validator == "oneOf" and tuple(e.absolute_path) in wrong_type)]
    if problems:
        return problems  # the graph checks below assume the shape is right
    return _check_oci_pins(data) + _check_targets(data["targets"], None if known_kinds is None else set(known_kinds))


def _check_oci_pins(data: dict) -> list[str]:
    """oci:// pins must name the manifest and pin a layer, or the bytes fetched are never pinned."""
    problems = []
    arts = [("qq", data["qq"])] if "source" in data.get("qq", {}) else []
    for section in ("toolchains", "deps"):
        for name, pin in data.get(section, {}).items():
            arts += ([(f"{section}.{name}.platforms.{plat}", art) for plat, art in pin["platforms"].items()]
                     if "platforms" in pin else [(f"{section}.{name}", pin)])
    for where, art in arts:
        if not art["source"].lower().startswith("oci:"):  # any spelling of the scheme
            continue
        m = OCI_SOURCE.match(art["source"])
        if not m:
            problems.append(f"{where}.source: {art['source']!r} is not "
                            "oci://REGISTRY/REPO@sha256:<manifest> (lower-case OCI repository names)")
        elif not m["manifest"]:
            problems.append(f"{where}.source: {art['source']!r} has no @sha256:<manifest>; {OCI_PIN_RULE}")
        elif not art["digest"].startswith("sha256:") or art["digest"] == m["manifest"]:
            problems.append(f"{where}.digest: {art['digest']} must be the layer's sha256, not "
                            f"{'the manifest digest' if art['digest'] == m['manifest'] else 'a commit'}; "
                            f"{OCI_PIN_RULE}")
    return problems


def _message(error) -> str:
    # "is not valid under any of the given schemas" names no fix; the schema's description does.
    if error.validator == "oneOf" and "description" in error.schema:
        return "expected " + error.schema["description"][0].lower() + error.schema["description"][1:]
    if error.validator == "not" and error.validator_value in ({"pattern": "\\s"}, {"pattern": "[\\r\\n]"}):
        return f"{error.instance!r} must not contain whitespace or line breaks"
    if error.validator == "pattern" and "description" in error.schema:
        return f"{error.instance!r} is not valid: {error.schema['description']}"
    return error.message


def _where(path: Iterable) -> str:
    out = ""
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "(root)"


def _check_targets(targets: list[dict], known_kinds: set[str] | None) -> list[str]:
    problems = []
    index: dict[str, int] = {}
    for i, t in enumerate(targets):
        if t["name"] in index:
            problems.append(f"targets[{i}].name: {t['name']!r} is already used by targets[{index[t['name']]}]")
        else:
            index[t["name"]] = i
        if known_kinds is not None and t["kind"] not in known_kinds:
            problems.append(f"targets[{i}].kind: {t['kind']!r} is not a kind infra-config lists")
    for i, t in enumerate(targets):
        for dep in t.get("deps", []):
            if dep == t["name"]:
                problems.append(f"targets[{i}].deps: {dep!r} depends on itself")
            elif dep not in index:
                problems.append(f"targets[{i}].deps: {dep!r} is not a target in this manifest")
    if not problems:
        cycle = _find_cycle({t["name"]: t.get("deps", []) for t in targets})
        if cycle:
            problems.append("targets: dependency cycle " + " -> ".join(cycle))
    return problems


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    done: set[str] = set()
    for root in graph:
        if root in done:
            continue
        path: list[str] = []
        on_path: set[str] = set()
        stack = [(root, iter(graph[root]))]
        path.append(root)
        on_path.add(root)
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                stack.pop()
                path.pop()
                on_path.discard(node)
                done.add(node)
            elif child in on_path:
                return path[path.index(child):] + [child]
            elif child not in done:
                stack.append((child, iter(graph[child])))
                path.append(child)
                on_path.add(child)
    return None
