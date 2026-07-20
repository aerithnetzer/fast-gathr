# Aerith Integration Guide

## Purpose

This guide identifies the boundaries Aerith can replace with PostgreSQL, AWS chatbots, and S3 without changing historian review rules. It deliberately avoids documenting UI design rationale that is not needed for backend integration.

## Workflow authority

The application owns this order and its review gates:

```text
Document
  -> Summary & Keywords
  -> Clause Parser
  -> Entity Registry + human row review
  -> Event Extraction
  -> dense candidate lookup + chatbot chooser
  -> human Event headword review
  -> Occurrence Registry
  -> Assembler
  -> versioned export
```

Dependency closure may add required upstream stages when a user selects only a downstream stage. A downstream service consumes accepted or explicitly reviewed upstream data, never an unreviewed model response.

## What "contract" means here

A contract is a stable data shape and behavioural rule shared across components. It lets an AWS provider or database implementation be replaced without teaching it the internal details of every bot.

Machine-facing contracts live in the repository:

- `contracts/emtl_workflow_export_v1.schema.json`: exported package envelope.
- `contracts/postgresql_schema_v1.sql`: proposed relational destination.
- `tagger/integrations/contracts.py`: Python protocols for chatbot jobs, artifacts, and export commits.
- `tagger/integrations/handoff_map.json`: machine-readable provider, review migration, and downstream-package map.
- `tagger/services/contracts.py`: stage request/result labels and lifecycle states.
- `tagger/services/export_contract.py`: canonical export construction, integrity hashes, and validation.

These files are implementation references. They do not need to be sent as separate email attachments when the repository is shared.

## Data authority rule

The export has two deliberately different layers:

- `accepted_data`: only accepted StageOutputs, final Entity decisions, final Event assignments, accepted Occurrence Registry output, and accepted Assembler output. This is the database ingestion authority.
- `audit`: generated attempts, checking/failed outputs, raw responses, validations, and provenance. This is retained for traceability but must not be promoted to accepted domain tables automatically.

`review.decisions` contains stage acceptance, per-row Entity accept/edit/reject decisions, Entity proposals, and Event headword decisions. `export_id` and the canonical SHA-256 make an export idempotent and tamper-evident.

### Four data layers

| Layer | Meaning | Downstream authority |
|---|---|---|
| Raw output | Exact provider text retained for display and audit | No |
| Parsed output | Deterministically parsed records and validation evidence | No |
| Reviewed output | Human decisions applied to parsed records | Not by itself |
| Approved downstream package | Versioned package emitted after the stage-specific approval gate | Yes |

Downstream code must consume package builders rather than reading arbitrary JSON fields from a prior StageOutput. This prevents model text or an unfinished edit from being treated as approved research data.

## AWS chatbot integration

Current local development calls a server-side provider client. Browser JavaScript never receives provider credentials. The durable stage request/result shapes should remain compatible with:

```text
submit(request, idempotency_key=request_id) -> job_id
status(job_id) -> queued | running | completed | failed | cancelled
result(job_id) -> emtl-stage-execution-result-v1
```

Relevant files:

- `tagger/services/providers/factory.py`: the single live-provider selection point.
- `tagger/services/providers/gpu_local.py`: current synchronous development client and normalized result handling.
- `tagger/integrations/contracts.py`: target asynchronous provider protocol.
- `tagger/integrations/stubs.py`: explicit unconfigured cloud behaviour.
- `tagger/services/stage_runner.py`: common stage request construction.
- `tagger/services/summary_assembler_generation.py`, `occurrence_generation.py`, `eventcut_extraction.py`, and `entity_generation.py`: stage-specific prompt and validation boundaries.

`EMTL_STAGE_PROVIDER` is the only provider-selection variable used by live generation services. It currently resolves `gpu_local`. Selecting `aws_bedrock` or `external_api` fails explicitly until the corresponding client is returned from `stage_generation_client()`; it never silently falls back to a fixture.

The Entity runner is the only special capability boundary. Local development performs tokenize-only, prompt-completeness, and GPU-readiness checks before generation. AWS does not need to reproduce local GPU memory diagnostics, but the AWS implementation must preserve prompt/source completeness and real-execution evidence before applying an Entity result.

Recommended integration sequence:

1. Implement `AsyncChatbotProvider` against Aerith's AWS endpoint.
2. Preserve `request_id` as the idempotency key.
3. Persist the job ID before polling so browser refresh does not duplicate a model call.
4. Convert the completed job to the existing stage execution result shape.
5. Store failed validation as audit; do not apply it as accepted stage data.
6. Replace the current synchronous UI wait with job polling when the AWS endpoint is available.

Environment placeholders are in `.env.example`. Secrets must be supplied through the deployment environment or AWS secret management, never committed.

## Review migration from JSON to API and database

Current JSON/JSONField review state is a local persistence implementation, not the public API contract. Preserve the review state machine and audit information while moving each action behind authenticated endpoints:

| Review operation | Current service | Recommended durable destination |
|---|---|---|
| Stage text edit | `save_summary_output`, `save_clause_output`, `save_occurrence_output`, `save_assembler_output` | `emtl_stage_run` revision plus `emtl_review_decision` |
| Entity accept/edit/reject | `save_entity_review_decision` | Append decision; write `emtl_entity_record` only on finalization |
| Entity proposal | `propose_entity` | Append proposal decision; allocate provisional ID server-side |
| Event headword decision | `apply_review_action` | `emtl_event_candidate`, append decision, then one `emtl_event_assignment` |
| Stage approval | `accept_stage` / `finalize_entity_review` | Update exact stage revision and append approval in one transaction |

Every mutation should send the current revision, be idempotent, return the updated review object, and return HTTP 409 on revision conflict. Browser refresh should reload review state from the API rather than reconstruct it from button history. The exact action names, current storage locations, proposed endpoints, and target tables are listed in `tagger/integrations/handoff_map.json`.

## Downstream package boundaries

| Package contract | Producer | Consumer |
|---|---|---|
| `clause-parser-header-body-v1` | Clause validation | Entity, Event Extraction, Occurrence Registry |
| `entity-reviewed-downstream-v1` | `build_entity_downstream_package` | Occurrence Registry, Assembler |
| `eventcut-downstream-v1` | `build_downstream_package` | Dense Event lookup |
| `event-assignment-downstream-v1` | `load_accepted_event_assignments` | Occurrence Registry |
| `occurrence-clause-eaq-v1` | `OccurrenceGenerationService` | Assembler, export |
| `event-occurrence-merged-review-v1` | `build_merged_event_occurrence_package` | Clause-level review, export |
| `tag-assembler-occurrence-conservation-v1` | `AssemblerGenerationService` | Export |
| `emtl-handoff-contract-v1` | `build_workflow_export` | Repository/PostgreSQL/S3 handoff |

Each builder is a contract boundary: its consumer should validate `contract_version` and approval state, not query an upstream model attempt directly. The machine-readable map includes the precise approval gate for every package.

## Authority resources

The selected prompts, registries, controlled lists, and Event workbook are declared by `tagger/chatbot_bundle_manifest.json` and the resource specifications in the relevant services. Their hashes and versions are provenance. Replacement is an explicit versioned operation: update the resource, manifest/specification, expected hash, validation tests, and exported provenance together. Never replace an authority file silently in place.

## PostgreSQL integration

Django already selects PostgreSQL when `DATABASE_URL` is set; otherwise it uses local SQLite. Aerith can choose either:

- Use Django ORM migrations directly against a development PostgreSQL database; or
- Map the formal export into an existing database through `WorkflowRepository.commit_export`.

The proposed DDL in `contracts/postgresql_schema_v1.sql` separates documents, workflow runs, stage runs, model attempts, review decisions, accepted Entity records, EventCuts/candidates/assignments, Occurrence tags, assembled tags, artifacts, and export receipts.

Required database guarantees:

- `export_id` and `request_id` are unique and retry-safe.
- Model attempts and review decisions are append-only audit records.
- Accepted data is revisioned rather than silently overwritten.
- Multi-user review writes use optimistic concurrency and return HTTP 409 on revision conflict.
- One export commit is transactional: checksum verification, audit rows, accepted rows, artifacts, and receipt either commit together or roll back.

## S3 integration

Implement `ArtifactStore` for source documents, large attempt payloads, and workflow exports. PostgreSQL should store immutable references rather than presigned URLs.

Each reference records bucket, key, version ID, SHA-256, content type, size, role, and metadata. Upload and checksum verification happen before the database commit. Short-lived presigned URLs may be issued to the UI for download.

Recommended key pattern:

```text
{prefix}/{workspace_id}/{document_id}/source/{sha256}/{filename}
{prefix}/{workspace_id}/{document_id}/runs/{workflow_run_id}/attempts/{request_id}.json
{prefix}/{workspace_id}/{document_id}/exports/{export_id}.json
```

## Local versus Aerith-owned components

| Area | Included implementation | Aerith integration |
|---|---|---|
| UI and workflow rules | Implemented | Reuse and iterate |
| Review semantics | Implemented and exported | Preserve |
| Local SQLite | Development default | Set `DATABASE_URL` or repository adapter |
| Local model provider | Development-only | Replace with AWS chatbot adapter |
| Local artifact directory | Development-only | Replace with S3 adapter |
| Export contract | Implemented | Validate and ingest |
| Authentication/workspaces | Not production-complete | Connect to platform identity and workspace model |

## Acceptance checklist

Before merging the integration:

1. Run `python manage.py check` and `python manage.py test tagger.tests`.
2. Submit one stage request twice with the same `request_id` and verify only one AWS job exists.
3. Resume a workflow after browser refresh without repeating an accepted stage.
4. Export a document containing edited/rejected Entity rows and final Event headword decisions.
5. Validate the export, ingest it twice, and verify the second commit returns the existing receipt.
6. Verify rejected/checking data appears only in audit tables.
7. Verify S3 checksum and object version before committing its database reference.

## What the tests prove

The automated suite proves deterministic parsing/validation, review state transitions, dependency closure, provider/request contract handling, downstream package gates, export separation/integrity, and Django UI action wiring. It does not prove AWS IAM/deployment, production concurrency, database migration compatibility with an existing Aerith schema, chatbot accuracy, cloud latency/cost, or S3 lifecycle policy. Those require integration and operational acceptance tests in Aerith's environment.

## Explicit non-claims

The handoff contains no Oxford credentials, local model weights, AWS credentials, database API key, deployed AWS adapter, production authentication, or production S3/PostgreSQL connection. The unconfigured adapters fail explicitly until Aerith supplies and selects real implementations.
