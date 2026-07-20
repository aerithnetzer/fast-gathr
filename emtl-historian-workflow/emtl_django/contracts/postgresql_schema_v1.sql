CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE emtl_document (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    external_document_id text NOT NULL UNIQUE,
    workspace_id uuid,
    title text NOT NULL,
    archival_reference text NOT NULL DEFAULT '',
    document_type text NOT NULL DEFAULT '',
    normalized_date text NOT NULL DEFAULT '',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    revision bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE emtl_artifact (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid REFERENCES emtl_document(id) ON DELETE CASCADE,
    role text NOT NULL,
    uri text NOT NULL,
    bucket text NOT NULL DEFAULT '',
    object_key text NOT NULL DEFAULT '',
    version_id text NOT NULL DEFAULT '',
    sha256 char(64) NOT NULL,
    content_type text NOT NULL,
    size_bytes bigint NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (bucket, object_key, version_id)
);

CREATE TABLE emtl_workflow_run (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid NOT NULL REFERENCES emtl_document(id) ON DELETE CASCADE,
    contract_version text NOT NULL,
    requested_stages jsonb NOT NULL,
    expanded_stages jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('ready','running','waiting_for_review','blocked','complete','failed','cancelled')),
    plan_fingerprint char(64) NOT NULL,
    orchestration_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    revision bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE emtl_stage_run (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_run_id uuid NOT NULL REFERENCES emtl_workflow_run(id) ON DELETE CASCADE,
    stage_id text NOT NULL,
    lifecycle_status text NOT NULL,
    display_title text NOT NULL DEFAULT '',
    raw_output text NOT NULL DEFAULT '',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    revision bigint NOT NULL DEFAULT 0,
    accepted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workflow_run_id, stage_id)
);

CREATE TABLE emtl_model_attempt (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    stage_run_id uuid NOT NULL REFERENCES emtl_stage_run(id) ON DELETE CASCADE,
    request_id text NOT NULL UNIQUE,
    provider text NOT NULL,
    model text NOT NULL DEFAULT '',
    execution_status text NOT NULL,
    disposition text NOT NULL,
    raw_output text NOT NULL DEFAULT '',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    validation jsonb NOT NULL DEFAULT '{}'::jsonb,
    error jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE emtl_clause (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid NOT NULL REFERENCES emtl_document(id) ON DELETE CASCADE,
    clause_id text NOT NULL,
    sequence integer NOT NULL,
    text text NOT NULL,
    start_char integer,
    end_char integer,
    UNIQUE (document_id, clause_id),
    UNIQUE (document_id, sequence)
);

CREATE TABLE emtl_review_decision (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_run_id uuid NOT NULL REFERENCES emtl_workflow_run(id) ON DELETE CASCADE,
    external_decision_id text NOT NULL,
    stage_id text NOT NULL DEFAULT '',
    target_type text NOT NULL,
    target_id text NOT NULL,
    decision_type text NOT NULL,
    original_value jsonb NOT NULL DEFAULT '{}'::jsonb,
    edited_value jsonb NOT NULL DEFAULT '{}'::jsonb,
    selected_candidate jsonb NOT NULL DEFAULT '{}'::jsonb,
    proposed_value jsonb NOT NULL DEFAULT '{}'::jsonb,
    reviewer_id text NOT NULL DEFAULT '',
    reviewer_note text NOT NULL DEFAULT '',
    is_final boolean NOT NULL,
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    revision bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workflow_run_id, external_decision_id)
);

CREATE TABLE emtl_entity_record (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_run_id uuid NOT NULL REFERENCES emtl_workflow_run(id) ON DELETE CASCADE,
    clause_id uuid REFERENCES emtl_clause(id) ON DELETE SET NULL,
    stable_id text NOT NULL,
    record_type text NOT NULL,
    headword text NOT NULL,
    evidence_form text NOT NULL DEFAULT '',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    review_decision_id uuid REFERENCES emtl_review_decision(id) ON DELETE SET NULL,
    UNIQUE (workflow_run_id, stable_id, headword, evidence_form)
);

CREATE TABLE emtl_event_cut (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_run_id uuid NOT NULL REFERENCES emtl_workflow_run(id) ON DELETE CASCADE,
    clause_id uuid NOT NULL REFERENCES emtl_clause(id) ON DELETE CASCADE,
    external_event_cut_id text NOT NULL,
    text_verbatim text NOT NULL,
    trigger_text text NOT NULL DEFAULT '',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (workflow_run_id, external_event_cut_id)
);

CREATE TABLE emtl_event_candidate (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_cut_id uuid NOT NULL REFERENCES emtl_event_cut(id) ON DELETE CASCADE,
    rank integer NOT NULL,
    event_id text NOT NULL,
    headword text NOT NULL,
    cosine_similarity double precision,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (event_cut_id, rank)
);

CREATE TABLE emtl_event_assignment (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_cut_id uuid NOT NULL UNIQUE REFERENCES emtl_event_cut(id) ON DELETE CASCADE,
    event_id text NOT NULL,
    headword text NOT NULL,
    assignment_source text NOT NULL,
    review_decision_id uuid REFERENCES emtl_review_decision(id) ON DELETE SET NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    accepted_at timestamptz NOT NULL
);

CREATE TABLE emtl_occurrence_tag (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_run_id uuid NOT NULL REFERENCES emtl_workflow_run(id) ON DELETE CASCADE,
    clause_id uuid NOT NULL REFERENCES emtl_clause(id) ON DELETE CASCADE,
    tag_type char(1) NOT NULL CHECK (tag_type IN ('E','A','Q')),
    stable_id text NOT NULL,
    headword text NOT NULL,
    raw_line text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    sequence integer NOT NULL,
    UNIQUE (workflow_run_id, clause_id, tag_type, sequence)
);

CREATE TABLE emtl_assembled_tag (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_run_id uuid NOT NULL REFERENCES emtl_workflow_run(id) ON DELETE CASCADE,
    clause_id uuid NOT NULL REFERENCES emtl_clause(id) ON DELETE CASCADE,
    tag_type text NOT NULL,
    raw_line text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    sequence integer NOT NULL,
    UNIQUE (workflow_run_id, clause_id, sequence)
);

CREATE TABLE emtl_export_package (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_run_id uuid NOT NULL REFERENCES emtl_workflow_run(id) ON DELETE CASCADE,
    export_id text NOT NULL UNIQUE,
    schema_version text NOT NULL,
    canonical_sha256 char(64) NOT NULL,
    package jsonb NOT NULL,
    artifact_id uuid REFERENCES emtl_artifact(id) ON DELETE SET NULL,
    idempotency_key text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX emtl_stage_run_status_idx ON emtl_stage_run (lifecycle_status);
CREATE INDEX emtl_review_target_idx ON emtl_review_decision (target_type, target_id);
CREATE INDEX emtl_occurrence_clause_idx ON emtl_occurrence_tag (clause_id, sequence);
