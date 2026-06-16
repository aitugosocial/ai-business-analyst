"""add_recommendation_mode_to_business_analyses

Revision ID: cf2b1944f856
Revises: 80725524ba7b
Create Date: 2026-05-12 01:55:55.175920

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'cf2b1944f856'
down_revision: Union[str, Sequence[str], None] = '80725524ba7b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Every operation in this function is idempotent: IF NOT EXISTS / IF EXISTS
    # guards are used throughout so the migration is safe to run against a DB
    # that is partially or fully ahead of Alembic's recorded revision.

    # --- security_metrics_summary ------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS security_metrics_summary (
            total_events_24h         INTEGER NOT NULL PRIMARY KEY,
            high_severity_events_24h INTEGER,
            blocked_attacks_24h      INTEGER,
            failed_logins_24h        INTEGER,
            active_blacklisted_ips   INTEGER,
            active_firewall_rules    INTEGER
        )
    """)

    # --- drop legacy tables (indexes first, then table) --------------------
    op.execute("DROP INDEX IF EXISTS ix_system_metrics_id")
    op.execute("DROP INDEX IF EXISTS ix_system_metrics_timestamp")
    op.execute("DROP TABLE IF EXISTS system_metrics")

    op.execute("DROP INDEX IF EXISTS idx_errors_last_seen")
    op.execute("DROP INDEX IF EXISTS idx_errors_resolved")
    op.execute("DROP INDEX IF EXISTS idx_errors_service")
    op.execute("DROP INDEX IF EXISTS idx_errors_severity")
    op.execute("DROP INDEX IF EXISTS idx_errors_type")
    op.execute("DROP INDEX IF EXISTS ix_error_tracking_id")
    op.execute("DROP TABLE IF EXISTS error_tracking")

    op.execute("DROP INDEX IF EXISTS idx_service_health_created")
    op.execute("DROP INDEX IF EXISTS idx_service_health_service")
    op.execute("DROP INDEX IF EXISTS idx_service_health_status")
    op.execute("DROP INDEX IF EXISTS ix_service_health_id")
    op.execute("DROP TABLE IF EXISTS service_health")

    op.execute("DROP INDEX IF EXISTS idx_activity_logs_created")
    op.execute("DROP INDEX IF EXISTS idx_activity_logs_operation")
    op.execute("DROP INDEX IF EXISTS idx_activity_logs_service")
    op.execute("DROP INDEX IF EXISTS idx_activity_logs_success")
    op.execute("DROP INDEX IF EXISTS ix_activity_logs_id")
    op.execute("DROP TABLE IF EXISTS activity_logs")

    op.execute("DROP INDEX IF EXISTS idx_system_alerts_service")
    op.execute("DROP INDEX IF EXISTS idx_system_alerts_status")
    op.execute("DROP INDEX IF EXISTS idx_system_alerts_timestamp")
    op.execute("DROP INDEX IF EXISTS idx_system_alerts_type")
    op.execute("DROP INDEX IF EXISTS ix_system_alerts_id")
    op.execute("DROP TABLE IF EXISTS system_alerts")

    op.execute("DROP INDEX IF EXISTS idx_uptime_date")
    op.execute("DROP INDEX IF EXISTS idx_uptime_date_unique")
    op.execute("DROP INDEX IF EXISTS ix_uptime_records_id")
    op.execute("DROP TABLE IF EXISTS uptime_records")

    op.execute("DROP INDEX IF EXISTS ix_roadmap_stages_id")
    op.execute("DROP TABLE IF EXISTS roadmap_stages")

    op.execute("DROP INDEX IF EXISTS idx_api_usage_date")
    op.execute("DROP INDEX IF EXISTS idx_api_usage_endpoint")
    op.execute("DROP INDEX IF EXISTS idx_api_usage_user")
    op.execute("DROP INDEX IF EXISTS ix_api_usage_id")
    op.execute("DROP TABLE IF EXISTS api_usage")

    op.execute("DROP INDEX IF EXISTS idx_db_ops_duration")
    op.execute("DROP INDEX IF EXISTS idx_db_ops_table")
    op.execute("DROP INDEX IF EXISTS idx_db_ops_timestamp")
    op.execute("DROP INDEX IF EXISTS ix_database_operations_id")
    op.execute("DROP TABLE IF EXISTS database_operations")

    op.execute("DROP INDEX IF EXISTS idx_waitlist_created_at")
    op.execute("DROP INDEX IF EXISTS idx_waitlist_email")
    op.execute("DROP INDEX IF EXISTS idx_waitlist_status")
    op.execute("DROP INDEX IF EXISTS ix_waitlist_email")
    op.execute("DROP INDEX IF EXISTS ix_waitlist_id")
    op.execute("DROP INDEX IF EXISTS ix_waitlist_referral_code")
    op.execute("DROP TABLE IF EXISTS waitlist")

    op.execute("DROP INDEX IF EXISTS idx_performance_endpoint")
    op.execute("DROP INDEX IF EXISTS idx_performance_status")
    op.execute("DROP INDEX IF EXISTS idx_performance_timestamp")
    op.execute("DROP INDEX IF EXISTS ix_performance_metrics_id")
    op.execute("DROP TABLE IF EXISTS performance_metrics")

    op.execute("DROP TABLE IF EXISTS tool_combinations")

    # --- ai_tools ----------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS ai_tools_embedding_hnsw_idx")
    op.execute("ALTER TABLE ai_tools DROP COLUMN IF EXISTS embedding")

    # --- alerts ------------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_alerts_created_at")
    op.execute("DROP INDEX IF EXISTS idx_alerts_is_active")

    # --- business_analyses -------------------------------------------------
    op.execute("ALTER TABLE business_analyses ADD COLUMN IF NOT EXISTS recommendation_mode TEXT")
    op.execute("ALTER TABLE business_analyses ADD COLUMN IF NOT EXISTS single_tool_recommendation JSON")
    op.execute("DROP INDEX IF EXISTS idx_ba_created_at")
    op.execute("DROP INDEX IF EXISTS idx_ba_user_id")
    op.execute("DROP INDEX IF EXISTS idx_business_analyses_analysis_type")
    op.execute("DROP INDEX IF EXISTS idx_business_analyses_created_at")
    op.execute("DROP INDEX IF EXISTS idx_business_analyses_status")

    # --- channel_members ---------------------------------------------------
    op.execute("ALTER TABLE channel_members DROP CONSTRAINT IF EXISTS channel_members_user_id_channel_id_key")
    op.execute("DROP INDEX IF EXISTS idx_channel_members_channel")
    op.execute("DROP INDEX IF EXISTS idx_channel_members_user")
    op.execute("DROP INDEX IF EXISTS idx_cm_channel_id")
    op.execute("DROP INDEX IF EXISTS idx_cm_user_id")
    op.execute("CREATE INDEX IF NOT EXISTS ix_channel_members_id ON channel_members (id)")

    # --- commission_summaries ----------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_cs_user_id")

    # --- commissions -------------------------------------------------------
    op.alter_column('commissions', 'referred_user_id',
               existing_type=sa.INTEGER(),
               nullable=True)
    op.alter_column('commissions', 'subscription_id',
               existing_type=sa.INTEGER(),
               nullable=True)
    op.alter_column('commissions', 'original_amount',
               existing_type=sa.NUMERIC(precision=10, scale=2),
               nullable=True)
    op.alter_column('commissions', 'currency',
               existing_type=sa.VARCHAR(length=10),
               nullable=True)
    op.execute("DROP INDEX IF EXISTS idx_com_created_at")
    op.execute("DROP INDEX IF EXISTS idx_com_referred_id")
    op.execute("DROP INDEX IF EXISTS idx_com_status")
    op.execute("DROP INDEX IF EXISTS idx_com_sub_id")
    op.execute("DROP INDEX IF EXISTS idx_com_user_id")
    op.execute("DROP INDEX IF EXISTS idx_commissions_created_at")
    op.execute("DROP INDEX IF EXISTS idx_commissions_user_status")
    op.execute("ALTER TABLE commissions DROP CONSTRAINT IF EXISTS commissions_payout_id_fkey")

    # --- community_activities ----------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_community_activities_user")
    op.execute("CREATE INDEX IF NOT EXISTS ix_community_activities_id ON community_activities (id)")

    # --- community_channels ------------------------------------------------
    op.alter_column('community_channels', 'icon',
               existing_type=sa.VARCHAR(length=10),
               type_=sa.String(length=20),
               existing_nullable=True)
    op.execute("ALTER TABLE community_channels DROP CONSTRAINT IF EXISTS community_channels_slug_key")
    op.execute("CREATE INDEX IF NOT EXISTS ix_community_channels_id ON community_channels (id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_community_channels_slug ON community_channels (slug)")

    # --- community_discussions ---------------------------------------------
    op.alter_column('community_discussions', 'tags',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               type_=sa.JSON(),
               existing_nullable=True)
    op.execute("DROP INDEX IF EXISTS idx_cd_channel_id")
    op.execute("DROP INDEX IF EXISTS idx_cd_user_id")
    op.execute("DROP INDEX IF EXISTS idx_discussions_channel")
    op.execute("DROP INDEX IF EXISTS idx_discussions_user")
    op.execute("CREATE INDEX IF NOT EXISTS ix_community_discussions_id ON community_discussions (id)")

    # --- community_events --------------------------------------------------
    op.execute("CREATE INDEX IF NOT EXISTS ix_community_events_id ON community_events (id)")

    # --- conversations -----------------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_conv_is_read")
    op.execute("DROP INDEX IF EXISTS idx_conv_review_id")

    # --- discussion_likes --------------------------------------------------
    op.execute("ALTER TABLE discussion_likes DROP CONSTRAINT IF EXISTS discussion_likes_user_id_discussion_id_key")
    op.execute("DROP INDEX IF EXISTS idx_dl_user_id")
    op.execute("CREATE INDEX IF NOT EXISTS ix_discussion_likes_id ON discussion_likes (id)")

    # --- discussion_replies ------------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_dr_discussion_id")
    op.execute("CREATE INDEX IF NOT EXISTS ix_discussion_replies_id ON discussion_replies (id)")

    # --- event_registrations -----------------------------------------------
    op.execute("ALTER TABLE event_registrations DROP CONSTRAINT IF EXISTS event_registrations_user_id_event_id_key")
    op.execute("CREATE INDEX IF NOT EXISTS ix_event_registrations_id ON event_registrations (id)")

    # --- insights ----------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_insights_created_at")
    op.execute("DROP INDEX IF EXISTS idx_insights_is_active")

    # --- marketplace_purchases ---------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_mp_status")
    op.execute("DROP INDEX IF EXISTS idx_mp_tool_id")
    op.execute("DROP INDEX IF EXISTS idx_mp_user_id")

    # --- mission_steps -----------------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_mission_steps_mission_id")

    # --- payout_accounts ---------------------------------------------------
    # Backfill NULLs before enforcing NOT NULL — existing rows with no value
    # would cause Postgres to reject the constraint otherwise.
    op.execute("UPDATE payout_accounts SET default_payout_method = 'bank_transfer' WHERE default_payout_method IS NULL")
    op.alter_column('payout_accounts', 'default_payout_method',
               existing_type=sa.VARCHAR(length=20),
               type_=sa.String(length=50),
               nullable=False)
    op.alter_column('payout_accounts', 'account_number',
               existing_type=sa.VARCHAR(length=50),
               type_=sa.String(length=100),
               existing_nullable=True)
    op.execute("DROP INDEX IF EXISTS idx_pa_user_id")

    # --- payouts -----------------------------------------------------------
    op.alter_column('payouts', 'currency',
               existing_type=sa.VARCHAR(length=10),
               nullable=True)
    op.alter_column('payouts', 'payment_method',
               existing_type=sa.VARCHAR(length=20),
               type_=sa.String(length=50),
               nullable=True)
    op.alter_column('payouts', 'recipient_email',
               existing_type=sa.VARCHAR(length=255),
               nullable=True)
    op.alter_column('payouts', 'recipient_name',
               existing_type=sa.VARCHAR(length=255),
               nullable=True)
    op.alter_column('payouts', 'account_details',
               existing_type=sa.TEXT(),
               nullable=True)
    op.alter_column('payouts', 'failure_reason',
               existing_type=sa.TEXT(),
               nullable=True)
    op.execute("DROP INDEX IF EXISTS idx_payout_status")
    op.execute("DROP INDEX IF EXISTS idx_payout_user_id")
    op.execute("DROP INDEX IF EXISTS idx_payouts_requested_at")
    op.execute("DROP INDEX IF EXISTS idx_payouts_user_status")
    op.execute("ALTER TABLE payouts DROP COLUMN IF EXISTS admin_notes")
    op.execute("ALTER TABLE payouts DROP COLUMN IF EXISTS retry_count")
    op.execute("ALTER TABLE payouts DROP COLUMN IF EXISTS failed_at")

    # --- referrals ---------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_ref_referred_id")
    op.execute("DROP INDEX IF EXISTS idx_ref_referrer_id")
    op.execute("DROP INDEX IF EXISTS idx_referrals_created_at")
    op.execute("DROP INDEX IF EXISTS idx_referrals_referrer_created")
    op.execute("DROP INDEX IF EXISTS idx_referrals_referrer_id")

    # --- reviews -----------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_reviews_user_id")
    op.execute("ALTER TABLE reviews DROP COLUMN IF EXISTS is_attended")

    # --- saved_items -------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_saved_items_user")
    op.execute("DROP INDEX IF EXISTS idx_si_composite")
    op.execute("DROP INDEX IF EXISTS idx_si_user_id")
    op.execute("ALTER TABLE saved_items DROP CONSTRAINT IF EXISTS saved_items_user_id_item_id_item_type_key")
    op.execute("CREATE INDEX IF NOT EXISTS ix_saved_items_id ON saved_items (id)")

    # --- subscriptions -----------------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_sub_created_at")
    op.execute("DROP INDEX IF EXISTS idx_sub_status")
    op.execute("DROP INDEX IF EXISTS idx_sub_user_id")

    # --- user_alerts -------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_ua_alert_id")
    op.execute("DROP INDEX IF EXISTS idx_ua_composite")
    op.execute("DROP INDEX IF EXISTS idx_ua_user_id")

    # --- user_insights -----------------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_ui_insight_id")
    op.execute("DROP INDEX IF EXISTS idx_ui_user_id")

    # --- user_missions -----------------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_um_user_id")

    # --- user_notifications ------------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_un_created_at")
    op.execute("DROP INDEX IF EXISTS idx_un_user_id")
    op.execute("DROP INDEX IF EXISTS idx_user_notifications_user_unread")

    # --- user_pinned_alerts ------------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_upa_alert_id")
    op.execute("DROP INDEX IF EXISTS idx_upa_composite")
    op.execute("DROP INDEX IF EXISTS idx_upa_user_id")

    # --- user_pinned_insights ----------------------------------------------
    op.execute("DROP INDEX IF EXISTS idx_upi_user_id")

    # --- users -------------------------------------------------------------
    # Backfill user_status NULLs before enforcing NOT NULL.
    op.execute("UPDATE users SET user_status = 'active' WHERE user_status IS NULL")
    op.alter_column('users', 'user_status',
               existing_type=sa.VARCHAR(length=20),
               nullable=False,
               existing_server_default=sa.text("'active'::character varying"))
    op.alter_column('users', 'last_login',
               existing_type=postgresql.TIMESTAMP(),
               type_=sa.DateTime(timezone=True),
               existing_nullable=True)
    op.alter_column('users', 'beta_joined_at',
               existing_type=postgresql.TIMESTAMP(),
               type_=sa.DateTime(timezone=True),
               existing_nullable=True)
    op.alter_column('users', 'grace_period_ends_at',
               existing_type=postgresql.TIMESTAMP(),
               type_=sa.DateTime(timezone=True),
               existing_nullable=True)
    op.alter_column('users', 'card_saved_at',
               existing_type=postgresql.TIMESTAMP(),
               type_=sa.DateTime(timezone=True),
               existing_nullable=True)
    op.alter_column('users', 'subscription_expires_at',
               existing_type=postgresql.TIMESTAMP(),
               type_=sa.DateTime(timezone=True),
               existing_nullable=True)
    op.alter_column('users', 'avatar_url',
               existing_type=sa.VARCHAR(length=500),
               type_=sa.Text(),
               existing_nullable=True)
    op.execute("DROP INDEX IF EXISTS idx_users_created_at")
    op.execute("DROP INDEX IF EXISTS idx_users_is_active")
    op.execute("DROP INDEX IF EXISTS idx_users_last_login")
    op.execute("DROP INDEX IF EXISTS idx_users_password_reset_token")
    op.execute("DROP INDEX IF EXISTS idx_users_status")
    op.execute("DROP INDEX IF EXISTS idx_users_stripe_subscription_id")
    op.execute("DROP INDEX IF EXISTS idx_users_sub_status")
    op.execute("DROP INDEX IF EXISTS idx_users_subscription_status")
    op.execute("DROP INDEX IF EXISTS ix_users_google_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS google_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS email_verified")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS stripe_subscription_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS profile_image_url")
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('users', sa.Column('profile_image_url', sa.VARCHAR(length=500), autoincrement=False, nullable=True))
    op.add_column('users', sa.Column('stripe_subscription_id', sa.VARCHAR(length=255), autoincrement=False, nullable=True))
    op.add_column('users', sa.Column('email_verified', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False))
    op.add_column('users', sa.Column('google_id', sa.VARCHAR(length=255), autoincrement=False, nullable=True))
    op.create_index(op.f('ix_users_google_id'), 'users', ['google_id'], unique=True)
    op.create_index(op.f('idx_users_subscription_status'), 'users', ['subscription_status'], unique=False)
    op.create_index(op.f('idx_users_sub_status'), 'users', ['subscription_status'], unique=False)
    op.create_index(op.f('idx_users_stripe_subscription_id'), 'users', ['stripe_subscription_id'], unique=False, postgresql_where='(stripe_subscription_id IS NOT NULL)')
    op.create_index(op.f('idx_users_status'), 'users', ['user_status'], unique=False)
    op.create_index(op.f('idx_users_password_reset_token'), 'users', ['password_reset_token'], unique=True)
    op.create_index(op.f('idx_users_last_login'), 'users', ['last_login'], unique=False)
    op.create_index(op.f('idx_users_is_active'), 'users', ['is_active'], unique=False)
    op.create_index(op.f('idx_users_created_at'), 'users', [sa.literal_column('created_at DESC')], unique=False)
    op.alter_column('users', 'avatar_url',
               existing_type=sa.Text(),
               type_=sa.VARCHAR(length=500),
               existing_nullable=True)
    op.alter_column('users', 'subscription_expires_at',
               existing_type=sa.DateTime(timezone=True),
               type_=postgresql.TIMESTAMP(),
               existing_nullable=True)
    op.alter_column('users', 'card_saved_at',
               existing_type=sa.DateTime(timezone=True),
               type_=postgresql.TIMESTAMP(),
               existing_nullable=True)
    op.alter_column('users', 'grace_period_ends_at',
               existing_type=sa.DateTime(timezone=True),
               type_=postgresql.TIMESTAMP(),
               existing_nullable=True)
    op.alter_column('users', 'beta_joined_at',
               existing_type=sa.DateTime(timezone=True),
               type_=postgresql.TIMESTAMP(),
               existing_nullable=True)
    op.alter_column('users', 'last_login',
               existing_type=sa.DateTime(timezone=True),
               type_=postgresql.TIMESTAMP(),
               existing_nullable=True)
    op.alter_column('users', 'user_status',
               existing_type=sa.VARCHAR(length=20),
               nullable=True,
               existing_server_default=sa.text("'active'::character varying"))
    op.create_index(op.f('idx_upi_user_id'), 'user_pinned_insights', ['user_id'], unique=False)
    op.create_index(op.f('idx_upa_user_id'), 'user_pinned_alerts', ['user_id'], unique=False)
    op.create_index(op.f('idx_upa_composite'), 'user_pinned_alerts', ['user_id', 'alert_id'], unique=False)
    op.create_index(op.f('idx_upa_alert_id'), 'user_pinned_alerts', ['alert_id'], unique=False)
    op.create_index(op.f('idx_user_notifications_user_unread'), 'user_notifications', ['user_id', 'is_read'], unique=False)
    op.create_index(op.f('idx_un_user_id'), 'user_notifications', ['user_id'], unique=False)
    op.create_index(op.f('idx_un_created_at'), 'user_notifications', [sa.literal_column('created_at DESC')], unique=False)
    op.create_index(op.f('idx_um_user_id'), 'user_missions', ['user_id'], unique=False)
    op.create_index(op.f('idx_ui_user_id'), 'user_insights', ['user_id'], unique=False)
    op.create_index(op.f('idx_ui_insight_id'), 'user_insights', ['insight_id'], unique=False)
    op.create_index(op.f('idx_ua_user_id'), 'user_alerts', ['user_id'], unique=False)
    op.create_index(op.f('idx_ua_composite'), 'user_alerts', ['user_id', 'alert_id'], unique=False)
    op.create_index(op.f('idx_ua_alert_id'), 'user_alerts', ['alert_id'], unique=False)
    op.create_index(op.f('idx_sub_user_id'), 'subscriptions', ['user_id'], unique=False)
    op.create_index(op.f('idx_sub_status'), 'subscriptions', ['subscription_status'], unique=False)
    op.create_index(op.f('idx_sub_created_at'), 'subscriptions', [sa.literal_column('created_at DESC')], unique=False)
    op.drop_index(op.f('ix_saved_items_id'), table_name='saved_items')
    op.create_unique_constraint(op.f('saved_items_user_id_item_id_item_type_key'), 'saved_items', ['user_id', 'item_id', 'item_type'])
    op.create_index(op.f('idx_si_user_id'), 'saved_items', ['user_id'], unique=False)
    op.create_index(op.f('idx_si_composite'), 'saved_items', ['user_id', 'item_type'], unique=False)
    op.create_index(op.f('idx_saved_items_user'), 'saved_items', ['user_id'], unique=False)
    op.add_column('reviews', sa.Column('is_attended', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=True))
    op.create_index(op.f('idx_reviews_user_id'), 'reviews', ['user_id'], unique=False)
    op.create_index(op.f('idx_referrals_referrer_id'), 'referrals', ['referrer_id'], unique=False)
    op.create_index(op.f('idx_referrals_referrer_created'), 'referrals', ['referrer_id', 'created_at'], unique=False)
    op.create_index(op.f('idx_referrals_created_at'), 'referrals', ['created_at'], unique=False)
    op.create_index(op.f('idx_ref_referrer_id'), 'referrals', ['referrer_id'], unique=False)
    op.create_index(op.f('idx_ref_referred_id'), 'referrals', ['referred_user_id'], unique=False)
    op.add_column('payouts', sa.Column('failed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True))
    op.add_column('payouts', sa.Column('retry_count', sa.INTEGER(), autoincrement=False, nullable=True))
    op.add_column('payouts', sa.Column('admin_notes', sa.TEXT(), autoincrement=False, nullable=True))
    op.create_index(op.f('idx_payouts_user_status'), 'payouts', ['user_id', 'status'], unique=False)
    op.create_index(op.f('idx_payouts_requested_at'), 'payouts', ['requested_at'], unique=False)
    op.create_index(op.f('idx_payout_user_id'), 'payouts', ['user_id'], unique=False)
    op.create_index(op.f('idx_payout_status'), 'payouts', ['status'], unique=False)
    op.alter_column('payouts', 'failure_reason',
               existing_type=sa.TEXT(),
               nullable=False)
    op.alter_column('payouts', 'account_details',
               existing_type=sa.TEXT(),
               nullable=False)
    op.alter_column('payouts', 'recipient_name',
               existing_type=sa.VARCHAR(length=255),
               nullable=False)
    op.alter_column('payouts', 'recipient_email',
               existing_type=sa.VARCHAR(length=255),
               nullable=False)
    op.alter_column('payouts', 'payment_method',
               existing_type=sa.String(length=50),
               type_=sa.VARCHAR(length=20),
               nullable=False)
    op.alter_column('payouts', 'currency',
               existing_type=sa.VARCHAR(length=10),
               nullable=False)
    op.create_index(op.f('idx_pa_user_id'), 'payout_accounts', ['user_id'], unique=False)
    op.alter_column('payout_accounts', 'account_number',
               existing_type=sa.String(length=100),
               type_=sa.VARCHAR(length=50),
               existing_nullable=True)
    op.alter_column('payout_accounts', 'default_payout_method',
               existing_type=sa.String(length=50),
               type_=sa.VARCHAR(length=20),
               nullable=True)
    op.create_index(op.f('idx_mission_steps_mission_id'), 'mission_steps', ['mission_id'], unique=False)
    op.create_index(op.f('idx_mp_user_id'), 'marketplace_purchases', ['user_id'], unique=False)
    op.create_index(op.f('idx_mp_tool_id'), 'marketplace_purchases', ['tool_id'], unique=False)
    op.create_index(op.f('idx_mp_status'), 'marketplace_purchases', ['status'], unique=False)
    op.create_index(op.f('idx_insights_is_active'), 'insights', ['is_active'], unique=False)
    op.create_index(op.f('idx_insights_created_at'), 'insights', [sa.literal_column('created_at DESC')], unique=False)
    op.drop_index(op.f('ix_event_registrations_id'), table_name='event_registrations')
    op.create_unique_constraint(op.f('event_registrations_user_id_event_id_key'), 'event_registrations', ['user_id', 'event_id'])
    op.drop_index(op.f('ix_discussion_replies_id'), table_name='discussion_replies')
    op.create_index(op.f('idx_dr_discussion_id'), 'discussion_replies', ['discussion_id'], unique=False)
    op.drop_index(op.f('ix_discussion_likes_id'), table_name='discussion_likes')
    op.create_index(op.f('idx_dl_user_id'), 'discussion_likes', ['user_id'], unique=False)
    op.create_unique_constraint(op.f('discussion_likes_user_id_discussion_id_key'), 'discussion_likes', ['user_id', 'discussion_id'])
    op.create_index(op.f('idx_conv_review_id'), 'conversations', ['review_id'], unique=False)
    op.create_index(op.f('idx_conv_is_read'), 'conversations', ['is_read'], unique=False)
    op.drop_index(op.f('ix_community_events_id'), table_name='community_events')
    op.drop_index(op.f('ix_community_discussions_id'), table_name='community_discussions')
    op.create_index(op.f('idx_discussions_user'), 'community_discussions', ['user_id'], unique=False)
    op.create_index(op.f('idx_discussions_channel'), 'community_discussions', ['channel_id'], unique=False)
    op.create_index(op.f('idx_cd_user_id'), 'community_discussions', ['user_id'], unique=False)
    op.create_index(op.f('idx_cd_channel_id'), 'community_discussions', ['channel_id'], unique=False)
    op.alter_column('community_discussions', 'tags',
               existing_type=sa.JSON(),
               type_=postgresql.JSONB(astext_type=sa.Text()),
               existing_nullable=True)
    op.drop_index(op.f('ix_community_channels_slug'), table_name='community_channels')
    op.drop_index(op.f('ix_community_channels_id'), table_name='community_channels')
    op.create_unique_constraint(op.f('community_channels_slug_key'), 'community_channels', ['slug'])
    op.alter_column('community_channels', 'icon',
               existing_type=sa.String(length=20),
               type_=sa.VARCHAR(length=10),
               existing_nullable=True)
    op.drop_index(op.f('ix_community_activities_id'), table_name='community_activities')
    op.create_index(op.f('idx_community_activities_user'), 'community_activities', ['user_id'], unique=False)
    op.create_foreign_key(op.f('commissions_payout_id_fkey'), 'commissions', 'payouts', ['payout_id'], ['id'])
    op.create_index(op.f('idx_commissions_user_status'), 'commissions', ['user_id', 'status'], unique=False)
    op.create_index(op.f('idx_commissions_created_at'), 'commissions', ['created_at'], unique=False)
    op.create_index(op.f('idx_com_user_id'), 'commissions', ['user_id'], unique=False)
    op.create_index(op.f('idx_com_sub_id'), 'commissions', ['subscription_id'], unique=False)
    op.create_index(op.f('idx_com_status'), 'commissions', ['status'], unique=False)
    op.create_index(op.f('idx_com_referred_id'), 'commissions', ['referred_user_id'], unique=False)
    op.create_index(op.f('idx_com_created_at'), 'commissions', [sa.literal_column('created_at DESC')], unique=False)
    op.alter_column('commissions', 'currency',
               existing_type=sa.VARCHAR(length=10),
               nullable=False)
    op.alter_column('commissions', 'original_amount',
               existing_type=sa.NUMERIC(precision=10, scale=2),
               nullable=False)
    op.alter_column('commissions', 'subscription_id',
               existing_type=sa.INTEGER(),
               nullable=False)
    op.alter_column('commissions', 'referred_user_id',
               existing_type=sa.INTEGER(),
               nullable=False)
    op.create_index(op.f('idx_cs_user_id'), 'commission_summaries', ['user_id'], unique=False)
    op.drop_index(op.f('ix_channel_members_id'), table_name='channel_members')
    op.create_index(op.f('idx_cm_user_id'), 'channel_members', ['user_id'], unique=False)
    op.create_index(op.f('idx_cm_channel_id'), 'channel_members', ['channel_id'], unique=False)
    op.create_index(op.f('idx_channel_members_user'), 'channel_members', ['user_id'], unique=False)
    op.create_index(op.f('idx_channel_members_channel'), 'channel_members', ['channel_id'], unique=False)
    op.create_unique_constraint(op.f('channel_members_user_id_channel_id_key'), 'channel_members', ['user_id', 'channel_id'])
    op.create_index(op.f('idx_business_analyses_status'), 'business_analyses', ['status'], unique=False)
    op.create_index(op.f('idx_business_analyses_created_at'), 'business_analyses', ['created_at'], unique=False)
    op.create_index(op.f('idx_business_analyses_analysis_type'), 'business_analyses', ['analysis_type'], unique=False)
    op.create_index(op.f('idx_ba_user_id'), 'business_analyses', ['user_id'], unique=False)
    op.create_index(op.f('idx_ba_created_at'), 'business_analyses', [sa.literal_column('created_at DESC')], unique=False)
    op.drop_column('business_analyses', 'single_tool_recommendation')
    op.drop_column('business_analyses', 'recommendation_mode')
    op.create_index(op.f('idx_alerts_is_active'), 'alerts', ['is_active'], unique=False)
    op.create_index(op.f('idx_alerts_created_at'), 'alerts', [sa.literal_column('created_at DESC')], unique=False)
    op.add_column('ai_tools', sa.Column('embedding', sa.NullType(), autoincrement=False, nullable=True))
    op.create_index(op.f('ai_tools_embedding_hnsw_idx'), 'ai_tools', ['embedding'], unique=False, postgresql_with={'m': '16', 'ef_construction': '64'}, postgresql_using='hnsw')
    op.create_table('tool_combinations',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('analysis_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('combo_name', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('tools', postgresql.JSON(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('synergy_score', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('integration_flow', postgresql.JSON(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('setup_difficulty', sa.VARCHAR(length=50), autoincrement=False, nullable=True),
    sa.Column('total_monthly_cost', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('why_this_combo', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('expected_outcome', sa.TEXT(), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['analysis_id'], ['business_analyses.id'], name=op.f('tool_combinations_analysis_id_fkey')),
    sa.PrimaryKeyConstraint('id', name=op.f('tool_combinations_pkey'))
    )
    op.create_table('performance_metrics',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('endpoint', sa.VARCHAR(length=255), autoincrement=False, nullable=False),
    sa.Column('method', sa.VARCHAR(length=10), autoincrement=False, nullable=False),
    sa.Column('status_code', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('response_time', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('request_size', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('response_size', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('user_id', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('ip_address', sa.VARCHAR(length=45), autoincrement=False, nullable=True),
    sa.Column('user_agent', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('timestamp', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('performance_metrics_pkey'))
    )
    op.create_index(op.f('ix_performance_metrics_id'), 'performance_metrics', ['id'], unique=False)
    op.create_index(op.f('idx_performance_timestamp'), 'performance_metrics', [sa.literal_column('timestamp DESC')], unique=False)
    op.create_index(op.f('idx_performance_status'), 'performance_metrics', ['status_code'], unique=False)
    op.create_index(op.f('idx_performance_endpoint'), 'performance_metrics', ['endpoint'], unique=False)
    op.create_table('waitlist',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('email', sa.VARCHAR(length=255), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('status', sa.VARCHAR(length=50), server_default=sa.text("'success'::character varying"), autoincrement=False, nullable=False),
    sa.Column('mailerlite_subscriber_id', sa.VARCHAR(length=100), autoincrement=False, nullable=True),
    sa.Column('mailerlite_sync_status', sa.VARCHAR(length=50), server_default=sa.text("'pending'::character varying"), autoincrement=False, nullable=True),
    sa.Column('updated_at', postgresql.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), autoincrement=False, nullable=True),
    sa.Column('referrer_code', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('referral_code', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('mailerlite_sync_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('referral_count', sa.INTEGER(), server_default=sa.text('0'), autoincrement=False, nullable=False),
    sa.Column('last_checked_position', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('wave', sa.INTEGER(), server_default=sa.text('1'), autoincrement=False, nullable=False),
    sa.Column('name', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('referral_source', sa.VARCHAR(length=100), autoincrement=False, nullable=True),
    sa.Column('mailerlite_synced_at', postgresql.TIMESTAMP(), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('waitlist_pkey'))
    )
    op.create_index(op.f('ix_waitlist_referral_code'), 'waitlist', ['referral_code'], unique=True)
    op.create_index(op.f('ix_waitlist_id'), 'waitlist', ['id'], unique=False)
    op.create_index(op.f('ix_waitlist_email'), 'waitlist', ['email'], unique=True)
    op.create_index(op.f('idx_waitlist_status'), 'waitlist', ['status'], unique=False)
    op.create_index(op.f('idx_waitlist_email'), 'waitlist', ['email'], unique=False)
    op.create_index(op.f('idx_waitlist_created_at'), 'waitlist', ['created_at'], unique=False)
    op.create_table('database_operations',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('operation_type', sa.VARCHAR(length=50), autoincrement=False, nullable=False),
    sa.Column('table_name', sa.VARCHAR(length=100), autoincrement=False, nullable=False),
    sa.Column('duration', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('rows_affected', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('success', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('error_message', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('query_hash', sa.VARCHAR(length=64), autoincrement=False, nullable=True),
    sa.Column('timestamp', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('database_operations_pkey'))
    )
    op.create_index(op.f('ix_database_operations_id'), 'database_operations', ['id'], unique=False)
    op.create_index(op.f('idx_db_ops_timestamp'), 'database_operations', [sa.literal_column('timestamp DESC')], unique=False)
    op.create_index(op.f('idx_db_ops_table'), 'database_operations', ['table_name'], unique=False)
    op.create_index(op.f('idx_db_ops_duration'), 'database_operations', [sa.literal_column('duration DESC')], unique=False)
    op.create_table('api_usage',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('endpoint', sa.VARCHAR(length=255), autoincrement=False, nullable=False),
    sa.Column('method', sa.VARCHAR(length=10), autoincrement=False, nullable=False),
    sa.Column('user_id', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('api_key_id', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('request_count', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('total_response_time', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('avg_response_time', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('error_count', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('date', sa.DATE(), autoincrement=False, nullable=False),
    sa.Column('hour', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('api_usage_pkey')),
    sa.UniqueConstraint('endpoint', 'method', 'user_id', 'date', 'hour', name=op.f('idx_api_usage_unique'))
    )
    op.create_index(op.f('ix_api_usage_id'), 'api_usage', ['id'], unique=False)
    op.create_index(op.f('idx_api_usage_user'), 'api_usage', ['user_id'], unique=False)
    op.create_index(op.f('idx_api_usage_endpoint'), 'api_usage', ['endpoint'], unique=False)
    op.create_index(op.f('idx_api_usage_date'), 'api_usage', [sa.literal_column('date DESC')], unique=False)
    op.create_table('roadmap_stages',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('analysis_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('stage_number', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('stage_name', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('duration_weeks', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('tasks', postgresql.JSON(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('deliverables', postgresql.JSON(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('metrics', postgresql.JSON(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('cost_this_stage', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['analysis_id'], ['business_analyses.id'], name=op.f('roadmap_stages_analysis_id_fkey')),
    sa.PrimaryKeyConstraint('id', name=op.f('roadmap_stages_pkey'))
    )
    op.create_index(op.f('ix_roadmap_stages_id'), 'roadmap_stages', ['id'], unique=False)
    op.create_table('uptime_records',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('date', sa.DATE(), autoincrement=False, nullable=False),
    sa.Column('total_uptime_seconds', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('total_downtime_seconds', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('uptime_percentage', sa.NUMERIC(precision=5, scale=2), autoincrement=False, nullable=False),
    sa.Column('incidents_count', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('uptime_records_pkey'))
    )
    op.create_index(op.f('ix_uptime_records_id'), 'uptime_records', ['id'], unique=False)
    op.create_index(op.f('idx_uptime_date_unique'), 'uptime_records', ['date'], unique=True)
    op.create_index(op.f('idx_uptime_date'), 'uptime_records', [sa.literal_column('date DESC')], unique=False)
    op.create_table('system_alerts',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('alert_id', sa.VARCHAR(length=255), autoincrement=False, nullable=False),
    sa.Column('type', sa.VARCHAR(length=20), autoincrement=False, nullable=False),
    sa.Column('service', sa.VARCHAR(length=100), autoincrement=False, nullable=False),
    sa.Column('message', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('status', sa.VARCHAR(length=20), autoincrement=False, nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('timestamp', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.Column('resolved_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('resolved_by', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('system_alerts_pkey')),
    sa.UniqueConstraint('alert_id', name=op.f('system_alerts_alert_id_key'))
    )
    op.create_index(op.f('ix_system_alerts_id'), 'system_alerts', ['id'], unique=False)
    op.create_index(op.f('idx_system_alerts_type'), 'system_alerts', ['type'], unique=False)
    op.create_index(op.f('idx_system_alerts_timestamp'), 'system_alerts', ['timestamp'], unique=False)
    op.create_index(op.f('idx_system_alerts_status'), 'system_alerts', ['status'], unique=False)
    op.create_index(op.f('idx_system_alerts_service'), 'system_alerts', ['service'], unique=False)
    op.create_table('activity_logs',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('service', sa.VARCHAR(length=100), autoincrement=False, nullable=False),
    sa.Column('operation', sa.VARCHAR(length=100), autoincrement=False, nullable=False),
    sa.Column('duration', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('success', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('user_id', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('ip_address', sa.VARCHAR(length=45), autoincrement=False, nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('error_message', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('activity_logs_pkey'))
    )
    op.create_index(op.f('ix_activity_logs_id'), 'activity_logs', ['id'], unique=False)
    op.create_index(op.f('idx_activity_logs_success'), 'activity_logs', ['success'], unique=False)
    op.create_index(op.f('idx_activity_logs_service'), 'activity_logs', ['service'], unique=False)
    op.create_index(op.f('idx_activity_logs_operation'), 'activity_logs', ['operation'], unique=False)
    op.create_index(op.f('idx_activity_logs_created'), 'activity_logs', [sa.literal_column('created_at DESC')], unique=False)
    op.create_table('service_health',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('service_name', sa.VARCHAR(length=100), autoincrement=False, nullable=False),
    sa.Column('status', sa.VARCHAR(length=20), autoincrement=False, nullable=False),
    sa.Column('uptime', sa.NUMERIC(precision=5, scale=2), autoincrement=False, nullable=True),
    sa.Column('response_time', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('error_count', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('last_error', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('last_check', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('service_health_pkey'))
    )
    op.create_index(op.f('ix_service_health_id'), 'service_health', ['id'], unique=False)
    op.create_index(op.f('idx_service_health_status'), 'service_health', ['status'], unique=False)
    op.create_index(op.f('idx_service_health_service'), 'service_health', ['service_name'], unique=False)
    op.create_index(op.f('idx_service_health_created'), 'service_health', [sa.literal_column('created_at DESC')], unique=False)
    op.create_table('error_tracking',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('error_type', sa.VARCHAR(length=100), autoincrement=False, nullable=True),
    sa.Column('error_message', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('stack_trace', sa.TEXT(), autoincrement=False, nullable=True),
    sa.Column('service', sa.VARCHAR(length=100), autoincrement=False, nullable=True),
    sa.Column('endpoint', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('method', sa.VARCHAR(length=10), autoincrement=False, nullable=True),
    sa.Column('user_id', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('ip_address', sa.VARCHAR(length=45), autoincrement=False, nullable=True),
    sa.Column('request_body', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
    sa.Column('environment', sa.VARCHAR(length=50), autoincrement=False, nullable=True),
    sa.Column('severity', sa.VARCHAR(length=20), autoincrement=False, nullable=True),
    sa.Column('sentry_issue_id', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('first_seen', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.Column('last_seen', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.Column('occurrence_count', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('resolved', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('resolved_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('error_tracking_pkey'))
    )
    op.create_index(op.f('ix_error_tracking_id'), 'error_tracking', ['id'], unique=False)
    op.create_index(op.f('idx_errors_type'), 'error_tracking', ['error_type'], unique=False)
    op.create_index(op.f('idx_errors_severity'), 'error_tracking', ['severity'], unique=False)
    op.create_index(op.f('idx_errors_service'), 'error_tracking', ['service'], unique=False)
    op.create_index(op.f('idx_errors_resolved'), 'error_tracking', ['resolved'], unique=False)
    op.create_index(op.f('idx_errors_last_seen'), 'error_tracking', [sa.literal_column('last_seen DESC')], unique=False)
    op.create_table('system_metrics',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('timestamp', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.Column('uptime', sa.NUMERIC(precision=10, scale=2), autoincrement=False, nullable=True),
    sa.Column('uptime_percentage', sa.NUMERIC(precision=5, scale=2), autoincrement=False, nullable=True),
    sa.Column('cpu_usage', sa.NUMERIC(precision=5, scale=2), autoincrement=False, nullable=True),
    sa.Column('memory_total_mb', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('memory_used_mb', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('memory_percentage', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('load_average_1m', sa.NUMERIC(precision=5, scale=2), autoincrement=False, nullable=True),
    sa.Column('load_average_5m', sa.NUMERIC(precision=5, scale=2), autoincrement=False, nullable=True),
    sa.Column('load_average_15m', sa.NUMERIC(precision=5, scale=2), autoincrement=False, nullable=True),
    sa.Column('avg_response_time', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('requests_per_minute', sa.INTEGER(), autoincrement=False, nullable=True),
    sa.Column('error_rate', sa.NUMERIC(precision=5, scale=2), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('system_metrics_pkey'))
    )
    op.create_index(op.f('ix_system_metrics_timestamp'), 'system_metrics', ['timestamp'], unique=False)
    op.create_index(op.f('ix_system_metrics_id'), 'system_metrics', ['id'], unique=False)
    op.execute("DROP TABLE IF EXISTS security_metrics_summary")
    # ### end Alembic commands ###
