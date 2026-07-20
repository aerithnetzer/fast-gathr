from __future__ import annotations

from dataclasses import dataclass

from .services.contracts import PROVIDER_LABELS, STAGE_CONTRACT_VERSION


@dataclass(frozen=True)
class StageProfile:
    stage_id: str
    label: str
    short_label: str
    requires: tuple[str, ...] = ()
    input_summary: str = ""
    future: bool = False
    control_key: str = ""
    output_contract: str = ""
    allowed_providers: tuple[str, ...] = PROVIDER_LABELS


STAGE_PROFILES: dict[str, StageProfile] = {
    "summary_keywords": StageProfile(
        stage_id="summary_keywords",
        label="Summary & Keyword",
        short_label="Summary",
        input_summary="Header + text",
        control_key="summary_keywords",
        output_contract="labelled_text_summary_and_keyword_evidence",
    ),
    "entity_registry": StageProfile(
        stage_id="entity_registry",
        label="Entity Registry",
        short_label="Entities",
        input_summary="Header + text",
        control_key="entity_registry",
        output_contract="gathr_entity_registry_text",
    ),
    "clause_parser": StageProfile(
        stage_id="clause_parser",
        label="Clause Parser",
        short_label="Clauses",
        input_summary="Header + text",
        control_key="clause_parser",
        output_contract="verbatim_numbered_clauses",
    ),
    "occurrences_registry": StageProfile(
        stage_id="occurrences_registry",
        label="Occurrences Registry",
        short_label="Occurrences",
        requires=("clause_parser", "entity_registry"),
        input_summary="Header + parsed clauses + Entity Registry",
        control_key="key_events",
        output_contract="gathr_occurrences_registry_text",
    ),
    "tag_assembler": StageProfile(
        stage_id="tag_assembler",
        label="Tag Assembler",
        short_label="Full Tagset",
        requires=("clause_parser", "entity_registry", "occurrences_registry"),
        input_summary="Header + parsed clauses with Occurrences Registry tags + Entity Registry",
        control_key="full_tagset",
        output_contract="gathr_assembled_clause_tagset_text",
    ),
    "key_narrative": StageProfile(
        stage_id="key_narrative",
        label="Key Narrative Tagger",
        short_label="Key Narrative",
        requires=("summary_keywords", "entity_registry", "clause_parser", "occurrences_registry"),
        input_summary="Future: Summary & Keyword + Entity Registry + Clause Parser + Occurrences Registry",
        future=True,
        output_contract="future_key_narrative_text",
    ),
}


STAGE_CHECKER_NOTES: dict[str, str] = {
    "summary_keywords": "Future checker can compare summary/keyword coverage against document text.",
    "entity_registry": "Future verifier checks headword, ID, form, syntax, and fuzzy candidate matches.",
    "clause_parser": "Future verifier checks clause boundaries, numbering, and source-text coverage.",
    "occurrences_registry": "Future verifier checks selected headwords match the database or carry NEW-IDs.",
    "tag_assembler": "Future verifier checks no invented tags appear and final tag syntax is valid.",
    "key_narrative": "Future checker is blocked until Chatbot 6 / Key Narrative Tagger is written.",
}


PIPELINE_ORDER = (
    "document",
    "summary_keywords",
    "entity_registry",
    "clause_parser",
    "occurrences_registry",
    "tag_assembler",
    "key_narrative",
    "review",
    "accept_extract",
)


WORKFLOW_STAGE_ORDER = tuple(stage_id for stage_id in PIPELINE_ORDER if stage_id in STAGE_PROFILES)


TAGGING_CONTROLS = (
    {
        "key": "summary_keywords",
        "label": "Summary & Keyword",
        "stage_id": "summary_keywords",
        "description": "Summary and keywords from one chatbot stage.",
    },
    {
        "key": "entity_registry",
        "label": "Entity Registry",
        "stage_id": "entity_registry",
        "description": "People, places, things, concepts, and NEW-ID candidates.",
    },
    {
        "key": "clause_parser",
        "label": "Clause Parser",
        "stage_id": "clause_parser",
        "description": "Parsed clauses used by later chatbot stages.",
    },
    {
        "key": "key_events",
        "label": "Key Events / Occurrences",
        "stage_id": "occurrences_registry",
        "description": "Occurrences Registry-style event and occurrence outputs.",
    },
    {
        "key": "full_tagset",
        "label": "Full Tagset / Tag Assembler",
        "stage_id": "tag_assembler",
        "description": "Assembled full tag set placeholder.",
    },
)


DEFAULT_TOGGLE_KEYS = {"summary_keywords", "entity_registry", "clause_parser", "key_events"}
TOGGLE_KEYS = tuple(control["key"] for control in TAGGING_CONTROLS)


def selected_stage_ids(selected_toggles: set[str]) -> set[str]:
    stages: set[str] = set()
    for control in TAGGING_CONTROLS:
        if control["key"] in selected_toggles:
            stages.add(str(control["stage_id"]))
    return stages


def ordered_stage_ids(selected_stages: set[str]) -> list[str]:
    return [stage_id for stage_id in WORKFLOW_STAGE_ORDER if stage_id in selected_stages]


def dependency_closure(selected_stages: set[str]) -> set[str]:
    """Return the smallest public-stage set required by the user's selection."""
    unknown = sorted(set(selected_stages) - set(STAGE_PROFILES))
    if unknown:
        raise ValueError(f"Unknown workflow stages: {', '.join(unknown)}")
    expanded = set(selected_stages)
    pending = list(selected_stages)
    while pending:
        stage_id = pending.pop()
        for required in STAGE_PROFILES[stage_id].requires:
            if required not in expanded:
                expanded.add(required)
                pending.append(required)
    return expanded


def dependency_warnings(selected_stages: set[str]) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    for stage_id in selected_stages:
        profile = STAGE_PROFILES.get(stage_id)
        if not profile:
            continue
        missing = [required for required in profile.requires if required not in selected_stages]
        if missing:
            warnings.append(
                {
                    "stage_id": stage_id,
                    "label": profile.label,
                    "missing": [STAGE_PROFILES[item].label for item in missing],
                    "message": (
                        f"{profile.label} normally requires "
                        f"{', '.join(STAGE_PROFILES[item].label for item in missing)} outputs."
                    ),
                }
            )
        if profile.future:
            warnings.append(
                {
                    "stage_id": stage_id,
                    "label": profile.label,
                    "missing": [],
                    "message": f"{profile.label} is a future module and has not yet been written.",
                }
            )
    return warnings


def missing_required_stages(stage_id: str, available_stage_ids: set[str]) -> list[str]:
    profile = STAGE_PROFILES.get(stage_id)
    if not profile:
        return []
    return [required for required in profile.requires if required not in available_stage_ids]


def stage_labels(stage_ids: list[str] | tuple[str, ...]) -> list[str]:
    return [STAGE_PROFILES[stage_id].label for stage_id in stage_ids if stage_id in STAGE_PROFILES]


def stage_contract(stage_id: str) -> dict[str, object]:
    profile = STAGE_PROFILES[stage_id]
    return {
        "contract_version": STAGE_CONTRACT_VERSION,
        "stage_id": profile.stage_id,
        "label": profile.label,
        "short_label": profile.short_label,
        "requires": list(profile.requires),
        "input_summary": profile.input_summary,
        "output_contract": profile.output_contract,
        "allowed_providers": list(profile.allowed_providers),
        "future": profile.future,
    }


def all_stage_contracts() -> list[dict[str, object]]:
    return [stage_contract(stage_id) for stage_id in WORKFLOW_STAGE_ORDER]


def control_context(selected_toggles: set[str]) -> list[dict[str, object]]:
    return [
        {
            **control,
            "checked": control["key"] in selected_toggles,
        }
        for control in TAGGING_CONTROLS
    ]


def pipeline_context(
    *,
    selected_stages: set[str],
    has_document: bool,
    review_summary: dict[str, int],
    stage_statuses: dict[str, str] | None = None,
    active_stage_id: str = "",
) -> list[dict[str, object]]:
    warnings_by_stage = {warning["stage_id"] for warning in dependency_warnings(selected_stages)}
    status_by_stage = stage_statuses or {}
    rows: list[dict[str, object]] = []
    for stage_id in PIPELINE_ORDER:
        if stage_id == "document":
            rows.append(
                {
                    "stage_id": stage_id,
                    "label": "Document",
                    "status": "done" if has_document else "idle",
                    "status_label": "selected" if has_document else "choose",
                    "detail": "Document selected or uploaded.",
                    "is_active": False,
                }
            )
            continue
        if stage_id == "review":
            touched = review_summary.get("approved", 0) + review_summary.get("rejected", 0) + review_summary.get("edited", 0)
            rows.append(
                {
                    "stage_id": stage_id,
                    "label": "Review",
                    "status": "review" if active_stage_id == "final_review" or touched else "idle",
                    "status_label": "current" if active_stage_id == "final_review" else "in review" if touched else "pending",
                    "detail": "Human review of proposed IDs and stage outputs.",
                    "is_active": active_stage_id == "final_review",
                }
            )
            continue
        if stage_id == "accept_extract":
            rows.append(
                {
                    "stage_id": stage_id,
                    "label": "Accept & Extract",
                    "status": "future",
                    "status_label": "future DB write",
                    "detail": "Future PostgreSQL extraction stage; not active in this prototype.",
                    "is_active": False,
                }
            )
            continue

        profile = STAGE_PROFILES[stage_id]
        persisted_status = status_by_stage.get(stage_id, "")
        if profile.future:
            status = "future"
            status_label = "future"
        elif persisted_status == "accepted":
            status = "done"
            status_label = "accepted"
        elif persisted_status == "checking":
            status = "review"
            status_label = "checking"
        elif persisted_status == "needs_rerun":
            status = "warning"
            status_label = "edit requested"
        elif persisted_status == "blocked":
            status = "warning"
            status_label = "blocked"
        elif stage_id in warnings_by_stage:
            status = "warning"
            status_label = "needs prerequisites"
        elif stage_id in selected_stages:
            status = "selected"
            status_label = "current" if stage_id == active_stage_id else "loaded" if persisted_status == "loaded" else "selected"
        else:
            status = "available"
            status_label = "loaded" if persisted_status == "loaded" else "available"
        rows.append(
            {
                "stage_id": stage_id,
                "label": profile.label,
                "status": status,
                "status_label": status_label,
                "detail": profile.input_summary,
                "is_active": stage_id == active_stage_id,
            }
        )
    return rows
