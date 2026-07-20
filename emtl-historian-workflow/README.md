# EMTL Historian Workflow

This Django application is the historian-facing workflow shell for the Early Modern Text Lab. It accepts an uploaded source document and coordinates Summary & Keywords, Clause Parser, Entity Registry, Event Extraction, Event headword review, Occurrence Registry, Assembler, and export.

The repository is intended to let the frontend/workflow team and Aerith's database/AWS work proceed against stable boundaries. Local model execution is an implementation used for development, not a production requirement.

## What is implemented

- Document upload, source correction, stage selection, and dependency closure.
- Resumable stage state in Django models.
- Human review for Entity rows and Event headword assignments.
- Accept-all-remaining actions that preserve prior edits and rejects.
- Editable Summary, Clause, Occurrence Registry, and Assembler outputs with validation.
- Dense Event candidate lookup and controlled headword selection.
- A versioned workflow export separating accepted data from audit-only data.
- PostgreSQL DDL plus provider, repository, and artifact-store interfaces.
- Explicit unconfigured AWS, PostgreSQL-repository, and S3 adapters; no cloud access is claimed.

## Start the application without the local model stack

Aerith can inspect the UI, models, contracts, and tutorial surfaces without Oxford GPU access:

```powershell
cd emtl_django
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

Open `http://127.0.0.1:8000/`.

`.env.example` is an environment-variable reference, not an automatically loaded secrets file. Set required values in the process environment or deployment platform.

Uploaded-document chatbot actions require a configured provider. Without one, document upload, workflow selection, database inspection, contracts, and non-provider UI remain available; model-backed actions report that the provider is unavailable. The Oxford/Qwen operator scripts and credentials are deliberately not part of the Aerith handoff package.

## Required reading

Only two documents are designated as required:

1. This README: application purpose, setup, and repository map.
2. `docs/AERITH_INTEGRATION_GUIDE.md`: exact AWS chatbot, PostgreSQL, S3, and export integration boundaries.

Everything under `contracts/` is machine-facing implementation reference rather than additional required reading.

## Repository map

- `tagger/templates/` and `tagger/static/`: historian-facing UI.
- `tagger/live_views.py`: live workflow HTTP actions and presentation mapping.
- `tagger/services/`: orchestration, generation, validation, review, and export logic.
- `tagger/models.py`: current Django persistence model.
- `tagger/integrations/`: provider, repository, and artifact-store protocols/stubs.
- `tagger/integrations/handoff_map.json`: exact provider call sites, review migration map, and downstream package boundaries.
- `tagger/services/providers/factory.py`: single live-provider selection and replacement point.
- `contracts/`: JSON Schema and proposed PostgreSQL DDL.
- `tagger/tests/`: workflow, review, provider, and export contract tests.
- `.env.example`: environment variable names only; it contains no credentials.

## Verification

```powershell
python manage.py check
python manage.py test tagger.tests
```

Do not commit `.env`, SQLite databases, runtime outputs, uploaded documents, credentials, caches, or local model files.
