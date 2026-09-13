"""Persistent analysis attempts and atomic publication.

Revision ID: 0013_analysis
Revises: 0012_access
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_analysis"
down_revision: str | None = "0012_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE analysis_runs
          ADD COLUMN attempt_id uuid,
          ADD COLUMN attempt_expires_at timestamptz,
          ADD COLUMN attempt_job_id uuid,
          ADD COLUMN attempt_lease_token uuid,
          ADD COLUMN summary text NOT NULL DEFAULT '',
          ADD COLUMN abstained_reason text,
          ADD COLUMN published_at timestamptz;
        UPDATE analysis_runs r SET
          summary = COALESCE((SELECT e.payload->>'text' FROM outbox_events e
              WHERE e.workspace_id=r.workspace_id AND e.aggregate_id=r.id
                AND e.event_type='AnalysisCompleted'
              ORDER BY e.event_seq LIMIT 1), ''),
          published_at = (SELECT min(e.occurred_at) FROM outbox_events e
              WHERE e.workspace_id=r.workspace_id AND e.aggregate_id=r.id
                AND e.event_type='AnalysisCompleted');
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE analysis_runs
          DROP COLUMN published_at, DROP COLUMN abstained_reason,
          DROP COLUMN summary, DROP COLUMN attempt_lease_token,
          DROP COLUMN attempt_job_id, DROP COLUMN attempt_expires_at,
          DROP COLUMN attempt_id;
    """)
