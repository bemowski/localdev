"""Validate request examples against openapi.yaml.

  examples/*.json          must be valid EventBatch bodies
  examples/invalid/*.json  must be rejected
  inline examples in the spec (request bodies and responses) must match their schemas
  frozen versions (api/, see api_versions.py) must match openapi.yaml at the same version

Run: mise run api:check
"""
import json
import sys
from pathlib import Path

import yaml
from api_versions import problems as version_problems
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parent.parent
SPEC_URI = "urn:usage-api"

spec = yaml.safe_load((ROOT / "openapi.yaml").read_text())
registry = Registry().with_resource(SPEC_URI, DRAFT202012.create_resource(spec))


def validator(ref):
    """ref: a JSON pointer within the spec, or a local '#/...' ref."""
    return Draft202012Validator({"$ref": SPEC_URI + ref.removeprefix(SPEC_URI)}, registry=registry,
                                format_checker=Draft202012Validator.FORMAT_CHECKER)


def leaves(e):
    """The specific errors behind a oneOf failure: only from the branch whose `type` constant
    matched (the discriminator's choice). No branch matched: the type is unknown."""
    if not e.context:
        return [e]
    branches = {}
    for sub in e.context:
        branches.setdefault(sub.relative_schema_path[0], []).extend(leaves(sub))
    if e.validator != "oneOf":
        return [leaf for found in branches.values() for leaf in found]
    chosen = [found for found in branches.values()
              if not any(f.validator == "const" and list(f.absolute_path)[-1:] == ["type"] for f in found)]
    if not chosen:
        e.message = f"unknown event type {e.instance.get('type')!r}"
        return [e]
    return [leaf for found in chosen for leaf in found]


def errors(v, doc):
    return [f"{'.'.join(map(str, leaf.absolute_path)) or '<root>'}: {leaf.message[:100]}"
            for e in v.iter_errors(doc) for leaf in leaves(e)]


batch = validator("#/components/schemas/EventBatch")
failures = 0

for f in sorted((ROOT / "examples").glob("*.json")):
    errs = errors(batch, json.loads(f.read_text()))
    status = "ok" if not errs else "FAIL"
    failures += bool(errs)
    print(f"{status:4}  valid    {f.relative_to(ROOT)}")
    for e in errs:
        print(f"        {e}")

for f in sorted((ROOT / "examples" / "invalid").glob("*.json")):
    errs = errors(batch, json.loads(f.read_text()))
    status = "ok" if errs else "FAIL"
    failures += not errs
    print(f"{status:4}  invalid  {f.relative_to(ROOT)}" + (f"  ({errs[0][:90]})" if errs else "  (was accepted)"))

oversized = {"events": json.loads((ROOT / "examples" / "cluster-destroyed.json").read_text())["events"] * 501}
ok = bool(errors(batch, oversized))
failures += not ok
print(f"{'ok' if ok else 'FAIL':4}  invalid  <501 events>")

# inline examples in the spec
for path, ops in spec["paths"].items():
    for method, op in ops.items():
        media = [("request", c) for c in op.get("requestBody", {}).get("content", {}).values()]
        media += [(f"response {code}", c) for code, r in op.get("responses", {}).items()
                  for c in r.get("content", {}).values()]
        for where, content in media:
            schema = content.get("schema", {})
            if "$ref" not in schema:
                continue
            v = validator(schema["$ref"])
            samples = [("example", content["example"])] if "example" in content else []
            samples += [(name, ex["value"]) for name, ex in content.get("examples", {}).items()]
            for name, value in samples:
                errs = errors(v, value)
                failures += bool(errs)
                print(f"{'ok' if not errs else 'FAIL':4}  inline   {method.upper()} {path} {where} {name}")
                for e in errs:
                    print(f"        {e}")

# frozen API versions
for problem in version_problems() or [None]:
    failures += problem is not None
    print(f"{'ok' if problem is None else 'FAIL':4}  api/     {problem or 'frozen versions match openapi.yaml'}")

print(f"\n{failures} failure(s)" if failures else "\nall checks passed")
sys.exit(1 if failures else 0)
