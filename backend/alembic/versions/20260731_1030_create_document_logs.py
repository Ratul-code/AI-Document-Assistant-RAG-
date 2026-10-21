"""create document_logs

Revision ID: 65cfc10044dd
Revises: fee8236f7164
Create Date: 2026-07-31 10:30:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '65cfc10044dd'
down_revision: Union[str, None] = 'fee8236f7164'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'document_logs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('document_id', sa.Uuid(), nullable=False),
        sa.Column('owner_id', sa.Uuid(), nullable=False),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('metadata_json', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    # Per-document recent-first queries (logs panel on doc page).
    op.create_index(
        op.f('ix_document_logs_document_id_created_at'),
        'document_logs',
        ['document_id', 'created_at'],
        unique=False,
    )
    # Per-owner recent-first queries (global activity feed).
    op.create_index(
        op.f('ix_document_logs_owner_id_created_at'),
        'document_logs',
        ['owner_id', 'created_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_document_logs_owner_id_created_at'), table_name='document_logs')
    op.drop_index(op.f('ix_document_logs_document_id_created_at'), table_name='document_logs')
    op.drop_table('document_logs')