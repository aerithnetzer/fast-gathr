from __future__ import annotations

import hashlib
import json
import gc
from pathlib import Path
from typing import Any, Callable

import numpy as np

from tagger.models import StageOutput

from .event_lookup import EventCandidateTextBuilder
from .event_lookup_contract import load_event_registry_snapshot
from .eventcut_extraction import INTERNAL_CONTRACT_VERSION


DENSE_FROM_CLAUSES_CONTRACT = "event-lookup-dense-from-clauses-v1"
DENSE_BACKEND_NAME = "dense_embedding_cosine"
DENSE_SCORE_TYPE = "dense_cosine"
DEFAULT_ENCODER_MODEL = "BAAI/bge-large-en-v1.5"
FALLBACK_ENCODER_MODEL = "BAAI/bge-base-en-v1.5"
INDEX_TEXT_CONTRACT = "event-authority-headword-definition-vector-example-v1"


class DenseEventLookupError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SentenceTransformerDenseEncoder:
    """Thin fail-closed adapter around a real sentence-transformers encoder."""

    def __init__(self, model_name: str, device: str = "") -> None:
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(model_name, device=device or None)
        except Exception as exc:
            raise DenseEventLookupError(
                "dense_encoder_load_failed",
                f"Could not load dense encoder {model_name!r}: {type(exc).__name__}: {exc}",
            ) from exc
        self.model_name = model_name
        self.device = str(getattr(self._model, "device", device or "unknown"))

    def encode(self, texts: list[str]) -> np.ndarray:
        try:
            vectors = self._model.encode(
                texts,
                batch_size=32,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=False,
            )
        except Exception as exc:
            raise DenseEventLookupError(
                "dense_encoder_encode_failed",
                f"Dense encoding failed for {self.model_name!r}: {type(exc).__name__}: {exc}",
            ) from exc
        return np.asarray(vectors, dtype=np.float32)


class DenseEventLookupFromClausesService:
    def __init__(
        self,
        *,
        encoder_factory: Callable[[str, str], Any] | None = None,
        workbook_path: Path | None = None,
    ) -> None:
        self.encoder_factory = encoder_factory or SentenceTransformerDenseEncoder
        self.workbook_path = workbook_path

    def build(
        self,
        *,
        clause_output: StageOutput,
        clauses: list[dict[str, Any]],
        top_k: int = 20,
        encoder_model: str = DEFAULT_ENCODER_MODEL,
        encoder_device: str = "",
    ) -> dict[str, Any]:
        if (
            clause_output.stage != StageOutput.Stage.CLAUSE_PARSER
            or clause_output.status != StageOutput.Status.ACCEPTED
        ):
            raise DenseEventLookupError(
                "accepted_clause_parser_required",
                "Dense Event lookup requires an accepted Clause Parser StageOutput.",
            )
        if not clauses:
            raise DenseEventLookupError(
                "selected_clauses_required", "Select at least one Clause for dense lookup."
            )
        if top_k <= 0:
            raise DenseEventLookupError("invalid_top_k", "--top-k must be positive.")

        eventcut_output, event_cuts = _validated_eventcuts_for_clauses(
            clause_output=clause_output,
            clauses=clauses,
        )
        snapshot = load_event_registry_snapshot(self.workbook_path)
        if not snapshot.entries:
            raise DenseEventLookupError(
                "event_authority_empty", "The authoritative Event workbook has no valid rows."
            )
        text_builder = EventCandidateTextBuilder()
        authority_texts = [text_builder.build(entry) for entry in snapshot.entries]
        query_texts = [str(item["event_cut_text"]) for item in event_cuts]
        model_candidates = _encoder_model_candidates(encoder_model)
        attempts: list[dict[str, Any]] = []
        encoder = None
        authority_vectors = None
        query_vectors = None
        for model_name in model_candidates:
            candidate_encoder = None
            try:
                candidate_encoder = self.encoder_factory(model_name, encoder_device)
                candidate_authority = _normalized_matrix(
                    candidate_encoder.encode(authority_texts),
                    label="authority index",
                )
                candidate_queries = _normalized_matrix(
                    candidate_encoder.encode(query_texts),
                    label="EventCut queries",
                )
                if candidate_authority.shape[1] != candidate_queries.shape[1]:
                    raise DenseEventLookupError(
                        "embedding_dimension_mismatch",
                        "Authority and EventCut embeddings have different dimensions.",
                    )
                encoder = candidate_encoder
                authority_vectors = candidate_authority
                query_vectors = candidate_queries
                attempts.append({"encoder_model": model_name, "status": "loaded_and_encoded"})
                break
            except Exception as exc:
                attempts.append(
                    {
                        "encoder_model": model_name,
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                candidate_encoder = None
                _release_failed_encoder()
        if encoder is None or authority_vectors is None or query_vectors is None:
            detail = "; ".join(
                f"{item['encoder_model']}: {item.get('error', 'failed')}"
                for item in attempts
            )
            raise DenseEventLookupError(
                "dense_encoder_unavailable",
                "No dense encoder could be loaded and executed. " + detail,
            )

        actual_model = str(getattr(encoder, "model_name", model_candidates[-1]))
        actual_device = str(getattr(encoder, "device", encoder_device or "unknown"))
        embedding_dim = int(authority_vectors.shape[1])
        index_hash = _index_hash(
            registry_hash=snapshot.resource_hash,
            encoder_model=actual_model,
            entries=snapshot.entries,
            authority_texts=authority_texts,
            vectors=authority_vectors,
        )
        per_cut: list[dict[str, Any]] = []
        candidate_limit = min(top_k, len(snapshot.entries))
        for event_cut, query_vector in zip(event_cuts, query_vectors):
            scores = authority_vectors @ query_vector
            ranked_indexes = sorted(
                range(len(snapshot.entries)),
                key=lambda index: (-float(scores[index]), snapshot.entries[index].event_id),
            )[:candidate_limit]
            candidates = []
            for rank, index in enumerate(ranked_indexes, start=1):
                entry = snapshot.entries[index]
                candidates.append(
                    {
                        "rank": rank,
                        "event_id": entry.event_id,
                        "headword": entry.headword,
                        "definition": entry.definition,
                        "vector_example": entry.vector_examples,
                        "llm_example": entry.llm_examples,
                        "score": round(float(scores[index]), 8),
                        "score_type": DENSE_SCORE_TYPE,
                        "source_workbook": Path(snapshot.resource_path).name,
                        "source_sheet": snapshot.workbook_sheet,
                        "source_row": entry.source_row,
                        "provenance": {
                            "registry_hash": snapshot.resource_hash,
                            "registry_version": snapshot.registry_version,
                            "index_hash": index_hash,
                            "encoder_model": actual_model,
                        },
                    }
                )
            per_cut.append(
                {
                    key: event_cut.get(key)
                    for key in (
                        "event_cut_id",
                        "clause_id",
                        "trigger",
                        "event_cut_text",
                        "lookup_context_text",
                    )
                }
                | {"candidates": candidates}
            )

        selected_clause_ids = [str(item["clause_id"]) for item in clauses]
        return {
            "contract_version": DENSE_FROM_CLAUSES_CONTRACT,
            "source_clause_parser_stage_output_id": int(clause_output.pk),
            "source_eventcut_stage_output_id": int(eventcut_output.pk),
            "doc_id": clause_output.document.doc_id,
            "selected_clause_ids": selected_clause_ids,
            "requested_encoder_model": encoder_model,
            "encoder_model": actual_model,
            "encoder_device": actual_device,
            "encoder_fallback_used": actual_model != encoder_model,
            "encoder_attempts": attempts,
            "embedding_dim": embedding_dim,
            "registry_path": snapshot.resource_path,
            "registry_sheet": snapshot.workbook_sheet,
            "registry_hash": snapshot.resource_hash,
            "registry_version": snapshot.registry_version,
            "index_hash": index_hash,
            "index_text_contract": INDEX_TEXT_CONTRACT,
            "index_row_count": len(snapshot.entries),
            "backend_name": DENSE_BACKEND_NAME,
            "score_type": DENSE_SCORE_TYPE,
            "tfidf_used": False,
            "lexical_fallback_used": False,
            "top_k_requested": top_k,
            "event_cut_count": len(per_cut),
            "event_cuts": per_cut,
        }


def write_dense_lookup_package(package: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _validated_eventcuts_for_clauses(
    *, clause_output: StageOutput, clauses: list[dict[str, Any]]
) -> tuple[StageOutput, list[dict[str, Any]]]:
    eventcut_output = (
        StageOutput.objects.filter(
            document=clause_output.document,
            stage=StageOutput.Stage.EVENTCUT_EXTRACTION,
        )
        .order_by("-updated_at", "-id")
        .first()
    )
    if eventcut_output is None:
        raise DenseEventLookupError(
            "validated_eventcuts_required",
            "No internal EventCut StageOutput exists for this document.",
        )
    payload = dict(eventcut_output.payload or {})
    if (
        payload.get("contract_version") != INTERNAL_CONTRACT_VERSION
        or payload.get("internal_usable_for_lookup") is not True
    ):
        raise DenseEventLookupError(
            "validated_eventcuts_required",
            f"EventCut StageOutput {eventcut_output.pk} is not validated for lookup.",
        )
    if int(payload.get("source_clause_parser_stage_output_id") or 0) != int(
        clause_output.pk
    ):
        raise DenseEventLookupError(
            "eventcut_clause_source_mismatch",
            "The latest validated EventCuts were produced from a different Clause Parser output.",
        )

    selected_ids = [str(item["clause_id"]) for item in clauses]
    clause_map = {str(item["clause_id"]): item for item in clauses}
    selected_cuts: list[dict[str, Any]] = []
    stale_ids: set[str] = set()
    for cut in payload.get("parsed_event_cuts") or []:
        if not isinstance(cut, dict) or cut.get("valid") is not True:
            continue
        clause_id = str(cut.get("clause_id") or "")
        if clause_id not in clause_map:
            continue
        clause = clause_map[clause_id]
        if (
            cut.get("source_clause_parser_stage_output_id") != clause_output.pk
            or cut.get("clause_text_sha256") != clause.get("text_sha256")
            or str(cut.get("event_cut_text") or "") not in str(clause.get("text") or "")
        ):
            stale_ids.add(clause_id)
            continue
        selected_cuts.append(dict(cut))
    if stale_ids:
        raise DenseEventLookupError(
            "eventcut_source_validation_failed",
            "Validated EventCuts no longer match accepted Clause text for Clause IDs: "
            + ", ".join(sorted(stale_ids)),
        )
    available_ids = {str(item.get("clause_id") or "") for item in selected_cuts}
    missing = [clause_id for clause_id in selected_ids if clause_id not in available_ids]
    if missing:
        raise DenseEventLookupError(
            "missing_validated_eventcuts",
            "Selected Clause IDs have no validated internal EventCuts: "
            + ", ".join(missing),
        )
    sequence = {clause_id: index for index, clause_id in enumerate(selected_ids)}
    selected_cuts.sort(
        key=lambda item: (
            sequence[str(item["clause_id"])],
            int((item.get("source_offsets") or {}).get("start", 10**9)),
            str(item.get("event_cut_id") or ""),
        )
    )
    return eventcut_output, selected_cuts


def _encoder_model_candidates(requested: str) -> list[str]:
    cleaned = str(requested or "").strip() or DEFAULT_ENCODER_MODEL
    if cleaned == DEFAULT_ENCODER_MODEL:
        return [DEFAULT_ENCODER_MODEL, FALLBACK_ENCODER_MODEL]
    return [cleaned]


def _release_failed_encoder() -> None:
    """Release a failed large encoder before attempting the allowed base model."""
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        # Cleanup must not obscure the original, reported encoder failure.
        pass


def _normalized_matrix(vectors: Any, *, label: str) -> np.ndarray:
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    if matrix.ndim != 2 or not matrix.shape[0] or not matrix.shape[1]:
        raise DenseEventLookupError(
            "invalid_dense_embeddings", f"Dense {label} must be a non-empty 2D matrix."
        )
    if not np.isfinite(matrix).all():
        raise DenseEventLookupError(
            "invalid_dense_embeddings", f"Dense {label} contains non-finite values."
        )
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise DenseEventLookupError(
            "zero_dense_embedding", f"Dense {label} contains a zero vector."
        )
    return matrix / norms


def _index_hash(
    *, registry_hash: str, encoder_model: str, entries, authority_texts, vectors
) -> str:
    digest = hashlib.sha256()
    digest.update(registry_hash.encode("utf-8"))
    digest.update(encoder_model.encode("utf-8"))
    digest.update(INDEX_TEXT_CONTRACT.encode("utf-8"))
    for entry, authority_text in zip(entries, authority_texts):
        digest.update(entry.event_id.encode("utf-8"))
        digest.update(authority_text.encode("utf-8"))
    digest.update(np.asarray(vectors, dtype="<f4").tobytes(order="C"))
    return digest.hexdigest()
