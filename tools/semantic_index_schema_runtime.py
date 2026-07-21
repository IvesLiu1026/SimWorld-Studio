"""Bounded, pure-stdlib schema gate for pinned semantic-index contracts.

This is deliberately *not* a general JSON Schema implementation.  It accepts
only the closed Draft 2020-12 keyword subset used by the pinned semantic-index
schemas in this repository.  Unsupported keywords, Boolean schemas, unresolved
references, recursive references, and inputs outside fixed resource limits
fail closed.

Callers remain responsible for hashing and pinning every schema document before
parsing it.  ``registry`` is an in-memory map of already pinned schema ``$id``
values to parsed schema documents.  The gate never performs network or file
resolution, even when an exact registry identifier is an HTTPS URI.
"""

from __future__ import annotations

import dataclasses
import re
import unicodedata
from collections.abc import Mapping
from typing import Any


DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

MAX_SCHEMA_RESOURCES = 32
MAX_SCHEMA_DEPTH = 64
MAX_SCHEMA_NODES = 100_000
MAX_SCHEMA_STRING_BYTES = 16_384
MAX_PATTERN_BYTES = 2_048
MAX_PATTERN_INPUT_CHARS = 4_096
MAX_PATTERN_REPEAT = 1_000_000
MAX_DEFINITIONS = 4_096
MAX_SCHEMA_BRANCHES = 256

MAX_INSTANCE_DEPTH = 64
MAX_INSTANCE_NODES = 300_000
MAX_INSTANCE_CONTAINER_ITEMS = 131_072
MAX_INSTANCE_STRING_BYTES = 1_048_576
MAX_INTEGER_BITS = 128
MAX_EVALUATION_STEPS = 2_000_000

_DEFINITION_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_TYPE_NAMES = frozenset({"object", "array", "string", "integer", "boolean", "null"})
_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$defs",
        "$ref",
        "title",
        "type",
        "const",
        "enum",
        "required",
        "properties",
        "additionalProperties",
        "allOf",
        "anyOf",
        "oneOf",
        "if",
        "then",
        "else",
        "not",
        "pattern",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "prefixItems",
        "items",
        "contains",
        "minContains",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
)
_SCHEMA_CHILD_KEYS = frozenset({"if", "then", "else", "not", "items", "contains"})
_SCHEMA_LIST_KEYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})

# The closed pattern subset intentionally admits only the escapes used by the
# pinned corpus plus common zero-width/character-class escapes.  In particular,
# numeric/named backreferences and Python's extension groups remain outside the
# contract because they complicate both complexity analysis and portability.
_SAFE_PATTERN_ESCAPES = frozenset("AbBdDsSwWZ.^$*+?{}[]\\|()-/")
_BRACED_QUANTIFIER_RE = re.compile(r"\{([0-9]+)(?:,([0-9]*))?\}")
_AUDITED_NESTED_REPEAT_PATTERNS = frozenset(
    {
        r"^/Game(?:/[A-Za-z0-9_+\-]+)+\.[A-Za-z0-9_+\-]{1,240}$",
    }
)
_AUDITED_MULTI_REPEAT_PATTERNS = frozenset(
    {
        r"^/(?!.*//)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._+\-/]+$",
        r"^/Game(?:/[A-Za-z0-9_+\-]+)+\.[A-Za-z0-9_+\-]{1,240}$",
        r"^[0-9]{4}-(0[1-9]|1[0-2])-([0-2][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,9})?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$",
        r"^embedding-project:[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,95}/model:[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,127}$",
        r"^postgres-deployment:[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,95}/schema:[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$",
        r"^qdrant-cluster:[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,95}/collection:[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$",
    }
)


@dataclasses.dataclass(frozen=True)
class SchemaRuntimeError(Exception):
    """A bounded validation failure that never embeds an instance value."""

    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.message}"


def _fail(code: str, path: str, message: str) -> None:
    raise SchemaRuntimeError(code=code, path=path, message=message)


def _is_int(value: Any) -> bool:
    return type(value) is int


@dataclasses.dataclass
class _PatternAtom:
    is_group: bool
    contains_repeat: bool = False
    contains_alternation: bool = False
    quantified: bool = False


@dataclasses.dataclass
class _PatternFrame:
    contains_repeat: bool = False
    contains_alternation: bool = False
    last_atom: _PatternAtom | None = None


def _compile_closed_pattern(pattern: str, path: str) -> re.Pattern[str]:
    """Compile one regex after a linear, fail-closed complexity audit.

    This is deliberately a conservative source audit, not a second regex
    parser.  It rejects extension groups, backreferences, repeated ambiguous
    groups, and nested quantifiers.  The one nested form already present in the
    pinned corpus is admitted by exact source equality after separate review.
    """

    frames = [_PatternFrame()]
    allow_audited_nested_repeat = pattern in _AUDITED_NESTED_REPEAT_PATTERNS
    variable_repeat_count = 0
    index = 0
    while index < len(pattern):
        character = pattern[index]
        frame = frames[-1]

        if character == "\\":
            if index + 1 >= len(pattern):
                _fail("SCHEMA_RUNTIME_PATTERN_INVALID", path, "Schema pattern is invalid")
            escaped = pattern[index + 1]
            if escaped.isdigit() or escaped not in _SAFE_PATTERN_ESCAPES:
                _fail(
                    "SCHEMA_RUNTIME_PATTERN_UNSUPPORTED",
                    path,
                    "Schema pattern uses an unsupported escape",
                )
            frame.last_atom = _PatternAtom(is_group=False)
            index += 2
            continue

        if character == "[":
            cursor = index + 1
            if cursor < len(pattern) and pattern[cursor] == "^":
                cursor += 1
            if cursor < len(pattern) and pattern[cursor] == "]":
                cursor += 1
            while cursor < len(pattern) and pattern[cursor] != "]":
                if pattern[cursor] == "\\":
                    if cursor + 1 >= len(pattern):
                        break
                    escaped = pattern[cursor + 1]
                    if escaped.isdigit() or escaped not in _SAFE_PATTERN_ESCAPES:
                        _fail(
                            "SCHEMA_RUNTIME_PATTERN_UNSUPPORTED",
                            path,
                            "Schema pattern uses an unsupported class escape",
                        )
                    cursor += 2
                else:
                    cursor += 1
            if cursor >= len(pattern):
                _fail("SCHEMA_RUNTIME_PATTERN_INVALID", path, "Schema pattern is invalid")
            frame.last_atom = _PatternAtom(is_group=False)
            index = cursor + 1
            continue

        if character == "(":
            if pattern.startswith("(?:", index) or pattern.startswith("(?!", index):
                index += 3
            elif index + 1 < len(pattern) and pattern[index + 1] == "?":
                _fail(
                    "SCHEMA_RUNTIME_PATTERN_UNSUPPORTED",
                    path,
                    "Schema pattern uses an unsupported extension group",
                )
            else:
                index += 1
            frames.append(_PatternFrame())
            continue

        if character == ")":
            if len(frames) == 1:
                _fail("SCHEMA_RUNTIME_PATTERN_INVALID", path, "Schema pattern is invalid")
            child = frames.pop()
            parent = frames[-1]
            parent.contains_repeat = parent.contains_repeat or child.contains_repeat
            parent.contains_alternation = (
                parent.contains_alternation or child.contains_alternation
            )
            parent.last_atom = _PatternAtom(
                is_group=True,
                contains_repeat=child.contains_repeat,
                contains_alternation=child.contains_alternation,
            )
            index += 1
            continue

        if character == "|":
            frame.contains_alternation = True
            frame.last_atom = None
            index += 1
            continue

        maximum_repeat: int | None
        quantifier_end = index + 1
        if character in "*+?":
            maximum_repeat = 1 if character == "?" else None
        elif character == "{":
            match = _BRACED_QUANTIFIER_RE.match(pattern, index)
            if match is None:
                _fail(
                    "SCHEMA_RUNTIME_PATTERN_UNSUPPORTED",
                    path,
                    "Schema pattern uses an unsupported brace expression",
                )
            minimum_repeat = int(match.group(1))
            upper_text = match.group(2)
            if minimum_repeat > MAX_PATTERN_REPEAT or (
                upper_text not in {None, ""}
                and int(upper_text) > MAX_PATTERN_REPEAT
            ):
                _fail(
                    "SCHEMA_RUNTIME_LIMIT_EXCEEDED",
                    path,
                    "Schema pattern repetition exceeds the limit",
                )
            maximum_repeat = (
                minimum_repeat
                if upper_text is None
                else (int(upper_text) if upper_text else None)
            )
            quantifier_end = match.end()
        else:
            maximum_repeat = -1

        if maximum_repeat != -1:
            atom = frame.last_atom
            if atom is None or atom.quantified:
                _fail("SCHEMA_RUNTIME_PATTERN_INVALID", path, "Schema pattern is invalid")
            is_variable_repeat = character in "*+?" or (
                character == "{" and match.group(2) is not None
            )
            if is_variable_repeat:
                variable_repeat_count += 1
            repeats_more_than_once = maximum_repeat is None or maximum_repeat > 1
            if (
                repeats_more_than_once
                and atom.is_group
                and (atom.contains_repeat or atom.contains_alternation)
                and not allow_audited_nested_repeat
            ):
                _fail(
                    "SCHEMA_RUNTIME_PATTERN_HIGH_COST",
                    path,
                    "Schema pattern contains an unaudited repeated group",
                )
            frame.contains_repeat = True
            frame.last_atom = _PatternAtom(
                is_group=False,
                contains_repeat=True,
                contains_alternation=atom.contains_alternation,
                quantified=True,
            )
            index = quantifier_end
            continue

        if character in "^$":
            frame.last_atom = None
        else:
            frame.last_atom = _PatternAtom(is_group=False)
        index += 1

    if len(frames) != 1:
        _fail("SCHEMA_RUNTIME_PATTERN_INVALID", path, "Schema pattern is invalid")
    if (
        variable_repeat_count > 1
        and pattern not in _AUDITED_MULTI_REPEAT_PATTERNS
    ):
        _fail(
            "SCHEMA_RUNTIME_PATTERN_HIGH_COST",
            path,
            "Schema pattern contains multiple unaudited variable repeats",
        )
    try:
        return re.compile(pattern)
    except (re.error, OverflowError):
        _fail("SCHEMA_RUNTIME_PATTERN_INVALID", path, "Schema pattern is invalid")


def _bounded_text(value: Any, path: str, *, maximum_bytes: int) -> str:
    if type(value) is not str:
        _fail("SCHEMA_RUNTIME_TYPE_INVALID", path, "Expected text")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        _fail("SCHEMA_RUNTIME_TEXT_INVALID", path, "Text is not valid UTF-8")
    if len(encoded) > maximum_bytes:
        _fail("SCHEMA_RUNTIME_LIMIT_EXCEEDED", path, "Text exceeds the byte limit")
    return value


class _JsonBudget:
    def __init__(self, *, schema: bool) -> None:
        self.schema = schema
        self.nodes = 0
        self.active_containers: set[int] = set()

    def check(self, value: Any, path: str = "$", depth: int = 0) -> None:
        depth_limit = MAX_SCHEMA_DEPTH if self.schema else MAX_INSTANCE_DEPTH
        node_limit = MAX_SCHEMA_NODES if self.schema else MAX_INSTANCE_NODES
        string_limit = MAX_SCHEMA_STRING_BYTES if self.schema else MAX_INSTANCE_STRING_BYTES
        if depth > depth_limit:
            _fail("SCHEMA_RUNTIME_LIMIT_EXCEEDED", path, "JSON nesting exceeds the limit")
        self.nodes += 1
        if self.nodes > node_limit:
            _fail("SCHEMA_RUNTIME_LIMIT_EXCEEDED", path, "JSON node count exceeds the limit")
        if value is None or type(value) is bool:
            return
        if _is_int(value):
            if value.bit_length() > MAX_INTEGER_BITS:
                _fail("SCHEMA_RUNTIME_INTEGER_INVALID", path, "Integer exceeds the bit limit")
            return
        if type(value) is str:
            _bounded_text(value, path, maximum_bytes=string_limit)
            if not self.schema and any(
                unicodedata.category(character) in {"Cc", "Cs"}
                for character in value
            ):
                _fail(
                    "SCHEMA_RUNTIME_TEXT_INVALID",
                    path,
                    "Instance text contains a forbidden control character",
                )
            return
        if type(value) not in {list, dict}:
            _fail("SCHEMA_RUNTIME_JSON_INVALID", path, "Value is not in the supported JSON domain")
        if len(value) > MAX_INSTANCE_CONTAINER_ITEMS:
            _fail("SCHEMA_RUNTIME_LIMIT_EXCEEDED", path, "Container exceeds the item limit")
        identity = id(value)
        if identity in self.active_containers:
            _fail("SCHEMA_RUNTIME_CYCLE", path, "Parsed JSON contains a container cycle")
        self.active_containers.add(identity)
        try:
            if type(value) is list:
                for index, item in enumerate(value):
                    self.check(item, f"{path}[{index}]", depth + 1)
            else:
                for key, item in value.items():
                    _bounded_text(key, f"{path}.<key>", maximum_bytes=string_limit)
                    self.check(item, f"{path}.{key}", depth + 1)
        finally:
            self.active_containers.remove(identity)


def _json_equal(left: Any, right: Any) -> bool:
    """JSON equality without Python's ``True == 1`` scalar aliasing."""

    if type(left) is not type(right):
        return False
    if type(left) is list:
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    if type(left) is dict:
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    return bool(left == right)


def _fingerprint(value: Any) -> Any:
    """Return a hashable, type-preserving JSON fingerprint."""

    if value is None:
        return ("null",)
    if type(value) is bool:
        return ("boolean", value)
    if _is_int(value):
        return ("integer", value)
    if type(value) is str:
        return ("string", value)
    if type(value) is list:
        return ("array", tuple(_fingerprint(item) for item in value))
    return (
        "object",
        tuple((key, _fingerprint(value[key])) for key in sorted(value)),
    )


class _ClosedSchemaRuntime:
    def __init__(
        self,
        schema: Mapping[str, Any],
        registry: Mapping[str, Mapping[str, Any]] | None,
    ) -> None:
        if type(schema) is not dict:
            _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", "$schema", "Schema must be a JSON object")
        if registry is not None and not isinstance(registry, Mapping):
            _fail("SCHEMA_RUNTIME_REGISTRY_INVALID", "$schema", "Registry must be a mapping")

        supplied = {} if registry is None else dict(registry)
        if len(supplied) + 1 > MAX_SCHEMA_RESOURCES:
            _fail("SCHEMA_RUNTIME_LIMIT_EXCEEDED", "$schema", "Registry has too many resources")

        self.resources: dict[str, dict[str, Any]] = {}
        documents = [schema]
        for identifier, document in supplied.items():
            _bounded_text(identifier, "$schema.registry.<id>", maximum_bytes=MAX_SCHEMA_STRING_BYTES)
            if type(document) is not dict:
                _fail(
                    "SCHEMA_RUNTIME_REGISTRY_INVALID",
                    "$schema.registry",
                    "Registry resource must be a JSON object",
                )
            if document.get("$id") != identifier:
                _fail(
                    "SCHEMA_RUNTIME_REGISTRY_INVALID",
                    "$schema.registry",
                    "Registry key must exactly match the resource identifier",
                )
            documents.append(document)

        for document in documents:
            identifier = document.get("$id")
            _bounded_text(identifier, "$schema.$id", maximum_bytes=MAX_SCHEMA_STRING_BYTES)
            previous = self.resources.get(identifier)
            if previous is not None and previous is not document:
                _fail(
                    "SCHEMA_RUNTIME_REGISTRY_INVALID",
                    "$schema.$id",
                    "Duplicate schema identifier is unsupported",
                )
            self.resources[identifier] = document

        # Count the complete bundle once.  Shared object identities do not grant
        # extra budget and container cycles still fail closed.
        bundle_budget = _JsonBudget(schema=True)
        for identifier in sorted(self.resources):
            bundle_budget.check(self.resources[identifier], "$schema")

        self.root_id = schema["$id"]
        self._patterns: dict[str, re.Pattern[str]] = {}
        self._audit_completed: set[tuple[str, int]] = set()
        self._audit_active: set[tuple[str, int]] = set()
        self._audit_nodes = 0
        for identifier in sorted(self.resources):
            self._audit_schema(
                self.resources[identifier],
                resource_id=identifier,
                path="$schema",
                depth=0,
                resource_root=True,
            )
        self.steps = 0

    def _resolve_ref(self, reference: str, resource_id: str, path: str) -> tuple[str, dict[str, Any]]:
        if reference.startswith("#"):
            target_id = resource_id
            fragment = reference[1:]
        else:
            base, separator, fragment = reference.partition("#")
            if base not in self.resources:
                _fail(
                    "SCHEMA_RUNTIME_REF_UNRESOLVED",
                    path,
                    "Reference is not an exact identifier in the pinned registry",
                )
            target_id = base
            if not separator:
                return target_id, self.resources[target_id]
        if not fragment.startswith("/$defs/"):
            _fail(
                "SCHEMA_RUNTIME_REF_UNSUPPORTED",
                path,
                "Only root or local definition references are supported",
            )
        raw_segments = fragment[1:].split("/")
        if len(raw_segments) > 16:
            _fail("SCHEMA_RUNTIME_LIMIT_EXCEEDED", path, "Reference path is too deep")
        segments: list[str] = []
        for raw_segment in raw_segments:
            if re.search(r"~(?![01])", raw_segment):
                _fail("SCHEMA_RUNTIME_REF_UNSUPPORTED", path, "Reference escape is invalid")
            segments.append(raw_segment.replace("~1", "/").replace("~0", "~"))
        target: Any = self.resources[target_id]
        for segment in segments:
            if type(target) is not dict or segment not in target:
                _fail("SCHEMA_RUNTIME_REF_UNRESOLVED", path, "Referenced definition is absent")
            target = target[segment]
        if type(target) is not dict:
            _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "Referenced definition is not a schema object")
        return target_id, target

    def _schema_integer(self, schema: dict[str, Any], keyword: str, path: str) -> int | None:
        if keyword not in schema:
            return None
        value = schema[keyword]
        if not _is_int(value):
            _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "Schema bound must be an integer")
        if keyword.startswith("min") or keyword.startswith("max"):
            if keyword not in {"minimum", "maximum"} and value < 0:
                _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "Schema size bound must be nonnegative")
        return value

    def _audit_schema(
        self,
        schema: Any,
        *,
        resource_id: str,
        path: str,
        depth: int,
        resource_root: bool = False,
    ) -> None:
        if type(schema) is not dict:
            _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "Boolean and non-object schemas are unsupported")
        if depth > MAX_SCHEMA_DEPTH:
            _fail("SCHEMA_RUNTIME_LIMIT_EXCEEDED", path, "Schema nesting exceeds the limit")
        key = (resource_id, id(schema))
        if key in self._audit_active:
            _fail("SCHEMA_RUNTIME_REF_CYCLE", path, "Recursive schema cycle is unsupported")
        if key in self._audit_completed:
            return
        self._audit_nodes += 1
        if self._audit_nodes > MAX_SCHEMA_NODES:
            _fail("SCHEMA_RUNTIME_LIMIT_EXCEEDED", path, "Schema node count exceeds the limit")
        unknown = set(schema).difference(_KEYWORDS)
        if unknown:
            _fail("SCHEMA_RUNTIME_KEYWORD_UNSUPPORTED", path, "Schema contains an unsupported keyword")
        if not schema:
            _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "Empty permissive schemas are unsupported")
        if not resource_root and ("$schema" in schema or "$id" in schema):
            _fail(
                "SCHEMA_RUNTIME_SCHEMA_INVALID",
                path,
                "Nested schema dialect or identifier changes are unsupported",
            )
        if resource_root:
            if schema.get("$schema") != DRAFT_2020_12:
                _fail("SCHEMA_RUNTIME_DIALECT_INVALID", path, "Schema dialect is not Draft 2020-12")
            if schema.get("$id") != resource_id:
                _fail("SCHEMA_RUNTIME_REGISTRY_INVALID", path, "Resource identifier mismatch")
        if "title" in schema:
            _bounded_text(schema["title"], path, maximum_bytes=MAX_SCHEMA_STRING_BYTES)
        if "type" in schema and schema["type"] not in _TYPE_NAMES:
            _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "Schema type is unsupported")
        if "additionalProperties" in schema and schema["additionalProperties"] is not False:
            _fail(
                "SCHEMA_RUNTIME_KEYWORD_UNSUPPORTED",
                path,
                "Only additionalProperties=false is supported",
            )
        if "uniqueItems" in schema and type(schema["uniqueItems"]) is not bool:
            _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "uniqueItems must be Boolean")

        minimum_length = self._schema_integer(schema, "minLength", path)
        maximum_length = self._schema_integer(schema, "maxLength", path)
        minimum_items = self._schema_integer(schema, "minItems", path)
        maximum_items = self._schema_integer(schema, "maxItems", path)
        minimum_properties = self._schema_integer(schema, "minProperties", path)
        maximum_properties = self._schema_integer(schema, "maxProperties", path)
        self._schema_integer(schema, "minimum", path)
        self._schema_integer(schema, "maximum", path)
        minimum_contains = self._schema_integer(schema, "minContains", path)
        for lower, upper in (
            (minimum_length, maximum_length),
            (minimum_items, maximum_items),
            (minimum_properties, maximum_properties),
        ):
            if lower is not None and upper is not None and lower > upper:
                _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "Schema minimum exceeds maximum")
        if maximum_items is not None and maximum_items > MAX_INSTANCE_CONTAINER_ITEMS:
            _fail("SCHEMA_RUNTIME_LIMIT_EXCEEDED", path, "Schema array bound exceeds runtime limit")
        if maximum_properties is not None and maximum_properties > MAX_INSTANCE_CONTAINER_ITEMS:
            _fail("SCHEMA_RUNTIME_LIMIT_EXCEEDED", path, "Schema object bound exceeds runtime limit")
        if maximum_length is not None and maximum_length > MAX_INSTANCE_STRING_BYTES:
            _fail("SCHEMA_RUNTIME_LIMIT_EXCEEDED", path, "Schema text bound exceeds runtime limit")
        if minimum_contains is not None and "contains" not in schema:
            _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "minContains requires contains")

        if "pattern" in schema:
            pattern = _bounded_text(schema["pattern"], path, maximum_bytes=MAX_PATTERN_BYTES)
            if pattern not in self._patterns:
                self._patterns[pattern] = _compile_closed_pattern(pattern, path)

        if "required" in schema:
            required = schema["required"]
            if type(required) is not list or len(required) > MAX_INSTANCE_CONTAINER_ITEMS:
                _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "required must be a bounded array")
            if not all(type(item) is str for item in required) or len(set(required)) != len(required):
                _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "required names must be unique text")
        if "enum" in schema:
            enum = schema["enum"]
            if type(enum) is not list or not enum or len(enum) > MAX_SCHEMA_BRANCHES:
                _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "enum must be a bounded nonempty array")
            seen: set[Any] = set()
            for value in enum:
                marker = _fingerprint(value)
                if marker in seen:
                    _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "enum values must be unique")
                seen.add(marker)

        self._audit_active.add(key)
        try:
            if "$defs" in schema:
                definitions = schema["$defs"]
                if type(definitions) is not dict or len(definitions) > MAX_DEFINITIONS:
                    _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "$defs must be a bounded object")
                for name, child in definitions.items():
                    if _DEFINITION_NAME_RE.fullmatch(name) is None:
                        _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "Definition name is unsupported")
                    self._audit_schema(
                        child,
                        resource_id=resource_id,
                        path=f"{path}.$defs.{name}",
                        depth=depth + 1,
                    )
            if "properties" in schema:
                properties = schema["properties"]
                if type(properties) is not dict or len(properties) > MAX_INSTANCE_CONTAINER_ITEMS:
                    _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "properties must be a bounded object")
                for name, child in properties.items():
                    _bounded_text(name, path, maximum_bytes=MAX_SCHEMA_STRING_BYTES)
                    self._audit_schema(
                        child,
                        resource_id=resource_id,
                        path=f"{path}.properties.{name}",
                        depth=depth + 1,
                    )
            for keyword in _SCHEMA_LIST_KEYS:
                if keyword not in schema:
                    continue
                children = schema[keyword]
                if type(children) is not list or not children or len(children) > MAX_SCHEMA_BRANCHES:
                    _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "Schema branch list is invalid")
                for index, child in enumerate(children):
                    self._audit_schema(
                        child,
                        resource_id=resource_id,
                        path=f"{path}.{keyword}[{index}]",
                        depth=depth + 1,
                    )
            for keyword in _SCHEMA_CHILD_KEYS:
                if keyword in schema:
                    if keyword == "items" and schema[keyword] is False:
                        continue
                    self._audit_schema(
                        schema[keyword],
                        resource_id=resource_id,
                        path=f"{path}.{keyword}",
                        depth=depth + 1,
                    )
            if ("then" in schema or "else" in schema) and "if" not in schema:
                _fail("SCHEMA_RUNTIME_SCHEMA_INVALID", path, "then or else requires if")
            if "$ref" in schema:
                reference = _bounded_text(schema["$ref"], path, maximum_bytes=MAX_SCHEMA_STRING_BYTES)
                target_resource, target = self._resolve_ref(reference, resource_id, path)
                self._audit_schema(
                    target,
                    resource_id=target_resource,
                    path=f"{path}.$ref",
                    depth=depth + 1,
                    resource_root=target is self.resources[target_resource],
                )
        finally:
            self._audit_active.remove(key)
        self._audit_completed.add(key)

    def _step(self, path: str) -> None:
        self.steps += 1
        if self.steps > MAX_EVALUATION_STEPS:
            _fail("SCHEMA_RUNTIME_LIMIT_EXCEEDED", path, "Schema evaluation step limit exceeded")

    def _matches(self, schema: dict[str, Any], value: Any, resource_id: str, path: str, depth: int) -> bool:
        try:
            self._validate(schema, value, resource_id, path, depth)
            return True
        except SchemaRuntimeError as error:
            if error.code == "SCHEMA_RUNTIME_LIMIT_EXCEEDED":
                raise
            return False

    def _validate(
        self,
        schema: dict[str, Any],
        value: Any,
        resource_id: str,
        path: str,
        depth: int,
    ) -> None:
        if depth > MAX_SCHEMA_DEPTH + MAX_INSTANCE_DEPTH:
            _fail("SCHEMA_RUNTIME_LIMIT_EXCEEDED", path, "Evaluation nesting exceeds the limit")
        self._step(path)
        if "$ref" in schema:
            target_resource, target = self._resolve_ref(schema["$ref"], resource_id, path)
            self._validate(target, value, target_resource, path, depth + 1)

        if "const" in schema and not _json_equal(value, schema["const"]):
            _fail("SCHEMA_RUNTIME_CONST_MISMATCH", path, "Value does not match const")
        if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
            _fail("SCHEMA_RUNTIME_ENUM_MISMATCH", path, "Value is outside enum")

        expected_type = schema.get("type")
        type_matches = {
            "object": type(value) is dict,
            "array": type(value) is list,
            "string": type(value) is str,
            "integer": _is_int(value),
            "boolean": type(value) is bool,
            "null": value is None,
            None: True,
        }[expected_type]
        if not type_matches:
            _fail("SCHEMA_RUNTIME_TYPE_MISMATCH", path, "Value has the wrong JSON type")

        for child in schema.get("allOf", ()):
            self._validate(child, value, resource_id, path, depth + 1)
        if "anyOf" in schema:
            if not any(self._matches(child, value, resource_id, path, depth + 1) for child in schema["anyOf"]):
                _fail("SCHEMA_RUNTIME_ANY_OF_MISMATCH", path, "No anyOf branch matched")
        if "oneOf" in schema:
            matches = sum(
                self._matches(child, value, resource_id, path, depth + 1)
                for child in schema["oneOf"]
            )
            if matches != 1:
                _fail("SCHEMA_RUNTIME_ONE_OF_MISMATCH", path, "Exactly one oneOf branch must match")
        if "if" in schema:
            branch = "then" if self._matches(schema["if"], value, resource_id, path, depth + 1) else "else"
            if branch in schema:
                self._validate(schema[branch], value, resource_id, path, depth + 1)
        if "not" in schema and self._matches(schema["not"], value, resource_id, path, depth + 1):
            _fail("SCHEMA_RUNTIME_NOT_MISMATCH", path, "Value matched a forbidden schema")

        if type(value) is str:
            if "minLength" in schema and len(value) < schema["minLength"]:
                _fail("SCHEMA_RUNTIME_LENGTH_MISMATCH", path, "Text is shorter than allowed")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                _fail("SCHEMA_RUNTIME_LENGTH_MISMATCH", path, "Text is longer than allowed")
            if "pattern" in schema:
                if len(value) > MAX_PATTERN_INPUT_CHARS:
                    _fail(
                        "SCHEMA_RUNTIME_LIMIT_EXCEEDED",
                        path,
                        "Pattern input exceeds the character limit",
                    )
                if self._patterns[schema["pattern"]].search(value) is None:
                    _fail(
                        "SCHEMA_RUNTIME_PATTERN_MISMATCH",
                        path,
                        "Text does not match the required pattern",
                    )

        if _is_int(value):
            if "minimum" in schema and value < schema["minimum"]:
                _fail("SCHEMA_RUNTIME_RANGE_MISMATCH", path, "Integer is below the minimum")
            if "maximum" in schema and value > schema["maximum"]:
                _fail("SCHEMA_RUNTIME_RANGE_MISMATCH", path, "Integer is above the maximum")

        if type(value) is list:
            if "minItems" in schema and len(value) < schema["minItems"]:
                _fail("SCHEMA_RUNTIME_ITEMS_MISMATCH", path, "Array has too few items")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                _fail("SCHEMA_RUNTIME_ITEMS_MISMATCH", path, "Array has too many items")
            prefix = schema.get("prefixItems", ())
            for index, child in enumerate(prefix[: len(value)]):
                self._validate(child, value[index], resource_id, f"{path}[{index}]", depth + 1)
            if "items" in schema:
                if schema["items"] is False:
                    if len(value) > len(prefix):
                        _fail(
                            "SCHEMA_RUNTIME_ITEMS_MISMATCH",
                            path,
                            "Array has items beyond its fixed prefix",
                        )
                else:
                    for index in range(len(prefix), len(value)):
                        self._validate(
                            schema["items"],
                            value[index],
                            resource_id,
                            f"{path}[{index}]",
                            depth + 1,
                        )
            if "contains" in schema:
                needed = schema.get("minContains", 1)
                matches = 0
                for index, item in enumerate(value):
                    if self._matches(schema["contains"], item, resource_id, f"{path}[{index}]", depth + 1):
                        matches += 1
                        if matches >= needed:
                            break
                if matches < needed:
                    _fail("SCHEMA_RUNTIME_CONTAINS_MISMATCH", path, "Array contains too few matching items")
            if schema.get("uniqueItems"):
                seen: set[Any] = set()
                for item in value:
                    marker = _fingerprint(item)
                    if marker in seen:
                        _fail("SCHEMA_RUNTIME_UNIQUE_MISMATCH", path, "Array items are not unique")
                    seen.add(marker)

        if type(value) is dict:
            if "minProperties" in schema and len(value) < schema["minProperties"]:
                _fail("SCHEMA_RUNTIME_PROPERTIES_MISMATCH", path, "Object has too few properties")
            if "maxProperties" in schema and len(value) > schema["maxProperties"]:
                _fail("SCHEMA_RUNTIME_PROPERTIES_MISMATCH", path, "Object has too many properties")
            for required in schema.get("required", ()):
                if required not in value:
                    _fail("SCHEMA_RUNTIME_REQUIRED_MISSING", path, "Object is missing a required property")
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False and any(key not in properties for key in value):
                _fail("SCHEMA_RUNTIME_ADDITIONAL_PROPERTY", path, "Object has an unknown property")
            for name, child in properties.items():
                if name in value:
                    self._validate(child, value[name], resource_id, f"{path}.{name}", depth + 1)

    def validate(self, instance: Any) -> None:
        _JsonBudget(schema=False).check(instance)
        self.steps = 0
        self._validate(self.resources[self.root_id], instance, self.root_id, "$", 0)


def validate_schema(
    schema: Mapping[str, Any],
    *,
    registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Audit a parsed pinned schema bundle without validating an instance."""

    _ClosedSchemaRuntime(schema, registry)


def validate_instance(
    schema: Mapping[str, Any],
    instance: Any,
    *,
    registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Validate one parsed JSON instance against a parsed pinned schema.

    ``registry`` is local-only.  Its keys must exactly equal each resource's
    ``$id``.  A URI-looking ``$ref`` is resolved only by exact lookup in this
    map; the function has no network or filesystem resolver.
    """

    _ClosedSchemaRuntime(schema, registry).validate(instance)


__all__ = [
    "DRAFT_2020_12",
    "MAX_PATTERN_INPUT_CHARS",
    "MAX_PATTERN_REPEAT",
    "SchemaRuntimeError",
    "validate_instance",
    "validate_schema",
]
