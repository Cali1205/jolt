"""Tokens of cloud sources whose refresh tokens rotate (Stellantis).

Revision ID: 0025
Revises: 0024
"""
from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cloud_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("account", sa.String(length=200), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("source", "account", name="uq_cloud_credential"),
    )


def downgrade() -> None:
    op.drop_table("cloud_credentials")
