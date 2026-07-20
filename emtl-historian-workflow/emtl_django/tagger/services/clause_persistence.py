from __future__ import annotations

from typing import Any

from django.db import transaction

from tagger.models import Clause, Document


@transaction.atomic
def replace_document_clauses(
    document: Document,
    clauses: list[dict[str, Any]],
) -> None:
    """Replace a Document's body clauses using the established persistence format."""

    document.clauses.all().delete()
    for index, clause in enumerate(clauses, start=1):
        Clause.objects.create(
            document=document,
            clause_id=str(clause.get("clause_id") or index).zfill(3),
            sequence=index,
            text=str(clause.get("text") or ""),
        )
