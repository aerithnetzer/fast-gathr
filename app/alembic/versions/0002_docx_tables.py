"""docx tables: drop legacy create_all artefacts, install pgvector, create
all tables defined by the docx ``Database fields`` reference.

This migration is destructive for any rows in the legacy domain tables
(``person``, ``mastervocabularylist``, ``mentions``, ``ocurrences``,
``occupation``, ``office``, ``status``, ``residence``, ``worklocation``,
``entity``, ``relationship``, ``socialidentity``, ``locations``,
``things``, ``intangibles``, ``vesselnames``, ``institutions``,
``temporalexpressions``, ``concepts``). Use ``IF EXISTS`` so it works on
both fresh databases and the existing prod database that has these
tables sitting empty from the prior ``SQLModel.metadata.create_all``
behavior.

Revision ID: 0002_docx_tables
Revises: 0001_baseline
Create Date: 2026-06-08
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


revision: str = "0002_docx_tables"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EMBEDDING_DIM = 1536


_LEGACY_TABLES = [
    # Order matters — child tables / FK holders first.
    "person",
    "mentions",
    "ocurrences",
    "vesselnames",
    "entity",
    "relationship",
    "socialidentity",
    "things",
    "intangibles",
    "institutions",
    "temporalexpressions",
    "concepts",
    "locations",
    "occupation",
    "office",
    "status",
    "residence",
    "worklocation",
    "mastervocabularylist",
]


def upgrade() -> None:
    # 1. pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 2. Drop any legacy tables left over from the prior create_all schema.
    #    These were never used in production and we're rebuilding them
    #    cleanly to match the docx.
    for tbl in _LEGACY_TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{tbl}" CASCADE')

    # 3. Document backbone
    op.create_table(
        "documentmetadata",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("record_id", sa.String(length=64), nullable=True),
        sa.Column("archival_reference", sa.String(length=256), nullable=True),
        sa.Column("archive_or_library", sa.String(length=256), nullable=True),
        sa.Column("record_title", sa.String(length=512), nullable=True),
        sa.Column("document_title", sa.String(length=512), nullable=True),
        sa.Column("document_type", sa.String(length=128), nullable=True),
        sa.Column("originating_body", sa.String(length=256), nullable=True),
        sa.Column("plaintiff", sa.String(length=256), nullable=True),
        sa.Column("defendant", sa.String(length=256), nullable=True),
        sa.Column("normalized_date", sa.Date(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("regnal_year", sa.String(length=64), nullable=True),
        sa.Column("court_term", sa.String(length=64), nullable=True),
        sa.Column("reading_order", sa.Integer(), nullable=True),
        sa.Column("topic", sa.String(length=512), nullable=True),
    )

    op.create_table(
        "clause",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "doc_id",
            sa.String(length=32),
            sa.ForeignKey("documentmetadata.id"),
            nullable=False,
        ),
        sa.Column("clause_text", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_clause_doc_id", "clause", ["doc_id"])

    # 4. Master vocabulary
    op.create_table(
        "mastervocabularylist",
        sa.Column("id", sa.String(length=16), primary_key=True),
        sa.Column("headword", sa.String(length=256), nullable=False),
        sa.Column("trigger", sa.String(length=256), nullable=False),
        sa.Column("form", sa.String(length=256), nullable=True),
        sa.Column("classifications", sa.String(length=256), nullable=True),
        sa.Column("notes", sa.String(length=2048), nullable=True),
        sa.Column("definition", sa.Text(), nullable=True),
        sa.Column("vector_embedding", Vector(EMBEDDING_DIM), nullable=True),
    )

    # 5. Locations (self-referential FK)
    op.create_table(
        "locations",
        sa.Column("id", sa.String(length=16), primary_key=True),
        sa.Column("headword", sa.String(length=256), nullable=False),
        sa.Column("trigger", sa.String(length=256), nullable=True),
        sa.Column(
            "located_in_id",
            sa.String(length=16),
            sa.ForeignKey("locations.id"),
            nullable=True,
        ),
        sa.Column("notes", sa.String(length=2048), nullable=True),
    )

    # 6. SocialIdentity (master)
    op.create_table(
        "socialidentity",
        sa.Column("id", sa.String(length=16), primary_key=True),
        sa.Column("headword", sa.String(length=256), nullable=False),
        sa.Column("form", sa.String(length=1024), nullable=True),
        sa.Column("function", sa.String(length=128), nullable=True),
        sa.Column("classification", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.String(length=2048), nullable=True),
    )

    # 7. Person (depends on socialidentity, mastervocabularylist, locations)
    op.create_table(
        "person",
        sa.Column("id", sa.String(length=16), primary_key=True),
        sa.Column("headword", sa.String(length=256), nullable=False),
        sa.Column("trigger", sa.String(length=256), nullable=True),
        sa.Column("surname", sa.String(length=120), nullable=True),
        sa.Column("suffix", sa.String(length=80), nullable=True),
        sa.Column("alias", sa.String(length=120), nullable=True),
        sa.Column(
            "sex", sa.String(length=2), nullable=False, server_default="X"
        ),
        sa.Column("imputed_birth_year", sa.Integer(), nullable=True),
        sa.Column(
            "identity_id",
            sa.String(length=16),
            sa.ForeignKey("socialidentity.id"),
            nullable=True,
        ),
        sa.Column(
            "affiliation_id",
            sa.String(length=16),
            sa.ForeignKey("mastervocabularylist.id"),
            nullable=True,
        ),
        sa.Column(
            "residence_id",
            sa.String(length=16),
            sa.ForeignKey("locations.id"),
            nullable=True,
        ),
        sa.Column(
            "work_location_id",
            sa.String(length=16),
            sa.ForeignKey("locations.id"),
            nullable=True,
        ),
        sa.Column("subscription", sa.String(length=4), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )

    # 8. VesselNames (depends on locations, person)
    op.create_table(
        "vesselnames",
        sa.Column("id", sa.String(length=16), primary_key=True),
        sa.Column("headword", sa.String(length=256), nullable=False),
        sa.Column("trigger", sa.String(length=256), nullable=True),
        sa.Column(
            "home_port_id",
            sa.String(length=16),
            sa.ForeignKey("locations.id"),
            nullable=True,
        ),
        sa.Column(
            "shipmaster_id",
            sa.String(length=16),
            sa.ForeignKey("person.id"),
            nullable=True,
        ),
        sa.Column(
            "shipowner_id",
            sa.String(length=16),
            sa.ForeignKey("person.id"),
            nullable=True,
        ),
    )

    # 9. Relationship master
    op.create_table(
        "relationship",
        sa.Column("id", sa.String(length=16), primary_key=True),
        sa.Column("headword", sa.String(length=256), nullable=False),
        sa.Column("form", sa.String(length=1024), nullable=True),
        sa.Column("inverse_exists", sa.String(length=32), nullable=True),
        sa.Column("inverse_relationship", sa.String(length=32), nullable=True),
    )

    # 10. Mentions
    op.create_table(
        "mentions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "clause_id",
            sa.String(length=32),
            sa.ForeignKey("clause.id"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            sa.String(length=16),
            sa.ForeignKey("mastervocabularylist.id"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(length=256), nullable=True),
    )
    op.create_index("ix_mentions_clause_id", "mentions", ["clause_id"])
    op.create_index("ix_mentions_item_id", "mentions", ["item_id"])

    # 11. Person occurrences
    op.create_table(
        "personoccurrence",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "clause_id",
            sa.String(length=32),
            sa.ForeignKey("clause.id"),
            nullable=False,
        ),
        sa.Column(
            "person_id",
            sa.String(length=16),
            sa.ForeignKey("person.id"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(length=256), nullable=True),
        sa.Column("suffix", sa.String(length=80), nullable=True),
        sa.Column("alias", sa.String(length=120), nullable=True),
        sa.Column("imputed_birth_year", sa.Integer(), nullable=True),
        sa.Column(
            "identity_id",
            sa.String(length=16),
            sa.ForeignKey("socialidentity.id"),
            nullable=True,
        ),
        sa.Column(
            "affiliation_id",
            sa.String(length=16),
            sa.ForeignKey("mastervocabularylist.id"),
            nullable=True,
        ),
        sa.Column(
            "residence_id",
            sa.String(length=16),
            sa.ForeignKey("locations.id"),
            nullable=True,
        ),
        sa.Column(
            "work_location_id",
            sa.String(length=16),
            sa.ForeignKey("locations.id"),
            nullable=True,
        ),
        sa.Column("subscription", sa.String(length=4), nullable=True),
    )
    op.create_index(
        "ix_personoccurrence_clause_id", "personoccurrence", ["clause_id"]
    )
    op.create_index(
        "ix_personoccurrence_person_id", "personoccurrence", ["person_id"]
    )

    # 12. Vessel occurrences
    op.create_table(
        "vesseloccurrence",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "clause_id",
            sa.String(length=32),
            sa.ForeignKey("clause.id"),
            nullable=False,
        ),
        sa.Column(
            "vessel_id",
            sa.String(length=16),
            sa.ForeignKey("vesselnames.id"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(length=256), nullable=True),
        sa.Column(
            "home_port_id",
            sa.String(length=16),
            sa.ForeignKey("locations.id"),
            nullable=True,
        ),
        sa.Column(
            "shipmaster_id",
            sa.String(length=16),
            sa.ForeignKey("person.id"),
            nullable=True,
        ),
        sa.Column(
            "shipowner_id",
            sa.String(length=16),
            sa.ForeignKey("person.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_vesseloccurrence_clause_id", "vesseloccurrence", ["clause_id"]
    )
    op.create_index(
        "ix_vesseloccurrence_vessel_id", "vesseloccurrence", ["vessel_id"]
    )

    # 13. Social identity occurrences
    op.create_table(
        "socialidentityoccurrence",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "clause_id",
            sa.String(length=32),
            sa.ForeignKey("clause.id"),
            nullable=False,
        ),
        sa.Column(
            "social_identity_id",
            sa.String(length=16),
            sa.ForeignKey("socialidentity.id"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(length=256), nullable=True),
        sa.Column("sex", sa.String(length=2), nullable=True),
        sa.Column(
            "secondary_identity_id",
            sa.String(length=16),
            sa.ForeignKey("socialidentity.id"),
            nullable=True,
        ),
        sa.Column(
            "affiliation_id",
            sa.String(length=16),
            sa.ForeignKey("mastervocabularylist.id"),
            nullable=True,
        ),
        sa.Column(
            "attribute_id",
            sa.String(length=16),
            sa.ForeignKey("mastervocabularylist.id"),
            nullable=True,
        ),
        sa.Column(
            "residence_id",
            sa.String(length=16),
            sa.ForeignKey("locations.id"),
            nullable=True,
        ),
        sa.Column(
            "work_location_id",
            sa.String(length=16),
            sa.ForeignKey("locations.id"),
            nullable=True,
        ),
        sa.Column("related_to_id", sa.String(length=16), nullable=True),
        sa.Column("related_to_type", sa.String(length=8), nullable=True),
        sa.Column("plural", sa.String(length=8), nullable=True),
    )
    op.create_index(
        "ix_socialidentityoccurrence_clause_id",
        "socialidentityoccurrence",
        ["clause_id"],
    )
    op.create_index(
        "ix_socialidentityoccurrence_social_identity_id",
        "socialidentityoccurrence",
        ["social_identity_id"],
    )

    # 14. Event occurrences
    op.create_table(
        "eventoccurrence",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "clause_id",
            sa.String(length=32),
            sa.ForeignKey("clause.id"),
            nullable=False,
        ),
        sa.Column(
            "event_id",
            sa.String(length=16),
            sa.ForeignKey("mastervocabularylist.id"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(length=256), nullable=True),
        sa.Column("actor_id", sa.String(length=16), nullable=True),
        sa.Column("actor_type", sa.String(length=8), nullable=True),
        sa.Column("counterparty_id", sa.String(length=16), nullable=True),
        sa.Column("counterparty_type", sa.String(length=8), nullable=True),
        sa.Column("object_id", sa.String(length=16), nullable=True),
        sa.Column("object_type", sa.String(length=8), nullable=True),
        sa.Column("means_id", sa.String(length=16), nullable=True),
        sa.Column("means_type", sa.String(length=8), nullable=True),
        sa.Column(
            "attribute_id",
            sa.String(length=16),
            sa.ForeignKey("mastervocabularylist.id"),
            nullable=True,
        ),
        sa.Column(
            "place_where_id",
            sa.String(length=16),
            sa.ForeignKey("locations.id"),
            nullable=True,
        ),
        sa.Column(
            "place_from_id",
            sa.String(length=16),
            sa.ForeignKey("locations.id"),
            nullable=True,
        ),
        sa.Column(
            "place_to_id",
            sa.String(length=16),
            sa.ForeignKey("locations.id"),
            nullable=True,
        ),
        sa.Column("when_", sa.String(length=128), nullable=True),
        sa.Column("modality", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_eventoccurrence_clause_id", "eventoccurrence", ["clause_id"]
    )
    op.create_index(
        "ix_eventoccurrence_event_id", "eventoccurrence", ["event_id"]
    )

    # 15. Relationship occurrences
    op.create_table(
        "relationshipoccurrence",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "clause_id",
            sa.String(length=32),
            sa.ForeignKey("clause.id"),
            nullable=False,
        ),
        sa.Column(
            "relationship_id",
            sa.String(length=16),
            sa.ForeignKey("relationship.id"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(length=256), nullable=True),
        sa.Column("subject_id", sa.String(length=16), nullable=True),
        sa.Column("subject_type", sa.String(length=8), nullable=True),
        sa.Column("object_id", sa.String(length=16), nullable=True),
        sa.Column("object_type", sa.String(length=8), nullable=True),
        sa.Column("inverse", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_relationshipoccurrence_clause_id",
        "relationshipoccurrence",
        ["clause_id"],
    )
    op.create_index(
        "ix_relationshipoccurrence_relationship_id",
        "relationshipoccurrence",
        ["relationship_id"],
    )

    # 16. Attribute occurrences
    op.create_table(
        "attributeoccurrence",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "clause_id",
            sa.String(length=32),
            sa.ForeignKey("clause.id"),
            nullable=False,
        ),
        sa.Column(
            "attribute_id",
            sa.String(length=16),
            sa.ForeignKey("mastervocabularylist.id"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(length=256), nullable=True),
        sa.Column("object_id", sa.String(length=16), nullable=True),
        sa.Column("object_type", sa.String(length=8), nullable=True),
    )
    op.create_index(
        "ix_attributeoccurrence_clause_id",
        "attributeoccurrence",
        ["clause_id"],
    )
    op.create_index(
        "ix_attributeoccurrence_attribute_id",
        "attributeoccurrence",
        ["attribute_id"],
    )

    # 17. Quantified statement occurrences
    op.create_table(
        "quantifiedstatementoccurrence",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "clause_id",
            sa.String(length=32),
            sa.ForeignKey("clause.id"),
            nullable=False,
        ),
        sa.Column(
            "q_id",
            sa.String(length=16),
            sa.ForeignKey("mastervocabularylist.id"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(length=256), nullable=True),
        sa.Column("object_id", sa.String(length=16), nullable=True),
        sa.Column("object_type", sa.String(length=8), nullable=True),
        sa.Column("quantity", sa.String(length=64), nullable=True),
        sa.Column(
            "unit_id",
            sa.String(length=16),
            sa.ForeignKey("mastervocabularylist.id"),
            nullable=True,
        ),
        sa.Column("per_quantity", sa.String(length=64), nullable=True),
        sa.Column(
            "per_unit_id",
            sa.String(length=16),
            sa.ForeignKey("mastervocabularylist.id"),
            nullable=True,
        ),
        sa.Column("rate_period", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_quantifiedstatementoccurrence_clause_id",
        "quantifiedstatementoccurrence",
        ["clause_id"],
    )
    op.create_index(
        "ix_quantifiedstatementoccurrence_q_id",
        "quantifiedstatementoccurrence",
        ["q_id"],
    )

    # 18. Summaries
    op.create_table(
        "summary",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "doc_id",
            sa.String(length=32),
            sa.ForeignKey("documentmetadata.id"),
            nullable=False,
        ),
        sa.Column("summary_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_date", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_summary_doc_id", "summary", ["doc_id"])

    # 19. Keyword (master)
    op.create_table(
        "keyword",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("keyword", sa.String(length=256), nullable=False),
        sa.Column("definition", sa.Text(), nullable=True),
        sa.Column("vector_embedding", Vector(EMBEDDING_DIM), nullable=True),
    )

    # 20. Keyword occurrences
    op.create_table(
        "keywordoccurrence",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "doc_id",
            sa.String(length=32),
            sa.ForeignKey("documentmetadata.id"),
            nullable=False,
        ),
        sa.Column(
            "keyword_id",
            sa.String(length=32),
            sa.ForeignKey("keyword.id"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(length=256), nullable=True),
    )
    op.create_index(
        "ix_keywordoccurrence_doc_id", "keywordoccurrence", ["doc_id"]
    )
    op.create_index(
        "ix_keywordoccurrence_keyword_id", "keywordoccurrence", ["keyword_id"]
    )

    # 21. Chat record
    op.create_table(
        "chatrecord",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "doc_id",
            sa.String(length=32),
            sa.ForeignKey("documentmetadata.id"),
            nullable=False,
        ),
        sa.Column("chat_text", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_chatrecord_doc_id", "chatrecord", ["doc_id"])


def downgrade() -> None:
    # Reverse order of creation.
    op.drop_index("ix_chatrecord_doc_id", table_name="chatrecord")
    op.drop_table("chatrecord")
    op.drop_index("ix_keywordoccurrence_keyword_id", table_name="keywordoccurrence")
    op.drop_index("ix_keywordoccurrence_doc_id", table_name="keywordoccurrence")
    op.drop_table("keywordoccurrence")
    op.drop_table("keyword")
    op.drop_index("ix_summary_doc_id", table_name="summary")
    op.drop_table("summary")
    op.drop_index(
        "ix_quantifiedstatementoccurrence_q_id",
        table_name="quantifiedstatementoccurrence",
    )
    op.drop_index(
        "ix_quantifiedstatementoccurrence_clause_id",
        table_name="quantifiedstatementoccurrence",
    )
    op.drop_table("quantifiedstatementoccurrence")
    op.drop_index(
        "ix_attributeoccurrence_attribute_id", table_name="attributeoccurrence"
    )
    op.drop_index(
        "ix_attributeoccurrence_clause_id", table_name="attributeoccurrence"
    )
    op.drop_table("attributeoccurrence")
    op.drop_index(
        "ix_relationshipoccurrence_relationship_id",
        table_name="relationshipoccurrence",
    )
    op.drop_index(
        "ix_relationshipoccurrence_clause_id", table_name="relationshipoccurrence"
    )
    op.drop_table("relationshipoccurrence")
    op.drop_index("ix_eventoccurrence_event_id", table_name="eventoccurrence")
    op.drop_index("ix_eventoccurrence_clause_id", table_name="eventoccurrence")
    op.drop_table("eventoccurrence")
    op.drop_index(
        "ix_socialidentityoccurrence_social_identity_id",
        table_name="socialidentityoccurrence",
    )
    op.drop_index(
        "ix_socialidentityoccurrence_clause_id",
        table_name="socialidentityoccurrence",
    )
    op.drop_table("socialidentityoccurrence")
    op.drop_index(
        "ix_vesseloccurrence_vessel_id", table_name="vesseloccurrence"
    )
    op.drop_index(
        "ix_vesseloccurrence_clause_id", table_name="vesseloccurrence"
    )
    op.drop_table("vesseloccurrence")
    op.drop_index(
        "ix_personoccurrence_person_id", table_name="personoccurrence"
    )
    op.drop_index(
        "ix_personoccurrence_clause_id", table_name="personoccurrence"
    )
    op.drop_table("personoccurrence")
    op.drop_index("ix_mentions_item_id", table_name="mentions")
    op.drop_index("ix_mentions_clause_id", table_name="mentions")
    op.drop_table("mentions")
    op.drop_table("relationship")
    op.drop_table("vesselnames")
    op.drop_table("person")
    op.drop_table("socialidentity")
    op.drop_table("locations")
    op.drop_table("mastervocabularylist")
    op.drop_index("ix_clause_doc_id", table_name="clause")
    op.drop_table("clause")
    op.drop_table("documentmetadata")
