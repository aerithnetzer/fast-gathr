from __future__ import annotations

import json
import os
import re
import tempfile
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import numpy as np


CONTRACT_VERSION = "event-headword-human-review-v1"
SIMILARITY_CONTRACT_VERSION = "event-headword-proposal-similarity-v1"

STATE_LLM_SELECTED = "llm_selected_candidate"
STATE_LLM_NONE_FIT = "llm_none_fit"
STATE_EDITING = "editing_headword"
STATE_SIMILARITY_REVIEW = "proposal_similarity_review"
STATE_ACCEPTED = "accepted_existing_headword"
STATE_PROVISIONAL = "provisional_headword_pending_review"

TERMINAL_STATES = {STATE_ACCEPTED, STATE_PROVISIONAL}

ACTION_ACCEPT = "accept"
ACTION_EDIT = "edit"
ACTION_REJECT = "reject"
ACTION_VIEW_TOP_K = "view_top_k"
ACTION_CHOOSE_TOP_K = "choose_top_k"
ACTION_SUBMIT_PROPOSAL = "submit_proposal"
ACTION_CHOOSE_SIMILAR = "choose_similar_existing"
ACTION_CONFIRM_PROPOSAL = "confirm_new_proposal"


class ReviewWorkflowError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RevisionConflictError(ReviewWorkflowError):
    pass


@dataclass(frozen=True)
class AuthorityHeadword:
    event_id: str
    headword: str
    definition: str = ""
    llm_example: str = ""
    authority_version: str = ""


class ProposalSimilarityBackend(Protocol):
    def check(
        self,
        *,
        proposed_headword: str,
        definition_hint: str,
        top_k: int,
    ) -> dict[str, Any]: ...


class ReviewRepository(Protocol):
    """Persistence boundary shared by JSON, PostgreSQL, and external DB adapters."""

    def initialize(self, item: dict[str, Any], *, overwrite: bool = False) -> None: ...

    def get(self, item_id: str) -> dict[str, Any]: ...

    def save(self, item: dict[str, Any], *, expected_revision: int) -> None: ...


class DenseProposalSimilarityBackend:
    """Compare a proposal with the complete controlled Event authority."""

    def __init__(
        self,
        *,
        authority: list[AuthorityHeadword],
        encoder: Any,
        model_name: str,
        authority_hash: str,
    ) -> None:
        if not authority:
            raise ReviewWorkflowError("authority_required", "Event authority is empty")
        self.authority = authority
        self.encoder = encoder
        self.model_name = model_name
        self.authority_hash = authority_hash
        texts = [_authority_text(row) for row in authority]
        self.authority_vectors = _normalized_matrix(
            _encode_documents(encoder, texts), label="authority headwords"
        )

    def check(
        self,
        *,
        proposed_headword: str,
        definition_hint: str,
        top_k: int,
    ) -> dict[str, Any]:
        query = _proposal_text(proposed_headword, definition_hint)
        query_vector = _normalized_matrix(
            _encode_queries(self.encoder, [query]), label="proposed headword"
        )[0]
        scores = self.authority_vectors @ query_vector
        normalized = normalize_headword(proposed_headword)
        indexes = sorted(
            range(len(self.authority)),
            key=lambda index: (-float(scores[index]), self.authority[index].event_id),
        )[: max(1, min(top_k, len(self.authority)))]
        matches = []
        for rank, index in enumerate(indexes, start=1):
            row = self.authority[index]
            lexical = _lexical_relationship(normalized, normalize_headword(row.headword))
            matches.append(
                {
                    "rank": rank,
                    "event_id": row.event_id,
                    "headword": row.headword,
                    "definition": row.definition,
                    "llm_example": row.llm_example,
                    "cosine_similarity": round(float(scores[index]), 8),
                    "lexical_relationship": lexical,
                    "authority_version": row.authority_version,
                }
            )
        exact = [
            {
                "event_id": row.event_id,
                "headword": row.headword,
                "relationship": _lexical_relationship(
                    normalized, normalize_headword(row.headword)
                ),
            }
            for row in self.authority
            if _lexical_relationship(normalized, normalize_headword(row.headword))
            in {"normalized_exact", "singular_plural_variant"}
        ]
        return {
            "contract_version": SIMILARITY_CONTRACT_VERSION,
            "status": "completed",
            "query": {
                "proposed_headword": proposed_headword,
                "definition_hint": definition_hint,
                "text": query,
            },
            "encoder_model": self.model_name,
            "authority_hash": self.authority_hash,
            "authority_count": len(self.authority),
            "embedding_dim": int(self.authority_vectors.shape[1]),
            "exact_or_morphological_matches": exact,
            "matches": matches,
            "user_override_permitted": True,
            "official_event_list_modified": False,
        }


class EvidenceProposalSimilarityBackend:
    """Use externally computed encoder evidence while preserving the workflow gate."""

    def __init__(self, evidence: dict[str, Any]) -> None:
        self.evidence = deepcopy(evidence)

    def check(
        self,
        *,
        proposed_headword: str,
        definition_hint: str,
        top_k: int,
    ) -> dict[str, Any]:
        evidence = deepcopy(self.evidence)
        query = evidence.get("query") or {}
        if evidence.get("contract_version") != SIMILARITY_CONTRACT_VERSION:
            raise ReviewWorkflowError(
                "similarity_contract_invalid", "Similarity evidence contract is invalid"
            )
        if evidence.get("status") != "completed":
            raise ReviewWorkflowError(
                "similarity_incomplete", "Similarity evidence is not completed"
            )
        if normalize_headword(query.get("proposed_headword")) != normalize_headword(
            proposed_headword
        ):
            raise ReviewWorkflowError(
                "similarity_query_mismatch",
                "Similarity evidence was generated for a different proposed headword",
            )
        evidence["matches"] = list(evidence.get("matches") or [])[:top_k]
        return evidence


class JsonReviewRepository:
    """Atomic JSON persistence adapter for the pre-database workflow."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def initialize(self, item: dict[str, Any], *, overwrite: bool = False) -> None:
        payload = self._read()
        item_id = str(item.get("item_id") or "")
        if not item_id:
            raise ReviewWorkflowError("item_id_required", "Review item_id is required")
        if item_id in payload["items"] and not overwrite:
            raise ReviewWorkflowError("item_exists", f"Review item {item_id} already exists")
        payload["items"][item_id] = deepcopy(item)
        self._write(payload)

    def get(self, item_id: str) -> dict[str, Any]:
        item = self._read()["items"].get(str(item_id))
        if not isinstance(item, dict):
            raise ReviewWorkflowError("item_not_found", f"Review item {item_id} not found")
        return deepcopy(item)

    def all(self) -> list[dict[str, Any]]:
        return [deepcopy(item) for item in self._read()["items"].values()]

    def replace_all(self, items: list[dict[str, Any]]) -> None:
        payload = self._read()
        replacement = {str(item.get("item_id") or ""): deepcopy(item) for item in items}
        if not all(replacement) or set(replacement) != set(payload["items"]):
            raise ReviewWorkflowError(
                "bulk_item_set_mismatch", "Bulk review must preserve the complete item identity set"
            )
        payload["items"] = replacement
        self._write(payload)

    def save(self, item: dict[str, Any], *, expected_revision: int) -> None:
        payload = self._read()
        item_id = str(item.get("item_id") or "")
        current = payload["items"].get(item_id)
        if not isinstance(current, dict):
            raise ReviewWorkflowError("item_not_found", f"Review item {item_id} not found")
        actual_revision = int(current.get("revision") or 0)
        if actual_revision != expected_revision:
            raise RevisionConflictError(
                "revision_conflict",
                f"Expected revision {expected_revision}, found {actual_revision}",
            )
        payload["items"][item_id] = deepcopy(item)
        self._write(payload)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"contract_version": CONTRACT_VERSION, "items": {}}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("contract_version") != CONTRACT_VERSION:
            raise ReviewWorkflowError("store_contract_invalid", "Review store contract is invalid")
        if not isinstance(payload.get("items"), dict):
            raise ReviewWorkflowError("store_invalid", "Review store items must be an object")
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def create_review_item(
    *,
    item_id: str,
    document_id: str,
    clause_id: str,
    event_cut_id: str,
    event_cut_text: str,
    candidates: list[dict[str, Any]],
    chooser_output: dict[str, Any],
    authority_version: str = "",
) -> dict[str, Any]:
    _validate_candidates(candidates)
    decision = str(chooser_output.get("decision") or "")
    if decision == "choose_candidate":
        selected = _resolve_candidate(candidates, chooser_output.get("selected_candidate") or {})
        state = STATE_LLM_SELECTED
    elif decision == "none_of_these_fit":
        selected = None
        state = STATE_LLM_NONE_FIT
    else:
        raise ReviewWorkflowError(
            "chooser_decision_invalid", "Chooser decision must select a candidate or none-fit"
        )
    item = {
        "contract_version": CONTRACT_VERSION,
        "item_id": str(item_id),
        "document_id": str(document_id),
        "clause_id": str(clause_id),
        "event_cut": {"event_cut_id": str(event_cut_id), "text": str(event_cut_text)},
        "authority_version": str(authority_version),
        "candidates": deepcopy(candidates),
        "chooser_output": deepcopy(chooser_output),
        "chooser_selected_candidate": deepcopy(selected),
        "initial_state": state,
        "state": state,
        "revision": 0,
        "edit_context": None,
        "proposal": None,
        "similarity_check": None,
        "assignment": None,
        "provisional_headword": None,
        "audit_log": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    item["allowed_actions"] = allowed_actions(item)
    return item


def allowed_actions(item: dict[str, Any]) -> dict[str, Any]:
    state = str(item.get("state") or "")
    if state == STATE_LLM_SELECTED:
        primary = {
            ACTION_ACCEPT: _control(True, True, "Accept"),
            ACTION_EDIT: _control(True, True, "Edit"),
            ACTION_REJECT: _control(True, True, "Reject"),
        }
        secondary = {ACTION_VIEW_TOP_K: _control(True, True, "View top-k")}
    elif state == STATE_LLM_NONE_FIT:
        primary = {
            ACTION_ACCEPT: _control(True, False, "Accept", "No LLM candidate exists"),
            ACTION_EDIT: _control(True, True, "Edit / propose"),
            ACTION_REJECT: _control(
                True, False, "Reject", "The LLM already rejected all supplied candidates"
            ),
        }
        secondary = {ACTION_VIEW_TOP_K: _control(True, True, "View top-k")}
    elif state == STATE_EDITING:
        primary = {}
        secondary = {
            ACTION_SUBMIT_PROPOSAL: _control(True, True, "Check proposed headword"),
            ACTION_VIEW_TOP_K: _control(True, True, "View top-k"),
            ACTION_CHOOSE_TOP_K: _control(True, True, "Choose existing headword"),
        }
    elif state == STATE_SIMILARITY_REVIEW:
        primary = {}
        secondary = {
            ACTION_CHOOSE_SIMILAR: _control(True, True, "Use similar existing headword"),
            ACTION_CONFIRM_PROPOSAL: _control(True, True, "Keep my new proposal"),
            ACTION_VIEW_TOP_K: _control(True, True, "View top-k"),
        }
    else:
        primary = {}
        secondary = {ACTION_VIEW_TOP_K: _control(True, True, "View top-k")}
    return {
        "state": state,
        "primary": primary,
        "secondary": secondary,
        "top_k_available": bool(item.get("candidates")),
        "is_terminal": state in TERMINAL_STATES,
        "edit_input": deepcopy(item.get("edit_context")),
    }


def apply_review_action(
    item: dict[str, Any],
    *,
    action: str,
    actor: str,
    expected_revision: int,
    action_id: str | None = None,
    candidate_rank: int | None = None,
    proposed_headword: str = "",
    definition_hint: str = "",
    reviewer_note: str = "",
    similarity_backend: ProposalSimilarityBackend | None = None,
    similarity_top_k: int = 10,
) -> dict[str, Any]:
    item = deepcopy(item)
    _validate_item(item)
    actual_revision = int(item.get("revision") or 0)
    if actual_revision != int(expected_revision):
        raise RevisionConflictError(
            "revision_conflict",
            f"Expected revision {expected_revision}, found {actual_revision}",
        )
    action_id = str(action_id or uuid4())
    if any(event.get("action_id") == action_id for event in item.get("audit_log") or []):
        raise ReviewWorkflowError("duplicate_action", f"Action {action_id} was already applied")
    state = str(item["state"])
    enabled = _enabled_actions(allowed_actions(item))
    if action not in enabled:
        raise ReviewWorkflowError(
            "action_not_allowed", f"Action {action} is disabled in state {state}"
        )
    if action == ACTION_VIEW_TOP_K:
        return item

    previous_state = state
    assignment = None
    proposal = None
    if action == ACTION_ACCEPT:
        selected = item.get("chooser_selected_candidate")
        if not isinstance(selected, dict):
            raise ReviewWorkflowError("candidate_required", "No LLM candidate is available")
        assignment = _assignment(item, selected, actor=actor, source="llm_accept")
        item["assignment"] = assignment
        item["state"] = STATE_ACCEPTED
    elif action == ACTION_EDIT:
        selected = item.get("chooser_selected_candidate") or {}
        item["edit_context"] = {
            "origin": "edit" if previous_state == STATE_LLM_SELECTED else "none_fit_edit",
            "prefill_headword": str(selected.get("headword") or ""),
            "prefill_event_id": str(selected.get("event_id") or ""),
            "input_is_blank": not bool(selected.get("headword")),
        }
        item["state"] = STATE_EDITING
    elif action == ACTION_REJECT:
        item["edit_context"] = {
            "origin": "reject",
            "prefill_headword": "",
            "prefill_event_id": "",
            "input_is_blank": True,
        }
        item["state"] = STATE_EDITING
    elif action in {ACTION_CHOOSE_TOP_K, ACTION_CHOOSE_SIMILAR}:
        if candidate_rank is None:
            raise ReviewWorkflowError("candidate_rank_required", "Candidate rank is required")
        source_candidates = (
            item.get("candidates")
            if action == ACTION_CHOOSE_TOP_K
            else (item.get("similarity_check") or {}).get("matches")
        )
        selected = _candidate_by_rank(source_candidates or [], candidate_rank)
        assignment = _assignment(
            item,
            selected,
            actor=actor,
            source="top_k_override" if action == ACTION_CHOOSE_TOP_K else "proposal_similarity_match",
        )
        item["assignment"] = assignment
        item["state"] = STATE_ACCEPTED
    elif action == ACTION_SUBMIT_PROPOSAL:
        headword = clean_required(proposed_headword, "proposed_headword_required")
        if similarity_backend is None:
            raise ReviewWorkflowError(
                "encoder_similarity_required",
                "A completed encoder similarity check is required before a proposal can continue",
            )
        proposal = {
            "headword": headword,
            "definition_hint": str(definition_hint or "").strip(),
            "reviewer_note": str(reviewer_note or "").strip(),
            "origin": (item.get("edit_context") or {}).get("origin", ""),
        }
        similarity = similarity_backend.check(
            proposed_headword=headword,
            definition_hint=proposal["definition_hint"],
            top_k=similarity_top_k,
        )
        _validate_similarity(similarity, headword)
        item["proposal"] = proposal
        item["similarity_check"] = similarity
        item["state"] = STATE_SIMILARITY_REVIEW
    elif action == ACTION_CONFIRM_PROPOSAL:
        proposal = item.get("proposal")
        similarity = item.get("similarity_check")
        if not isinstance(proposal, dict) or not isinstance(similarity, dict):
            raise ReviewWorkflowError(
                "similarity_required", "Proposal similarity must complete before confirmation"
            )
        provisional_event_id = f"NEW-E-{int(uuid4().hex[:10], 16) % 100000000:08d}"
        item["provisional_headword"] = {
            "proposal_id": f"event-headword-proposal-{uuid4().hex}",
            "event_id": provisional_event_id,
            **deepcopy(proposal),
            "status": "pending_shared_vocabulary_review",
            "submitted_by": actor,
            "submitted_at": _now(),
            "similarity_contract_version": similarity.get("contract_version"),
            "similarity_authority_hash": similarity.get("authority_hash", ""),
            "official_event_list_modified": False,
        }
        assignment = _assignment(
            item,
            {"event_id": provisional_event_id, "headword": proposal["headword"], "rank": None},
            actor=actor,
            source="user_confirmed_provisional",
        )
        item["assignment"] = assignment
        item["state"] = STATE_PROVISIONAL
    else:
        raise ReviewWorkflowError("action_unknown", f"Unknown action {action}")

    item["revision"] = actual_revision + 1
    item["updated_at"] = _now()
    item.setdefault("audit_log", []).append(
        {
            "audit_id": f"event-headword-audit-{uuid4().hex}",
            "action_id": action_id,
            "timestamp": item["updated_at"],
            "actor": clean_required(actor, "actor_required"),
            "action": action,
            "previous_state": previous_state,
            "next_state": item["state"],
            "revision_before": actual_revision,
            "revision_after": item["revision"],
            "item_id": item["item_id"],
            "event_cut_id": item["event_cut"]["event_cut_id"],
            "selected_event_id": str((assignment or {}).get("event_id") or ""),
            "selected_headword": str((assignment or proposal or {}).get("headword") or ""),
            "reviewer_note": str(reviewer_note or "").strip(),
        }
    )
    item["allowed_actions"] = allowed_actions(item)
    return item


def normalize_headword(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
    return " ".join(text.split())


def accept_remaining_items(
    items: list[dict[str, Any]], *, actor: str, action_id_prefix: str | None = None
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Accept untouched LLM selections without overwriting edits, rejects, or none-fit rows."""
    updated_items = []
    accepted = 0
    preserved = 0
    prefix = str(action_id_prefix or uuid4())
    for item in items:
        if str(item.get("state") or "") == STATE_LLM_SELECTED:
            updated_items.append(
                apply_review_action(
                    item,
                    action=ACTION_ACCEPT,
                    actor=actor,
                    expected_revision=int(item.get("revision") or 0),
                    action_id=f"{prefix}:{item.get('item_id')}",
                    reviewer_note="Accepted by bulk accept-remaining action.",
                )
            )
            accepted += 1
        else:
            updated_items.append(deepcopy(item))
            preserved += 1
    return updated_items, {"accepted": accepted, "preserved": preserved, "total": len(items)}


def clean_required(value: Any, code: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ReviewWorkflowError(code, code.replace("_", " "))
    return text


def _control(visible: bool, enabled: bool, label: str, disabled_reason: str = "") -> dict[str, Any]:
    return {
        "visible": visible,
        "enabled": enabled,
        "label": label,
        "disabled_reason": disabled_reason,
        "cursor": "pointer" if enabled else "not-allowed",
    }


def _enabled_actions(controls: dict[str, Any]) -> set[str]:
    return {
        name
        for group in (controls.get("primary") or {}, controls.get("secondary") or {})
        for name, control in group.items()
        if control.get("enabled")
    }


def _validate_candidates(candidates: list[dict[str, Any]]) -> None:
    if not candidates:
        raise ReviewWorkflowError("candidates_required", "Top-k candidates are required")
    seen = set()
    for expected_rank, candidate in enumerate(candidates, start=1):
        key = (str(candidate.get("event_id") or ""), str(candidate.get("headword") or ""))
        if not all(key) or key in seen:
            raise ReviewWorkflowError(
                "candidate_identity_invalid", "Candidates require unique Event ID/headword pairs"
            )
        if int(candidate.get("rank") or 0) != expected_rank:
            raise ReviewWorkflowError("candidate_rank_invalid", "Candidate ranks must be contiguous")
        seen.add(key)


def _validate_item(item: dict[str, Any]) -> None:
    if item.get("contract_version") != CONTRACT_VERSION:
        raise ReviewWorkflowError("item_contract_invalid", "Review item contract is invalid")
    _validate_candidates(list(item.get("candidates") or []))


def _resolve_candidate(candidates: list[dict[str, Any]], selected: dict[str, Any]) -> dict[str, Any]:
    for candidate in candidates:
        if (
            candidate.get("event_id") == selected.get("event_id")
            and candidate.get("headword") == selected.get("headword")
            and int(candidate.get("rank") or 0) == int(selected.get("rank") or 0)
        ):
            return deepcopy(candidate)
    raise ReviewWorkflowError(
        "chooser_candidate_invalid", "LLM selection is not an exact supplied candidate"
    )


def _candidate_by_rank(candidates: list[dict[str, Any]], rank: int) -> dict[str, Any]:
    for candidate in candidates:
        if int(candidate.get("rank") or 0) == int(rank):
            return deepcopy(candidate)
    raise ReviewWorkflowError("candidate_not_found", f"Candidate rank {rank} is unavailable")


def _assignment(
    item: dict[str, Any], candidate: dict[str, Any], *, actor: str, source: str
) -> dict[str, Any]:
    return {
        "assignment_id": f"event-assignment-{uuid4().hex}",
        "event_cut_id": item["event_cut"]["event_cut_id"],
        "event_id": str(candidate.get("event_id") or ""),
        "headword": str(candidate.get("headword") or ""),
        "candidate_rank": candidate.get("rank"),
        "authority_version": item.get("authority_version", ""),
        "status": "accepted",
        "source": source,
        "accepted_by": clean_required(actor, "actor_required"),
        "accepted_at": _now(),
    }


def _validate_similarity(similarity: dict[str, Any], proposed_headword: str) -> None:
    if similarity.get("contract_version") != SIMILARITY_CONTRACT_VERSION:
        raise ReviewWorkflowError("similarity_contract_invalid", "Similarity contract is invalid")
    if similarity.get("status") != "completed":
        raise ReviewWorkflowError("similarity_incomplete", "Similarity check did not complete")
    query = similarity.get("query") or {}
    if normalize_headword(query.get("proposed_headword")) != normalize_headword(proposed_headword):
        raise ReviewWorkflowError("similarity_query_mismatch", "Similarity query does not match proposal")
    if not similarity.get("encoder_model") or not similarity.get("authority_hash"):
        raise ReviewWorkflowError(
            "similarity_provenance_incomplete", "Encoder and authority provenance are required"
        )
    if not isinstance(similarity.get("matches"), list):
        raise ReviewWorkflowError("similarity_matches_invalid", "Similarity matches must be a list")


def _authority_text(row: AuthorityHeadword) -> str:
    return " ".join(
        part.strip() for part in (row.headword, row.definition, row.llm_example) if part.strip()
    )


def _proposal_text(headword: str, definition_hint: str) -> str:
    return " ".join(part.strip() for part in (headword, definition_hint) if part.strip())


def _encode_documents(encoder: Any, texts: list[str]) -> Any:
    method = getattr(encoder, "encode_documents", None)
    return method(texts) if callable(method) else encoder.encode(texts)


def _encode_queries(encoder: Any, texts: list[str]) -> Any:
    method = getattr(encoder, "encode_queries", None)
    return method(texts) if callable(method) else encoder.encode(texts)


def _normalized_matrix(value: Any, *, label: str) -> np.ndarray:
    matrix = np.asarray(value, dtype=np.float32)
    if matrix.ndim != 2 or not matrix.shape[0] or not matrix.shape[1]:
        raise ReviewWorkflowError("embedding_shape_invalid", f"{label} embeddings are invalid")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if not np.all(np.isfinite(matrix)) or np.any(norms <= 0):
        raise ReviewWorkflowError("embedding_values_invalid", f"{label} embeddings are invalid")
    return matrix / norms


def _lexical_relationship(left: str, right: str) -> str:
    if left == right:
        return "normalized_exact"
    if left.rstrip("s") == right.rstrip("s"):
        return "singular_plural_variant"
    return "none"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
