"""Request validation straight from openapi.yaml: the spec is the only definition of an event, so
the server can't drift from it. Adding an event type to the spec makes the server accept it (stored
in `events`); a handler in store.py is only needed to project it into typed tables."""
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

SPEC_URI = "urn:usage-api"
FORMATS = Draft202012Validator.FORMAT_CHECKER
# without rfc3339-validator installed, jsonschema silently skips date-time checks
assert {"date-time", "uuid"} <= set(FORMATS.checkers), "jsonschema format support missing (rfc3339-validator)"


class BatchValidator:
    def __init__(self, spec_text: str):
        self.spec_text = spec_text
        self.spec = yaml.safe_load(spec_text)
        registry = Registry().with_resource(SPEC_URI, DRAFT202012.create_resource(self.spec))
        self._batch = Draft202012Validator({"$ref": f"{SPEC_URI}#/components/schemas/EventBatch"},
                                           registry=registry, format_checker=FORMATS)

    @classmethod
    def from_file(cls, path: Path) -> "BatchValidator":
        return cls(Path(path).read_text())

    def errors(self, body) -> list[dict]:
        """ErrorDetail dicts ({index?, path, message}) for a request body; [] when valid."""
        details = []
        for err in self._batch.iter_errors(body):
            for leaf in _leaves(err):
                path = list(leaf.absolute_path)
                detail = {}
                if path[:1] == ["events"] and len(path) > 1:
                    detail["index"] = path[1]
                    path = path[2:]
                detail["path"] = ".".join(map(str, path))
                detail["message"] = leaf.message
                if detail not in details:
                    details.append(detail)
        return sorted(details, key=lambda d: (d.get("index", -1), d["path"]))


def _leaves(err):
    """The specific errors behind a oneOf failure: only those from the branch whose `type`
    constant matched (the discriminator's choice). No branch matched: the type is unknown."""
    if not err.context:
        return [err]
    branches = {}
    for sub in err.context:
        branches.setdefault(sub.relative_schema_path[0], []).extend(_leaves(sub))
    if err.validator != "oneOf":
        return [leaf for found in branches.values() for leaf in found]
    if not isinstance(err.instance, dict):
        return [err]  # "... is not valid under any of the given schemas"
    if not isinstance(err.instance.get("type"), str):
        err.message = "'type' is a required string property"
        return [err]
    chosen = [found for found in branches.values()
              if not any(f.validator == "const" and list(f.absolute_path)[-1:] == ["type"] for f in found)]
    if not chosen:
        err.message = f"unknown event type {err.instance['type']!r}"
        err.path.append("type")  # absolute_path is derived from path
        return [err]
    return [leaf for found in chosen for leaf in _real(found)]


def _real(found):
    """A failing allOf part loses its evaluated-property annotations, so unevaluatedProperties then
    also flags that part's (valid) fields as unexpected. Keep it only when nothing else failed."""
    other = [f for f in found if f.validator != "unevaluatedProperties"]
    return other or found
