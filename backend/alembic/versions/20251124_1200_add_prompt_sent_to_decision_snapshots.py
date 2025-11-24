"""Add prompt_sent column to decision_snapshots table.

Revision ID: add_prompt_sent
Revises: c18829e9861f
Create Date: 2025-11-24 12:00:00

Stores the full prompt sent to DeepSeek for each trading decision,
enabling debugging and prompt analysis in the frontend.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_prompt_sent'
down_revision = 'c18829e9861f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add prompt_sent column to decision_snapshots table."""
    op.add_column(
        'decision_snapshots',
        sa.Column('prompt_sent', sa.Text(), nullable=True)
    )


def downgrade() -> None:
    """Remove prompt_sent column from decision_snapshots table."""
    op.drop_column('decision_snapshots', 'prompt_sent')
