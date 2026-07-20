from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .entity_registry import (
    EntityRegistryService,
    OptionalVectorCandidateLayer,
    RegistryIssue,
    RegistryRecord,
    build_default_entity_registry_service,
    normalize_registry_name,
)


ENTITY_KNOWLEDGE_CONTRACT_VERSION = "entity-bounded-knowledge-v2"
DEFAULT_CANDIDATE_TOKEN_BUDGET = 8_000


class EntityKnowledgeBuildError(RuntimeError):
    def __init__(self, code: str, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or {}


@dataclass(frozen=True)
class EntityKnowledgeRetrievalOptions:
    candidate_token_budget: int = DEFAULT_CANDIDATE_TOKEN_BUDGET
    max_candidates: int = 80
    enable_fuzzy_candidates: bool = True
    fuzzy_score_threshold: float = 0.92
    max_fuzzy_queries: int = 20
    per_fuzzy_query_limit: int = 1
    fuzzy_pool_per_query_limit: int = 5
    max_fuzzy_candidates: int = 20
    allow_optional_vector_layer: bool = False


@dataclass(frozen=True)
class EntityKnowledgeCandidate:
    record: RegistryRecord
    matched_terms: tuple[str, ...]
    match_layers: tuple[str, ...]
    score: float

    def as_prompt_dict(self) -> dict[str, Any]:
        """Legacy v1 projection retained only for tokenizer comparison diagnostics."""
        return {
            "kind": self.record.kind,
            "id": self.record.record_id,
            "headword": self.record.headword,
            "matched_terms": list(self.matched_terms),
            "match_layers": list(self.match_layers),
            "candidate_score": round(self.score, 6),
            "source_fields": _candidate_source_fields(self.record),
            "provenance": {
                "source_file": self.record.provenance.source_file,
                "sheet": self.record.provenance.sheet,
                "row": self.record.provenance.row,
                "source_hash": self.record.provenance.source_hash,
            },
        }

    def as_model_row(self, *, candidate_key: str, rank: int) -> list[Any]:
        """Compact model-visible projection; strings are complete and unmodified."""
        return [
            candidate_key,
            rank,
            self.record.kind,
            self.record.record_id,
            self.record.headword,
            list(self.match_layers),
            list(self.matched_terms),
            round(self.score, 6),
            [
                [field["name"], field["value"]]
                for field in _candidate_source_fields(self.record)
            ],
        ]

    def audit_dict(self, *, candidate_key: str, rank: int) -> dict[str, Any]:
        return {
            "candidate_key": candidate_key,
            "rank": rank,
            "kind": self.record.kind,
            "id": self.record.record_id,
            "headword": self.record.headword,
            "matched_terms": list(self.matched_terms),
            "match_layers": list(self.match_layers),
            "candidate_score": round(self.score, 6),
            "source_provenance": self.record.provenance.as_dict(),
        }


@dataclass(frozen=True)
class EntityKnowledgePackage:
    registry_version: str
    resource_hashes: dict[str, str]
    indexed_resources: tuple[dict[str, Any], ...]
    candidates: tuple[EntityKnowledgeCandidate, ...]
    watch_candidates: tuple[EntityKnowledgeCandidate, ...]
    ambiguities: tuple[RegistryIssue, ...]
    document_provenance: dict[str, Any]
    candidate_token_budget: int
    candidate_character_count: int
    out_of_band_provenance_character_count: int
    legacy_v1_candidate_character_count: int
    vector_layer_enabled: bool
    provenance_complete: bool
    source_complete: bool
    selection_policy_complete: bool
    mandatory_candidates_retained: bool
    duplicate_elimination_count: int
    excluded_candidates: tuple[dict[str, Any], ...]
    query_coverage: tuple[dict[str, Any], ...]
    contract_version: str = ENTITY_KNOWLEDGE_CONTRACT_VERSION

    def prompt_payload(self) -> dict[str, Any]:
        rows = []
        for rank, candidate in enumerate(self.candidates, start=1):
            rows.append(candidate.as_model_row(candidate_key=f"C{rank:04d}", rank=rank))
        for rank, candidate in enumerate(self.watch_candidates, start=1):
            rows.append(candidate.as_model_row(candidate_key=f"W{rank:04d}", rank=rank))
        return {
            "v": self.contract_version,
            "rv": self.registry_version,
            "columns": [
                "key",
                "rank",
                "kind",
                "registry_id_exact",
                "registry_headword_exact",
                "match_layers",
                "matched_terms",
                "score",
                "source_fields_name_value",
            ],
            "rows": rows,
        }

    def prompt_text(self) -> str:
        return json.dumps(
            self.prompt_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def provenance_dict(self) -> dict[str, Any]:
        candidate_provenance = [
            candidate.audit_dict(candidate_key=f"C{rank:04d}", rank=rank)
            for rank, candidate in enumerate(self.candidates, start=1)
        ] + [
            candidate.audit_dict(candidate_key=f"W{rank:04d}", rank=rank)
            for rank, candidate in enumerate(self.watch_candidates, start=1)
        ]
        return {
            "contract_version": self.contract_version,
            "registry_version": self.registry_version,
            "resource_hashes": dict(self.resource_hashes),
            "indexed_resources": list(self.indexed_resources),
            "candidate_count": len(self.candidates),
            "watch_candidate_count": len(self.watch_candidates),
            "model_visible_candidate_character_count": self.candidate_character_count,
            "out_of_band_provenance_character_count": self.out_of_band_provenance_character_count,
            "legacy_v1_candidate_character_count": self.legacy_v1_candidate_character_count,
            "candidate_token_budget": self.candidate_token_budget,
            "candidate_token_budget_evaluation": "qwen_tokenizer_preflight_required",
            "vector_layer_enabled": self.vector_layer_enabled,
            "provenance_complete": self.provenance_complete,
            "source_complete": self.source_complete,
            "candidate_package_complete": (
                self.source_complete
                and self.selection_policy_complete
                and self.mandatory_candidates_retained
            ),
            "candidate_package_complete_semantics": {
                "all_three_registry_resources_fully_indexed": len(self.indexed_resources) == 3,
                "deterministic_selection_policy_executed": self.selection_policy_complete,
                "all_policy_required_candidates_included": self.mandatory_candidates_retained,
                "full_registry_tables_entered_model_prompt": False,
            },
            "selection_policy_complete": self.selection_policy_complete,
            "mandatory_candidates_retained": self.mandatory_candidates_retained,
            "selection_policy": {
                "exact_and_normalized_required": True,
                "watch_matches_required": True,
                "fuzzy_ranking": "score_desc_then_registry_deterministic_order",
                "fuzzy_per_query_top_k": True,
                "document_record_deduplication": True,
                "duplicate_ids_across_distinct_source_rows_remain_ambiguous": True,
                "optional_vector_enabled": self.vector_layer_enabled,
                "vector_may_select_final_id": False,
            },
            "duplicate_elimination_count": self.duplicate_elimination_count,
            "selection_counts": _selection_counts(self.candidates, self.watch_candidates),
            "candidate_provenance": candidate_provenance,
            "excluded_candidates": list(self.excluded_candidates),
            "query_coverage": list(self.query_coverage),
            "ambiguity_count": len(self.ambiguities),
            "document_scope": dict(self.document_provenance),
        }

    def legacy_v1_prompt_text(self) -> str:
        payload = {
            "contract_version": "entity-bounded-knowledge-v1",
            "registry_version": self.registry_version,
            "resource_hashes": dict(self.resource_hashes),
            "indexed_resources": list(self.indexed_resources),
            "document_scope": dict(self.document_provenance),
            "retrieval_policy": {
                "exact_and_normalized_are_candidates_only": True,
                "fuzzy_is_candidate_supplement_only": True,
                "optional_vector_enabled": self.vector_layer_enabled,
                "vector_may_select_final_id": False,
                "final_id_requires_structured_registry_validation": True,
                "header_used_as_entity_evidence": False,
                "document_level_deduplication": True,
            },
            "candidates": [candidate.as_prompt_dict() for candidate in self.candidates],
            "watch_candidates": [candidate.as_prompt_dict() for candidate in self.watch_candidates],
            "ambiguities": [issue.as_dict() for issue in self.ambiguities],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class EntityKnowledgeRetriever:
    """Build a bounded Entity-only candidate package from fully indexed resources.

    The Header is carried for provenance but never used to find entities. Exact,
    normalized, fuzzy, and optional-vector layers can only propose candidates.
    """

    def __init__(
        self,
        *,
        registry: EntityRegistryService | None = None,
        options: EntityKnowledgeRetrievalOptions | None = None,
        vector_layer: OptionalVectorCandidateLayer | None = None,
    ) -> None:
        self.registry = registry or build_default_entity_registry_service()
        self.options = options or EntityKnowledgeRetrievalOptions()
        self.vector_layer = vector_layer
        if vector_layer is not None and not self.options.allow_optional_vector_layer:
            raise EntityKnowledgeBuildError(
                "entity_vector_layer_disabled",
                "The optional Entity vector candidate layer is disabled by default.",
            )

    def build(self, *, document_header: str, document_body: str) -> EntityKnowledgePackage:
        snapshot = self.registry.snapshot
        body = str(document_body or "")
        normalized_body = normalize_registry_name(body)
        candidates: dict[tuple[str, str, int | None], dict[str, Any]] = {}
        watch_candidates: dict[tuple[str, str, int | None], dict[str, Any]] = {}
        ambiguities: list[RegistryIssue] = []
        excluded_candidates: list[dict[str, Any]] = []
        query_coverage: list[dict[str, Any]] = []
        duplicate_elimination_count = 0

        for record in snapshot.entity_records:
            for term in _record_terms(record):
                layer = _match_layer(term, body, normalized_body)
                if layer:
                    if not _add_candidate(
                        candidates,
                        record,
                        term,
                        layer,
                        1.0 if layer == "exact" else 0.99,
                    ):
                        duplicate_elimination_count += 1
        for record in snapshot.attribute_records:
            for term in _attribute_terms(record):
                layer = _match_layer(term, body, normalized_body)
                if layer:
                    if not _add_candidate(
                        candidates,
                        record,
                        term,
                        layer,
                        1.0 if layer == "exact" else 0.99,
                    ):
                        duplicate_elimination_count += 1

        for record in snapshot.watch_records:
            for term in _watch_terms(record):
                layer = _match_layer(term, body, normalized_body)
                if not layer:
                    continue
                if not _add_candidate(
                    watch_candidates,
                    record,
                    term,
                    f"watch_{layer}",
                    1.0,
                ):
                    duplicate_elimination_count += 1
                referenced = (
                    self.registry.validate_attribute_id(record.record_id)
                    if record.record_id.startswith("A-")
                    else self.registry.lookup_id(record.record_id)
                )
                if referenced.status == "match":
                    if not _add_candidate(
                        candidates,
                        referenced.candidates[0],
                        term,
                        "watch_reference",
                        1.0,
                    ):
                        duplicate_elimination_count += 1
                else:
                    ambiguities.extend(referenced.ambiguities)

        mandatory_candidate_count = len(candidates) + len(watch_candidates)
        if mandatory_candidate_count > self.options.max_candidates:
            raise EntityKnowledgeBuildError(
                "entity_mandatory_candidate_count_exceeded",
                "Exact, normalized, and Watch List candidates exceed the configured limit; none were removed.",
                diagnostics={
                    "mandatory_candidate_count": mandatory_candidate_count,
                    "max_candidates": self.options.max_candidates,
                },
            )

        if self.options.enable_fuzzy_candidates:
            selected_fuzzy_keys: set[tuple[str, str, int | None]] = set()
            for query in _fuzzy_queries(body, self.options.max_fuzzy_queries):
                result = self.registry.retrieve_candidates(
                    query,
                    top_k=max(
                        int(self.options.per_fuzzy_query_limit),
                        int(self.options.fuzzy_pool_per_query_limit),
                    ),
                    min_fuzzy_score=self.options.fuzzy_score_threshold,
                    vector_layer=self.vector_layer,
                )
                ambiguities.extend(result.ambiguities)
                selected_for_query: list[dict[str, Any]] = []
                excluded_for_query: list[dict[str, Any]] = []
                eligible_rank = 0
                for result_rank, match in enumerate(result.candidates, start=1):
                    if match.layer not in {"lexical_fuzzy", "exact", "normalized"} and self.vector_layer is None:
                        continue
                    key = _record_key(match.record)
                    already_selected = key in candidates
                    eligible_rank += 1
                    allowed = eligible_rank <= max(0, int(self.options.per_fuzzy_query_limit))
                    exclusion_reason = "per_query_fuzzy_top_k"
                    if match.layer in {"exact", "normalized"} and not already_selected:
                        allowed = True
                        exclusion_reason = ""
                    if match.layer == "lexical_fuzzy":
                        if (
                            allowed
                            and not already_selected
                            and len(selected_fuzzy_keys) >= max(0, int(self.options.max_fuzzy_candidates))
                        ):
                            allowed = False
                            exclusion_reason = "document_fuzzy_candidate_limit"
                        if (
                            allowed
                            and not already_selected
                            and len(candidates) + len(watch_candidates) >= self.options.max_candidates
                        ):
                            allowed = False
                            exclusion_reason = "document_candidate_limit"
                    audit_item = _fuzzy_audit_item(
                        query=query,
                        result_rank=result_rank,
                        record=match.record,
                        layer=match.layer,
                        score=match.score,
                    )
                    if not allowed:
                        audit_item["reason"] = exclusion_reason
                        excluded_candidates.append(audit_item)
                        excluded_for_query.append(
                            {
                                "id": match.record.record_id,
                                "headword": match.record.headword,
                                "rank": result_rank,
                                "reason": exclusion_reason,
                            }
                        )
                        continue
                    was_new = _add_candidate(
                        candidates,
                        match.record,
                        match.matched_text or query,
                        match.layer,
                        match.score,
                    )
                    if not was_new:
                        duplicate_elimination_count += 1
                    elif match.layer == "lexical_fuzzy":
                        selected_fuzzy_keys.add(key)
                    selected_for_query.append(
                        {
                            "id": match.record.record_id,
                            "headword": match.record.headword,
                            "rank": result_rank,
                            "layer": match.layer,
                        }
                    )
                query_coverage.append(
                    {
                        "document_term": query,
                        "normalized_term": normalize_registry_name(query),
                        "selected": selected_for_query,
                        "excluded": excluded_for_query,
                        "per_query_fuzzy_top_k": max(
                            0, int(self.options.per_fuzzy_query_limit)
                        ),
                    }
                )

        ordered_candidates = _finalize_candidates(candidates)
        ordered_watch = _finalize_candidates(watch_candidates)
        total_candidates = len(ordered_candidates) + len(ordered_watch)
        if total_candidates > self.options.max_candidates:
            raise EntityKnowledgeBuildError(
                "entity_candidate_count_exceeded",
                "The deterministic selected Entity candidate set exceeds the configured limit.",
                diagnostics={
                    "candidate_count": len(ordered_candidates),
                    "watch_candidate_count": len(ordered_watch),
                    "total_candidate_count": total_candidates,
                    "max_candidates": self.options.max_candidates,
                },
            )

        indexed_resources = _indexed_resource_metadata(snapshot)
        provenance_complete = all(
            candidate.record.provenance.source_hash
            and candidate.record.provenance.row is not None
            and candidate.record.provenance.registry_version == snapshot.registry_version
            for candidate in ordered_candidates + ordered_watch
        )
        document_provenance = {
            "header_character_count": len(str(document_header or "")),
            "header_sha256": hashlib.sha256(str(document_header or "").encode("utf-8")).hexdigest(),
            "body_character_count": len(body),
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "retrieval_evidence_scope": "document_body_only",
        }
        package = EntityKnowledgePackage(
            registry_version=snapshot.registry_version,
            resource_hashes=snapshot.resource_hashes,
            indexed_resources=indexed_resources,
            candidates=ordered_candidates,
            watch_candidates=ordered_watch,
            ambiguities=tuple(_deduplicate_registry_issues(ambiguities)),
            document_provenance=document_provenance,
            candidate_token_budget=max(1, int(self.options.candidate_token_budget)),
            candidate_character_count=0,
            out_of_band_provenance_character_count=0,
            legacy_v1_candidate_character_count=0,
            vector_layer_enabled=self.vector_layer is not None,
            provenance_complete=provenance_complete,
            source_complete=False,
            selection_policy_complete=True,
            mandatory_candidates_retained=True,
            duplicate_elimination_count=duplicate_elimination_count,
            excluded_candidates=tuple(excluded_candidates),
            query_coverage=tuple(query_coverage),
        )
        candidate_character_count = len(package.prompt_text())
        audit_payload = package.provenance_dict()
        out_of_band_character_count = len(
            json.dumps(audit_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        legacy_v1_character_count = len(package.legacy_v1_prompt_text())
        source_complete = (
            provenance_complete
            and len(indexed_resources) == 3
            and package.selection_policy_complete
            and package.mandatory_candidates_retained
        )
        package = EntityKnowledgePackage(
            **{
                **package.__dict__,
                "candidate_character_count": candidate_character_count,
                "out_of_band_provenance_character_count": out_of_band_character_count,
                "legacy_v1_candidate_character_count": legacy_v1_character_count,
                "source_complete": source_complete,
            }
        )
        for _ in range(3):
            measured = len(
                json.dumps(
                    package.provenance_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            if measured == package.out_of_band_provenance_character_count:
                break
            package = EntityKnowledgePackage(
                **{
                    **package.__dict__,
                    "out_of_band_provenance_character_count": measured,
                }
            )
        if not package.source_complete:
            raise EntityKnowledgeBuildError(
                "entity_candidate_provenance_incomplete",
                "Entity candidate provenance is incomplete; prompt construction is blocked.",
            )
        return package


def _record_terms(record: RegistryRecord) -> tuple[str, ...]:
    values = [record.headword]
    for field_name in ("Trigger", "Surname", "Alias"):
        for value in record.values(field_name):
            values.extend(_split_terms(value))
    return _unique_terms(values)


def _candidate_source_fields(record: RegistryRecord) -> list[dict[str, Any]]:
    """Return the bounded, role-relevant fields for one candidate.

    The full immutable row remains available in EntityRegistryService. This is
    a typed candidate projection, not a truncated copy of the source resource.
    """

    if record.kind == "watch":
        allowed = {
            "Word",
            "Possible Forms (non-exhaustive)",
            "Meaning",
            "Example",
        }
    elif record.kind == "attribute":
        allowed = {"Classification", "Definition", "Tagging Note"}
    else:
        allowed = {
            "Qualifier",
            "Sex",
            "Identity",
            "Residence",
            "Work Location",
            "Classification",
            "Sub-Class",
            "Class",
            "Function",
            "Inverse exists",
            "Inverse Relationship",
            "Located In",
            "HomePort",
            "HomePortID",
        }
    return [
        field.as_dict()
        for field in record.fields
        if field.value and field.name in allowed
    ]


def _attribute_terms(record: RegistryRecord) -> tuple[str, ...]:
    values = [record.headword]
    for value in record.values("Form"):
        values.extend(_split_terms(value))
    return _unique_terms(values)


def _watch_terms(record: RegistryRecord) -> tuple[str, ...]:
    values = list(record.values("Word"))
    for value in record.values("Possible Forms (non-exhaustive)"):
        values.extend(_split_terms(value))
    return _unique_terms(values)


def _unique_terms(values: list[str]) -> tuple[str, ...]:
    result = []
    seen = set()
    for value in values:
        cleaned = str(value or "").strip()
        normalized = normalize_registry_name(cleaned)
        if len(normalized) < 3 or normalized in seen:
            continue
        seen.add(normalized)
        result.append(cleaned)
    return tuple(result)


def _split_terms(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[;\n]", str(value or "")) if item.strip()]


def _match_layer(term: str, raw_body: str, normalized_body: str) -> str:
    raw_pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)")
    if raw_pattern.search(raw_body):
        return "exact"
    normalized_term = normalize_registry_name(term)
    if not normalized_term:
        return ""
    normalized_pattern = re.compile(rf"(?<!\w){re.escape(normalized_term)}(?!\w)")
    return "normalized" if normalized_pattern.search(normalized_body) else ""


def _fuzzy_queries(body: str, limit: int) -> tuple[str, ...]:
    result = []
    seen = set()
    for match in re.finditer(r"[^\W\d_][\w'’.-]{3,}", str(body or ""), flags=re.UNICODE):
        value = match.group(0)
        normalized = normalize_registry_name(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
        if len(result) >= max(0, int(limit)):
            break
    return tuple(result)


def _record_key(record: RegistryRecord) -> tuple[str, str, int | None]:
    return (record.provenance.source_file, record.provenance.sheet, record.provenance.row)


def _add_candidate(
    target: dict[tuple[str, str, int | None], dict[str, Any]],
    record: RegistryRecord,
    term: str,
    layer: str,
    score: float,
) -> bool:
    key = _record_key(record)
    is_new = key not in target
    entry = target.setdefault(
        key,
        {"record": record, "terms": [], "layers": [], "score": 0.0},
    )
    if term not in entry["terms"]:
        entry["terms"].append(term)
    if layer not in entry["layers"]:
        entry["layers"].append(layer)
    entry["score"] = max(float(entry["score"]), float(score))
    return is_new


def _finalize_candidates(
    values: dict[tuple[str, str, int | None], dict[str, Any]],
) -> tuple[EntityKnowledgeCandidate, ...]:
    candidates = [
        EntityKnowledgeCandidate(
            record=value["record"],
            matched_terms=tuple(value["terms"]),
            match_layers=tuple(value["layers"]),
            score=float(value["score"]),
        )
        for value in values.values()
    ]
    candidates.sort(
        key=lambda item: (
            -item.score,
            item.record.kind,
            item.record.normalized_headword,
            item.record.record_id,
            item.record.provenance.sheet,
            item.record.provenance.row or 0,
        )
    )
    return tuple(candidates)


def _indexed_resource_metadata(snapshot: Any) -> tuple[dict[str, Any], ...]:
    counts = {
        "Entity_List.xlsx": len(snapshot.entity_records),
        "Attributes_List.csv": len(snapshot.attribute_records),
        "Disambiguation_Watch_List.csv": len(snapshot.watch_records),
    }
    return tuple(
        {
            "source_file": source_file,
            "source_hash": snapshot.resource_hashes[source_file],
            "registry_version": snapshot.registry_version,
            "record_count": counts[source_file],
            "fully_indexed": True,
            "loaded_into_prompt_as_full_text": False,
        }
        for source_file in sorted(counts)
    )


def _fuzzy_audit_item(
    *,
    query: str,
    result_rank: int,
    record: RegistryRecord,
    layer: str,
    score: float,
) -> dict[str, Any]:
    return {
        "document_term": query,
        "normalized_term": normalize_registry_name(query),
        "rank": result_rank,
        "kind": record.kind,
        "id": record.record_id,
        "headword": record.headword,
        "layer": layer,
        "score": round(float(score), 6),
        "source_provenance": record.provenance.as_dict(),
    }


def _selection_counts(
    candidates: tuple[EntityKnowledgeCandidate, ...],
    watch_candidates: tuple[EntityKnowledgeCandidate, ...],
) -> dict[str, int]:
    exact = normalized = fuzzy = watch_reference = other = 0
    for candidate in candidates:
        layers = set(candidate.match_layers)
        if "exact" in layers:
            exact += 1
        elif "normalized" in layers:
            normalized += 1
        elif "watch_reference" in layers:
            watch_reference += 1
        elif "lexical_fuzzy" in layers:
            fuzzy += 1
        else:
            other += 1
    return {
        "entity_or_attribute_candidates": len(candidates),
        "watch_candidates": len(watch_candidates),
        "exact": exact,
        "normalized": normalized,
        "watch_reference": watch_reference,
        "fuzzy": fuzzy,
        "other": other,
    }


def _deduplicate_registry_issues(issues: list[RegistryIssue]) -> list[RegistryIssue]:
    result = []
    seen = set()
    for issue in issues:
        key = (
            issue.code,
            issue.record_id,
            issue.headword,
            tuple((item.source_file, item.sheet, item.row) for item in issue.provenance),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result
