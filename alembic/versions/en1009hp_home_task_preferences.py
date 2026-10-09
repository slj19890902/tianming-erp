"""Persist homepage reminders without modifying business records."""
from alembic import op
import sqlalchemy as sa

revision = "en1009hp"
down_revision = "em1009bs"
branch_labels = depends_on = None


def upgrade():
    op.create_table("home_task_preferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scope_key", sa.String(40), nullable=False),
        sa.Column("task_key", sa.String(180), nullable=False),
        sa.Column("customer_ids_json", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("remind_on", sa.Date(), nullable=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("scope_key", "task_key", name="uq_home_task_scope"),
        sa.CheckConstraint("version > 0", name="ck_home_task_version"),
        sa.CheckConstraint("state IN ('active','snoozed','waiting_customer','stock_review','cancel_review','condition_wait','verify','hidden')", name="ck_home_task_state"))
    op.create_table("home_task_mutations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("preference_id", sa.Integer(), sa.ForeignKey("home_task_preferences.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("customer_ids_json", sa.Text(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("actor_id", "idempotency_key", name="uq_home_mutation_key"))
    for action in ("UPDATE", "DELETE"):
        op.execute(f"CREATE TRIGGER home_task_mutations_no_{action.lower()} BEFORE {action} ON home_task_mutations "
                   "BEGIN SELECT RAISE(ABORT, 'home reminder history is immutable'); END")


def downgrade():
    for table in ("home_task_preferences", "home_task_mutations"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("已有首页提醒记录，禁止有损降级；保留数据库向前修复")
    for action in ("update", "delete"):
        op.execute(f"DROP TRIGGER home_task_mutations_no_{action}")
    op.drop_table("home_task_mutations")
    op.drop_table("home_task_preferences")
