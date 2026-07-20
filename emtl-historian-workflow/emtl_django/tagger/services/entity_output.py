from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from .entity_registry import (
    EntityRegistryService,
    LookupResult,
    RegistryIssue,
    build_default_entity_registry_service,
    normalize_registry_name,
)


ENTITY_OUTPUT_CONTRACT_VERSION = "entity-registry-structured-v1"
ENTITY_TYPES = ("P", "SI", "R", "L", "I", "T", "INT", "V", "TE")
TAG_LINE = re.compile(
    r"^(INT|SI|TE|P|R|L|I|T|V):\s*(.*?)\s+\[([^\]]+)\]\s*(?:\|\s*(.*))?$"
)
HEADER_END_MARKER = re.compile(r"(?im)^[ \t]*<END>[ \t]*(?:\r?\n|$)")
REFERENCE_ID = re.compile(
    r"\[((?:NEW-)?(?:P|SI|R|L|I|T|INT|V|TE|A)-\d{4})\]"
)
PRIMARY_ID_PATTERNS = {
    entity_type: re.compile(rf"^(?:{entity_type}-\d{{4}}|NEW-{entity_type}-\d{{4}})$")
    for entity_type in ENTITY_TYPES
}
FIELD_ORDER = {
    "P": (
        "Trigger",
        "Suffix",
        "Alias",
        "Sex",
        "ImputedBirthYear",
        "Identity",
        "Affiliation",
        "Residence",
        "WorkLocation",
        "Subscription",
    ),
    "SI": (
        "Trigger",
        "Sex",
        "SecondaryIdentity",
        "Affiliation",
        "Attribute",
        "Residence",
        "WorkLocation",
        "RelatedTo",
        "Plural",
    ),
    "R": ("Trigger", "Subject", "Object", "Inverse"),
    "L": ("Trigger", "LocatedIn"),
    "V": ("Trigger", "HomePort"),
    "T": ("Trigger",),
    "INT": ("Trigger",),
    "I": ("Trigger",),
    "TE": ("Trigger",),
}
TYPE_ORDER = {"P": 0, "SI": 1, "L": 2, "I": 3, "T": 4, "INT": 5, "V": 6, "TE": 7}


ENTITY_OUTPUT_SCHEMA = {
    "contract_version": ENTITY_OUTPUT_CONTRACT_VERSION,
    "tag_line": "TYPE: Headword [ID] | Trigger: Value | Key: Value [ID]",
    "types": list(ENTITY_TYPES),
    "field_order": {key: list(value) for key, value in FIELD_ORDER.items()},
    "notes_marker": "TAGGER NOTES & QUESTIONS",
}


def entity_model_output_contract_text() -> str:
    """Render model-facing rules from the same constants used by validation."""
    ordered_types = ", ".join(
        entity_type for entity_type, _ in sorted(TYPE_ORDER.items(), key=lambda item: item[1])
    )
    field_orders = "; ".join(
        f"{entity_type}: {', '.join(fields)}"
        for entity_type, fields in FIELD_ORDER.items()
    )
    return (
        "===== ENTITY OUTPUT ENFORCEMENT CONTRACT =====\n"
        "KNOWN-ID IMMUTABILITY: registry_id_exact and registry_headword_exact in each candidate "
        "row are one indivisible authoritative pair. When using a known ID, copy both values "
        "verbatim from that same row. Never correct, modernize, respell, translate, or combine "
        "an ID with a different Headword. If no candidate is valid, do not attach an altered "
        "Headword to a known ID.\n"
        f"NON-RELATIONSHIP TAG ORDER: output P/SI/L/I/T/INT/V/TE tag lines only in this "
        f"monotonic order: {ordered_types}. Never return to an earlier type. R relationship "
        "lines are excluded from this ordering check.\n"
        f"FIELD ORDER WITHIN EACH TAG (omit unused fields, never reorder present fields): "
        f"{field_orders}.\n"
        "TRIGGER SOURCE: every Trigger value must occur in the Document Body. Copy it from the "
        "Body; Header text is not valid Trigger evidence.\n"
        "These rules are fail-closed deterministic validation requirements."
    )


@dataclass(frozen=True)
class ParsedEntityField:
    key: str
    value: str
    referenced_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "referenced_ids": list(self.referenced_ids),
        }


@dataclass(frozen=True)
class ParsedEntityTag:
    entity_type: str
    headword: str
    record_id: str
    fields: tuple[ParsedEntityField, ...]
    line_number: int
    raw_line: str

    def field_values(self, key: str) -> tuple[str, ...]:
        return tuple(field.value for field in self.fields if field.key == key)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.entity_type,
            "headword": self.headword,
            "id": self.record_id,
            "fields": [field.as_dict() for field in self.fields],
            "line_number": self.line_number,
            "raw_line": self.raw_line,
        }


@dataclass(frozen=True)
class EntityParseIssue:
    code: str
    message: str
    line_number: int | None = None
    raw_line: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "line_number": self.line_number,
            "raw_line": self.raw_line,
        }


@dataclass(frozen=True)
class ParsedEntityOutput:
    header: str
    tags: tuple[ParsedEntityTag, ...]
    notes_block: str
    parse_issues: tuple[EntityParseIssue, ...]
    raw_output: str
    contract_version: str = ENTITY_OUTPUT_CONTRACT_VERSION

    def as_dict(self, *, include_raw_output: bool = False) -> dict[str, Any]:
        data = {
            "contract_version": self.contract_version,
            "header": self.header,
            "tags": [tag.as_dict() for tag in self.tags],
            "notes_block": self.notes_block,
            "parse_issues": [issue.as_dict() for issue in self.parse_issues],
        }
        if include_raw_output:
            data["raw_output"] = self.raw_output
        return data


@dataclass(frozen=True)
class EntityValidationIssue:
    code: str
    message: str
    severity: str = "error"
    line_number: int | None = None
    record_id: str = ""
    field: str = ""
    provenance: tuple[dict[str, Any], ...] = ()
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "line_number": self.line_number,
            "id": self.record_id,
            "field": self.field,
            "provenance": list(self.provenance),
            "details": self.details,
        }


@dataclass(frozen=True)
class EntityValidationResult:
    valid: bool
    requires_human_review: bool
    tag_count: int
    issues: tuple[EntityValidationIssue, ...]
    registry_version: str
    resource_hashes: dict[str, str]
    contract_version: str = ENTITY_OUTPUT_CONTRACT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "requires_human_review": self.requires_human_review,
            "contract_version": self.contract_version,
            "tag_count": self.tag_count,
            "issues": [issue.as_dict() for issue in self.issues],
            "registry_version": self.registry_version,
            "resource_hashes": dict(self.resource_hashes),
        }


def parse_entity_output(raw_output: str) -> ParsedEntityOutput:
    raw = str(raw_output or "").replace("\r\n", "\n").replace("\r", "\n")
    header = ""
    body = raw
    header_match = HEADER_END_MARKER.search(raw)
    if header_match is not None:
        header = raw[: header_match.end()].strip()
        body = raw[header_match.end() :]

    tags: list[ParsedEntityTag] = []
    issues: list[EntityParseIssue] = []
    notes_lines: list[str] = []
    in_notes = False
    header_line_count = header.count("\n") + 1 if header else 0
    for body_line_number, line in enumerate(body.split("\n"), start=1):
        line_number = header_line_count + body_line_number
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "TAGGER NOTES & QUESTIONS":
            in_notes = True
        if in_notes:
            notes_lines.append(line)
            continue
        match = TAG_LINE.fullmatch(stripped)
        if match is None:
            issues.append(
                EntityParseIssue(
                    code="unparsed_output_line",
                    message="Line does not match the formal Entity Registry tag syntax.",
                    line_number=line_number,
                    raw_line=line,
                )
            )
            continue
        entity_type, headword, record_id, fields_blob = match.groups()
        parsed_fields: list[ParsedEntityField] = []
        if fields_blob:
            for field_text in fields_blob.split("|"):
                field_text = field_text.strip()
                if field_text.count(":") != 1:
                    issues.append(
                        EntityParseIssue(
                            code="invalid_field_syntax",
                            message="Each field must contain exactly one colon.",
                            line_number=line_number,
                            raw_line=field_text,
                        )
                    )
                    continue
                key, value = (part.strip() for part in field_text.split(":", 1))
                if not key or not value:
                    issues.append(
                        EntityParseIssue(
                            code="empty_field",
                            message="Entity fields may not have an empty key or value.",
                            line_number=line_number,
                            raw_line=field_text,
                        )
                    )
                parsed_fields.append(
                    ParsedEntityField(
                        key=key,
                        value=value,
                        referenced_ids=tuple(REFERENCE_ID.findall(value)),
                    )
                )
        tags.append(
            ParsedEntityTag(
                entity_type=entity_type,
                headword=headword.strip(),
                record_id=record_id.strip(),
                fields=tuple(parsed_fields),
                line_number=line_number,
                raw_line=line,
            )
        )
    return ParsedEntityOutput(
        header=header,
        tags=tuple(tags),
        notes_block="\n".join(notes_lines).strip(),
        parse_issues=tuple(issues),
        raw_output=raw,
    )


def validate_entity_output(
    parsed: ParsedEntityOutput,
    *,
    expected_header: str,
    source_body: str,
    registry: EntityRegistryService | None = None,
) -> EntityValidationResult:
    registry = registry or build_default_entity_registry_service()
    issues: list[EntityValidationIssue] = [
        EntityValidationIssue(
            code=issue.code,
            message=issue.message,
            line_number=issue.line_number,
            details={"raw_line": issue.raw_line},
        )
        for issue in parsed.parse_issues
    ]
    if parsed.header != str(expected_header or "").strip():
        issues.append(
            EntityValidationIssue(
                code="header_mismatch",
                message="Generated Header does not exactly reproduce the expected Header.",
            )
        )
    if not parsed.tags:
        issues.append(
            EntityValidationIssue(
                code="no_entity_tags",
                message="No formal Entity Registry tag lines were parsed.",
            )
        )
    if parsed.notes_block:
        issues.append(
            EntityValidationIssue(
                code="tagger_notes_or_questions_require_review",
                message="The Tagger Notes & Questions block requires historian review.",
                severity="human_review",
            )
        )

    previous_type_order = -1
    new_ids: dict[str, list[int]] = defaultdict(list)
    output_primary_ids = {tag.record_id for tag in parsed.tags}
    normalized_source = normalize_registry_name(source_body)

    for tag in parsed.tags:
        expected_pattern = PRIMARY_ID_PATTERNS[tag.entity_type]
        if expected_pattern.fullmatch(tag.record_id) is None:
            issues.append(
                EntityValidationIssue(
                    code="primary_id_type_mismatch",
                    message=f"{tag.record_id!r} is not a valid {tag.entity_type} ID.",
                    line_number=tag.line_number,
                    record_id=tag.record_id,
                )
            )
        elif tag.record_id.startswith("NEW-"):
            new_ids[tag.entity_type].append(int(tag.record_id.rsplit("-", 1)[1]))
            issues.append(
                EntityValidationIssue(
                    code="new_id_requires_human_review",
                    message="A NEW-ID is structurally valid but requires historian confirmation.",
                    severity="human_review",
                    line_number=tag.line_number,
                    record_id=tag.record_id,
                )
            )
        else:
            issues.extend(
                _lookup_issues(
                    registry.validate_id_headword(tag.record_id, tag.headword),
                    line_number=tag.line_number,
                    field="Headword",
                )
            )
            headword_candidates = registry.lookup_headword(tag.headword)
            candidate_ids = {record.record_id for record in headword_candidates.candidates}
            if len(candidate_ids) > 1 and tag.record_id in candidate_ids:
                issues.append(
                    EntityValidationIssue(
                        code="headword_identity_requires_human_review",
                        message=(
                            "The normalized Headword has multiple registry IDs; the chosen identity "
                            "is structurally possible but cannot be confirmed deterministically."
                        ),
                        severity="human_review",
                        line_number=tag.line_number,
                        record_id=tag.record_id,
                        provenance=tuple(
                            record.provenance.as_dict()
                            for record in headword_candidates.candidates
                        ),
                        details={"candidate_ids": sorted(candidate_ids)},
                    )
                )

        if tag.entity_type == "R":
            issues.append(
                EntityValidationIssue(
                    code="relationship_semantics_require_human_review",
                    message=(
                        "Relationship syntax and IDs are deterministic, but relationship eligibility "
                        "and direction require semantic historian review."
                    ),
                    severity="human_review",
                    line_number=tag.line_number,
                    record_id=tag.record_id,
                )
            )

        if tag.entity_type != "R":
            current_type_order = TYPE_ORDER[tag.entity_type]
            if current_type_order < previous_type_order:
                issues.append(
                    EntityValidationIssue(
                        code="tag_type_order_violation",
                        message="Entity tag type order does not follow the formal Entity Registry ordering.",
                        line_number=tag.line_number,
                        record_id=tag.record_id,
                    )
                )
            previous_type_order = max(previous_type_order, current_type_order)

        allowed_order = FIELD_ORDER[tag.entity_type]
        allowed_indexes = {key: index for index, key in enumerate(allowed_order)}
        field_indexes = []
        trigger_count = 0
        for parsed_field in tag.fields:
            if parsed_field.key not in allowed_indexes:
                issues.append(
                    EntityValidationIssue(
                        code="field_not_allowed_for_type",
                        message=f"Field {parsed_field.key!r} is not allowed on {tag.entity_type} tags.",
                        line_number=tag.line_number,
                        record_id=tag.record_id,
                        field=parsed_field.key,
                    )
                )
                continue
            field_indexes.append(allowed_indexes[parsed_field.key])
            if parsed_field.key == "Trigger":
                trigger_count += 1
                for trigger in _split_trigger_values(parsed_field.value):
                    if normalize_registry_name(trigger) not in normalized_source:
                        issues.append(
                            EntityValidationIssue(
                                code="trigger_not_found_in_document_body",
                                message=f"Trigger {trigger!r} was not found in the Document Body.",
                                line_number=tag.line_number,
                                record_id=tag.record_id,
                                field="Trigger",
                            )
                        )
                    watch_result = registry.lookup_watch(trigger)
                    if watch_result.candidates:
                        listed_ids = {record.record_id for record in watch_result.candidates}
                        if tag.record_id not in listed_ids and not tag.record_id.startswith("NEW-"):
                            issues.append(
                                EntityValidationIssue(
                                    code="watch_list_id_mismatch",
                                    message=(
                                        f"Trigger {trigger!r} is watch-listed, but ID {tag.record_id} is not one of its listed senses."
                                    ),
                                    line_number=tag.line_number,
                                    record_id=tag.record_id,
                                    field="Trigger",
                                    provenance=tuple(
                                        record.provenance.as_dict()
                                        for record in watch_result.candidates
                                    ),
                                    details={"listed_ids": sorted(listed_ids)},
                                )
                            )
                        elif len(listed_ids) > 1:
                            issues.append(
                                EntityValidationIssue(
                                    code="watch_context_requires_human_review",
                                    message=(
                                        f"Trigger {trigger!r} has multiple watch-listed senses; "
                                        "the selected ID is allowed but contextual meaning requires review."
                                    ),
                                    severity="human_review",
                                    line_number=tag.line_number,
                                    record_id=tag.record_id,
                                    field="Trigger",
                                    provenance=tuple(
                                        record.provenance.as_dict()
                                        for record in watch_result.candidates
                                    ),
                                    details={"listed_ids": sorted(listed_ids)},
                                )
                            )
            for referenced_id in parsed_field.referenced_ids:
                issues.extend(
                    _validate_reference(
                        registry=registry,
                        tag=tag,
                        parsed_field=parsed_field,
                        referenced_id=referenced_id,
                        output_primary_ids=output_primary_ids,
                    )
                )
        if field_indexes != sorted(field_indexes):
            issues.append(
                EntityValidationIssue(
                    code="field_order_violation",
                    message=f"Fields on {tag.entity_type} tag are not in the required order.",
                    line_number=tag.line_number,
                    record_id=tag.record_id,
                )
            )
        if trigger_count != 1:
            issues.append(
                EntityValidationIssue(
                    code="trigger_field_count",
                    message="Each Entity tag must contain exactly one Trigger field.",
                    line_number=tag.line_number,
                    record_id=tag.record_id,
                    details={"count": trigger_count},
                )
            )

    for entity_type, numbers in sorted(new_ids.items()):
        expected = list(range(1, len(numbers) + 1))
        if sorted(numbers) != expected or len(numbers) != len(set(numbers)):
            issues.append(
                EntityValidationIssue(
                    code="new_id_sequence_invalid",
                    message=f"NEW-{entity_type} IDs must be unique and sequential from 0001.",
                    details={"observed": numbers, "expected": expected},
                )
            )

    valid = not any(issue.severity in {"error", "ambiguity"} for issue in issues)
    requires_human_review = any(issue.severity == "human_review" for issue in issues)
    return EntityValidationResult(
        valid=valid,
        requires_human_review=requires_human_review,
        tag_count=len(parsed.tags),
        issues=tuple(issues),
        registry_version=registry.registry_version,
        resource_hashes=registry.resource_hashes,
    )


def parse_and_validate_entity_output(
    raw_output: str,
    *,
    expected_header: str,
    source_body: str,
    registry: EntityRegistryService | None = None,
) -> tuple[ParsedEntityOutput, EntityValidationResult]:
    parsed = parse_entity_output(raw_output)
    validation = validate_entity_output(
        parsed,
        expected_header=expected_header,
        source_body=source_body,
        registry=registry,
    )
    return parsed, validation


def _validate_reference(
    *,
    registry: EntityRegistryService,
    tag: ParsedEntityTag,
    parsed_field: ParsedEntityField,
    referenced_id: str,
    output_primary_ids: set[str],
) -> list[EntityValidationIssue]:
    if referenced_id.startswith("NEW-"):
        if referenced_id not in output_primary_ids:
            return [
                EntityValidationIssue(
                    code="new_id_reference_missing_tag",
                    message=f"Referenced NEW-ID {referenced_id} has no corresponding output tag.",
                    line_number=tag.line_number,
                    record_id=tag.record_id,
                    field=parsed_field.key,
                )
            ]
        return []
    if referenced_id.startswith("A-"):
        if tag.entity_type != "SI" or parsed_field.key != "Attribute":
            return [
                EntityValidationIssue(
                    code="attribute_id_wrong_field",
                    message="A-IDs are allowed only in the Attribute field of SI tags.",
                    line_number=tag.line_number,
                    record_id=tag.record_id,
                    field=parsed_field.key,
                )
            ]
        return _lookup_issues(
            registry.validate_attribute_id(referenced_id),
            line_number=tag.line_number,
            field=parsed_field.key,
        )
    return _lookup_issues(
        registry.lookup_id(referenced_id),
        line_number=tag.line_number,
        field=parsed_field.key,
    )


def _lookup_issues(
    result: LookupResult,
    *,
    line_number: int,
    field: str,
) -> list[EntityValidationIssue]:
    if result.status == "match":
        return []
    if not result.ambiguities:
        return [
            EntityValidationIssue(
                code="registry_lookup_not_found",
                message=f"Registry lookup did not resolve {result.query!r}.",
                line_number=line_number,
                field=field,
            )
        ]
    return [
        _registry_issue_to_validation(issue, line_number=line_number, field=field)
        for issue in result.ambiguities
    ]


def _registry_issue_to_validation(
    issue: RegistryIssue,
    *,
    line_number: int,
    field: str,
) -> EntityValidationIssue:
    return EntityValidationIssue(
        code=issue.code,
        message=issue.message,
        severity="ambiguity" if issue.severity == "ambiguity" else issue.severity,
        line_number=line_number,
        record_id=issue.record_id,
        field=field,
        provenance=tuple(item.as_dict() for item in issue.provenance),
        details=issue.details,
    )


def _split_trigger_values(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(";") if item.strip()]
