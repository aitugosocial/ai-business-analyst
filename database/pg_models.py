# db/pg_models.py
"""
PostgreSQL database models - works with both PostgreSQL and SQLite.
This file contains all ORM models for the application.
"""

from pydantic import BaseModel, ConfigDict, EmailStr
from sqlalchemy import Column, DateTime, Float, Integer, String, Text, ForeignKey, JSON, Boolean, DECIMAL, Enum, Numeric, Index
from sqlalchemy.dialects.postgresql import UUID, INET, JSONB
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship, synonym
from sqlalchemy.sql import func
from sqlalchemy.sql.sqltypes import VARCHAR
from datetime import datetime, timezone
from decimal import Decimal

from .pg_connections import Base

import enum
from uuid import uuid4
from typing import Optional, List, Dict

class User(Base):
    """
    User model for authentication and user management.
    Stores user account information.
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password = Column(String(255), nullable=False)  # Hashed password
    confirm_password = Column(String(255), nullable=False)  # For validation
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Password reset fields
    password_reset_token = Column(String(255), nullable=True, unique=True)
    password_reset_expires = Column(DateTime(timezone=True), nullable=True)
    password_reset_used_at = Column(DateTime(timezone=True), nullable=True)

    # Chops system (Clinton's feature)
    total_chops = Column(Integer, default=0)
    alert_reading_chops = Column(Integer, default=0)
    alert_sharing_chops = Column(Integer, default=0)
    insight_reading_chops = Column(Integer, default=0)
    insight_sharing_chops = Column(Integer, default=0)
    referral_chops = Column(Integer, default=0)
    referral_count = Column(Integer, default=0)

    # Admin and subscription
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    user_status = Column(String(20), server_default="active", nullable=False)
    last_login = Column(DateTime(timezone=True), nullable=True)
    subscription_status = Column(String, default="Free")
    subscription_plan = Column(String, nullable=True)

    # Referral system (Clinton's feature)
    referral_code = Column(String, unique=True, index=True)
    referrer_code = Column(String, nullable=True)

    # User profile fields
    department = Column(String(100), nullable=True)
    location = Column(String(100), nullable=True)
    bio = Column(Text, nullable=True)
    two_factor_enabled = Column(Boolean, default=False)
    email_notifications = Column(Boolean, default=True)

    # Beta and Stripe Columns
    is_beta_user = Column(Boolean, default=False)
    beta_joined_at = Column(DateTime(timezone=True), nullable=True)
    grace_period_ends_at = Column(DateTime(timezone=True), nullable=True)
    stripe_customer_id = Column(String(255), nullable=True)
    stripe_payment_method_id = Column(String(255), nullable=True)
    card_last4 = Column(String(4), nullable=True)
    card_brand = Column(String(50), nullable=True)
    card_exp_month = Column(Integer, nullable=True)
    card_exp_year = Column(Integer, nullable=True)
    card_saved_at = Column(DateTime(timezone=True), nullable=True)
    subscription_expires_at = Column(DateTime(timezone=True), nullable=True)

    # Business profile fields
    company_name = Column(String(255), nullable=True)
    industry = Column(String(100), nullable=True)
    avatar_url = Column(Text, nullable=True)

    # User Settings and Metadata
    user_metadata = Column(JSON, nullable=True)  # Stores user settings, preferences, and other metadata

    # Relationships
    subscriptions = relationship("Subscriptions", back_populates="user")
    tickets = relationship("Ticket", back_populates="user")
    user_alerts = relationship("UserAlert", back_populates="user")
    user_insights = relationship("UserInsight", back_populates="user")
    pinned_insights = relationship("UserPinnedInsight", back_populates="user")
    pinned_alerts = relationship("UserPinnedAlert", back_populates="user")
    referrals = relationship("Referral", foreign_keys="Referral.referrer_id", back_populates="referrer")
    referred_by = relationship("Referral", foreign_keys="Referral.referred_user_id", back_populates="referred_user")
    commissions_earned = relationship("Commission", foreign_keys="Commission.user_id", back_populates="user")
    payouts = relationship("Payout", back_populates="user")
    payout_account = relationship("PayoutAccount", back_populates="user", uselist=False)
    user_missions = relationship("UserMission", cascade="all, delete-orphan")
    settings = relationship("UserSettings", uselist=False, cascade="all, delete-orphan")



class AITool(Base):
    """
    AI Tool model for storing the catalog of AI tools.
    This replaces the CSV file with database storage.
    """

    __tablename__ = "ai_tools"

    id = Column(Integer, primary_key=True, index=True)

    # Basic Information
    name = Column(String(255), unique=True, index=True, nullable=False)
    url = Column(String(500))
    description = Column(Text, nullable=False)
    summary = Column(Text)

    # Categorization
    main_category = Column(String(255), index=True)
    sub_category = Column(String(255), index=True)
    ai_categories = Column(Text)  # JSON string of categories

    # Pricing and Ratings
    pricing = Column(Text)
    ratings = Column(Float, default=0.0)

    # Features
    key_features = Column(Text)  # JSON string of features
    pros = Column(Text)  # Pipe-separated list
    cons = Column(Text)  # Pipe-separated list

    # Usage Information
    who_should_use = Column(Text)  # JSON string of use cases
    compatibility_integration = Column(Text)  # JSON string of integrations

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class BusinessAnalysis(Base):
    """
    Stores complete business analysis results from AI analyzer.
    NEW SCHEMA (redesigned 2026-01-14):
    - Primary bottleneck with consequences
    - Secondary constraints
    - What to stop
    - Strategic priority
    - Ranked action plans with toolkits
    - Execution roadmap with timeline
    - Exclusions note
    - LLM-generated motivational quote
    """
    __tablename__ = "business_analyses"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Original user input
    business_goal = Column(Text, nullable=False)  # Original user query

    # NEW UNIFIED SCHEMA
    primary_bottleneck = Column(JSON, nullable=True)  # {title, description, consequence}
    secondary_constraints = Column(JSON, nullable=True)  # [{id, title, description}]
    what_to_stop = Column(Text, nullable=True)  # Critical action to discontinue
    strategic_priority = Column(Text, nullable=True)  # Main strategic focus
    action_plans = Column(JSON, nullable=True)  # [{id, title, what_to_do, why_it_matters, effort_level, toolkit}]
    recommended_tool_stacks = Column(JSON, nullable=True)  # [{stack_id, stack_name, tools, setup_order, automation_logic, ...}]
    total_phases = Column(Integer, nullable=True)  # Number of delivery phases
    estimated_days = Column(Integer, nullable=True)  # Total days for execution
    execution_roadmap = Column(JSON, nullable=True)  # [{phase, days, title, tasks}]
    exclusions_note = Column(Text, nullable=True)  # What was excluded and why
    motivational_quote = Column(Text, nullable=True)  # LLM-generated quote

    # Admin Monitoring Fields
    confidence_score = Column(Integer, nullable=True)  # 0-100 confidence score
    duration = Column(String(50), nullable=True)  # e.g., "2.5s"
    analysis_type = Column(String(100), nullable=True)  # agentic
    insights_count = Column(Integer, default=0)  # Number of insights generated
    recommendations_count = Column(Integer, default=0)  # Number of recommendations

    # User Progress Tracking (for missions system)
    user_progress = Column(JSON, nullable=True)  # {completed_actions: [], reflections: {}, resolved_constraints: [], completion_dates: {}}

    # Metadata
    status = Column(String(50), default="completed")  # pending, completed, failed
    ai_model_used = Column(String(100), default="grok-4-1-fast-reasoning")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    user = relationship("User", backref="business_analyses")


# Pydantic models for API validation and serialization


class ShowUser(BaseModel):
    """Pydantic model for user login request"""

    email: str
    password: str


class SaveCardRequest(BaseModel):
    payment_method_id: str
    currency: Optional[str] = "USD"


class UserResponse(BaseModel):
    """Pydantic model for user response"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    subscription_status: str
    total_chops: int
    alert_reading_chops: int
    alert_sharing_chops: int
    insight_reading_chops: int
    insight_sharing_chops: int
    referral_chops: int
    referral_count: int
    referral_code: Optional[str] = None
    # Add new fields
    is_beta_user: Optional[bool] = False
    subscription_plan: Optional[str] = None
    subscription_expires_at: Optional[datetime] = None
    stripe_customer_id: Optional[str] = None
    stripe_payment_method_id: Optional[str] = None
    card_last4: Optional[str] = None
    card_brand: Optional[str] = None
    card_exp_month: Optional[int] = None
    card_exp_year: Optional[int] = None


class AIToolBase(BaseModel):
    """Base Pydantic model for AI Tool"""

    name: str
    description: str
    main_category: str | None = None
    sub_category: str | None = None
    pricing: str | None = None
    ratings: float | None = 0.0
    url: str | None = None


class AIToolCreate(AIToolBase):
    """Pydantic model for creating AI Tool"""

    key_features: str | None = None
    pros: str | None = None
    cons: str | None = None
    who_should_use: str | None = None
    compatibility_integration: str | None = None


class AIToolResponse(AIToolBase):
    """Pydantic model for AI Tool response"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    key_features: str | None = None
    pros: str | None = None
    cons: str | None = None
    who_should_use: str | None = None
    compatibility_integration: str | None = None


class ToolRecommendation(BaseModel):
    """Pydantic model for tool recommendation response"""

    tool_name: str
    similarity_score: float
    description: str


class BusinessAnalysisRequest(BaseModel):
    """Request model for business analysis"""
    business_goal: str  # User's goal (e.g., "Grow AI newsletter to 10k subs")


class IntentAnalysis(BaseModel):
    """Parsed intent from user goal"""
    objective: str
    capabilities_needed: list[str]
    stages: list[str]
    success_metrics: list[str]


class ToolComboResponse(BaseModel):
    """Single tool combination recommendation"""
    combo_name: str
    tools: list[dict]  # [{id, name, pricing}]
    synergy_score: float
    integration_flow: dict
    setup_difficulty: str
    total_monthly_cost: float
    why_this_combo: str
    expected_outcome: str


class RoadmapStageResponse(BaseModel):
    """Single roadmap stage"""
    stage_number: int
    stage_name: str
    duration_weeks: int
    tasks: list[str]
    deliverables: list[str]
    metrics: list[str]
    cost_this_stage: float


class BusinessAnalysisResponse(BaseModel):
    """Complete business analysis response"""
    analysis_id: int
    business_goal: str
    intent_analysis: IntentAnalysis
    tool_combinations: list[ToolComboResponse]
    roadmap: list[RoadmapStageResponse]
    estimated_cost: float
    timeline_weeks: int
    created_at: str

class AuthResponse(BaseModel):
    """Pydantic model for authentication token response"""
    access_token: str
    token_type: str
    id: int
    name: str
    email: str
    role: str
    subscription_status: str | None = None
    subscription_plan: str | None = None
    referral_code: str | None = None
    department: str | None = None
    location: str | None = None
    bio: str | None = None
    two_factor_enabled: bool | None = None
    email_notifications: bool | None = None
    created_at: datetime | None = None
    # Add new fields
    is_beta_user: bool | None = False
    subscription_expires_at: datetime | None = None
    stripe_customer_id: str | None = None
    stripe_payment_method_id: str | None = None
    card_last4: str | None = None
    card_brand: str | None = None
    card_exp_month: int | None = None
    card_exp_year: int | None = None


# Paypal payment gateway
class CreateOrderRequest(BaseModel):
    amount: str    # e.g. "29.00"
    currency: str = "USD"

class CaptureRequest(BaseModel):
    order_id: str

# Subcriptions table
class Subscriptions(Base):
    """
    Contains information about subscription payments made by customers
    """

    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    subscription_plan = Column(VARCHAR(50) )
    transaction_id = Column(String(255), nullable=False, unique=True, index=True)
    tx_ref = Column(String, unique=True, index=True, nullable=False)
    amount = Column(DECIMAL(10, 2), nullable=False)
    currency = Column(VARCHAR(10), nullable=False)
    status = Column(VARCHAR(20), nullable=False) # Original payment status
    subscription_status = Column(VARCHAR(20), nullable=True) # Lifecycle status: active, expired, Payment failed
    payment_provider = Column(VARCHAR(20), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    start_date = Column(DateTime(timezone=True), nullable=False)
    end_date = Column(DateTime(timezone=True), nullable=False)

    user = relationship("User", back_populates="subscriptions")
    commission = relationship("Commission", back_populates="subscription", uselist=False)

class NotificationType(enum.Enum):
    PAYMENT_SUCCESS = "payment_success"
    PAYMENT_FAILED = "payment_failed"
    COMMISSION_EARNED = "commission_earned"
    REFERRAL_REGISTERED = "referral_registered"
    PAYOUT_COMPLETED = "payout_completed"
    SYSTEM_ALERT = "system_alert"

class UserNotification(Base):
    """
    Stores individual notifications for users (payments, commissions, etc.)
    """
    __tablename__ = "user_notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    type = Column(String(50), nullable=False) # Maps to NotificationType
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    link = Column(String(255), nullable=True) # Optional link to relevant page
    is_read = Column(Boolean, default=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", backref="notifications")


class NotificationHistory(Base):
    """
    Tracks when specific notifications were sent to avoid spam/repetition.
    """
    __tablename__ = "notification_history"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    notification_type = Column(String(50), nullable=False)
    sent_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index('idx_notification_history_user_type_sent', 'user_id', 'notification_type', 'sent_at'),
    )


class CreateSubscriptionRequest(BaseModel):
    payment_method_id: str
    plan_type: str  # 'monthly' or 'yearly'
    currency: Optional[str] = "USD"
    billing_details: Optional[Dict] = None


class ConfirmSubscriptionRequest(BaseModel):
    subscription_id: str
    payment_intent_id: str


class UpdatePaymentMethodRequest(BaseModel):
    payment_method_id: str


# Models for the stripe payment gateway
class PaymentIntentCreate(BaseModel):
    amount: float
    plan_type: str  # monthly or yearly
    email: EmailStr
    name: str
    user_id: int

class PaymentIntentResponse(BaseModel):
    clientSecret: str
    paymentIntentId: str
    amount: float
    currency: str

class PaymentVerify(BaseModel):
    payment_intent_id: str
    user_id: int

class SubscriptionResponse(BaseModel):
    id: int
    user_id: int
    subscription_plan: str
    transaction_id: str
    tx_ref: str
    amount: Decimal
    currency: str
    status: str
    payment_provider: str
    created_at: datetime
    start_date: datetime
    end_date: datetime

    class Config:
        from_attributes = True


'''Customer Service tables and models
    Tickets for users reports are also included
'''
class TicketCreate(BaseModel):
    issue: str
    category: Optional[str] = "general"

class MessageCreate(BaseModel):
    ticket_id: int
    message: str

class TicketResponse(BaseModel):
    id: int
    user_id: int
    issue: str
    category: str
    status: str
    created_at: datetime
    updated_at: datetime
    unread_count: int = 0
    last_message: Optional[str] = None
    last_message_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class MessageResponse(BaseModel):
    id: int
    ticket_id: int
    sender_id: int
    sender_name: str
    sender_role: str
    message: str
    is_read: bool
    created_at: datetime

    class Config:
        from_attributes = True

class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    issue = Column(Text, nullable=False)
    category = Column(String(50), default="general")  # general, technical, billing, etc.
    status = Column(String(50), default="open")  # open, in_progress, resolved, closed
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    user = relationship("User", back_populates="tickets")
    messages = relationship("TicketMessage", back_populates="ticket", cascade="all, delete-orphan")

class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=False)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    sender_role = Column(String(20), nullable=False)  # "user" or "admin"
    message = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    ticket = relationship("Ticket", back_populates="messages")
    sender = relationship("User")


'''Customer reviews tables and the information
'''
class Review(Base):
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)  # Add user authentication later
    business_name = Column(String, index=True)
    review_title = Column(String)
    rating = Column(Integer)
    review_text = Column(Text)
    date_submitted = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    status = Column(String, default="Submitted")  # published, under-review, rejected (Clinton's)
    category = Column(String, default="General")
    helpful = Column(Integer, default=0)
    verified = Column(Boolean, default=False)

    conversations = relationship("Conversation", back_populates="review", cascade="all, delete-orphan")

class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    review_id = Column(Integer, ForeignKey("reviews.id"))
    sender_type = Column(String)  # 'admin' or 'user'
    message = Column(Text)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_read = Column(Boolean, default=False)

    # Relationships
    review = relationship("Review", back_populates="conversations")



class DisplayedReview(Base):
    """
    Stores reviews selected by admin to be displayed on the homepage.
    This allows dynamic control of which reviews appear to visitors.
    """
    __tablename__ = "displayed_reviews"

    id = Column(Integer, primary_key=True, index=True)
    review_id = Column(Integer, ForeignKey("reviews.id", ondelete="CASCADE"), unique=True, nullable=False)
    display_order = Column(Integer, default=0, nullable=False)  # Lower number = higher priority
    added_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    added_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))

    # Relationships
    review = relationship("Review", backref="display_info")
    admin = relationship("User", foreign_keys=[added_by])


class ReviewCreate(BaseModel):
    business_name: str
    review_title: str
    rating: int
    review_text: str
    category: Optional[str] = "General"

class ReviewResponse(BaseModel):
    id: int
    business_name: str
    review_title: str
    rating: int
    review_text: str
    date_submitted: datetime
    status: str
    category: str
    helpful: int
    verified: bool
    admin_response: bool
    conversation_count: int
    unread_messages: int
    has_conversation: bool

    class Config:
        from_attributes = True

class ConversationCreate(BaseModel):
    review_id: int
    sender_type: str
    message: str

class ConversationResponse(BaseModel):
    id: int
    review_id: int
    sender_type: str
    message: str
    timestamp: datetime
    is_read: bool

    class Config:
        from_attributes = True

class UnreadCountResponse(BaseModel):
    total_unread: int
    reviews_with_unread: int


'''Opportunity Alert Tables and Schema'''
class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    category = Column(String, nullable=False)
    priority = Column(String, nullable=False)
    score = Column(Integer, nullable=False)
    time_remaining = Column(String, nullable=False)
    why_act_now = Column(Text, nullable=False)
    potential_reward = Column(Text, nullable=False)
    action_required = Column(Text, nullable=False)
    source = Column(String, nullable=True)
    url = Column(String(500), nullable=True)
    date = Column(String, nullable=False)
    total_views = Column(Integer, default=0)
    total_shares = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user_alerts = relationship("UserAlert", back_populates="alert")


class UserAlert(Base):
    __tablename__ = "user_alerts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    alert_id = Column(Integer, ForeignKey("alerts.id"))
    has_viewed = Column(Boolean, default=False)
    has_shared = Column(Boolean, default=False)
    is_attended = Column(Boolean, default=False)
    viewed_at = Column(DateTime, nullable=True)
    shared_at = Column(DateTime, nullable=True)
    chops_earned_from_view = Column(Integer, default=0)
    chops_earned_from_share = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="user_alerts")
    alert = relationship("Alert", back_populates="user_alerts")


'''Referrals Table'''
class Referral(Base):
    __tablename__ = "referrals"

    id = Column(Integer, primary_key=True, index=True)
    referrer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    referred_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    chops_awarded = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    referrer = relationship("User", foreign_keys=[referrer_id], back_populates="referrals")
    referred_user = relationship("User",foreign_keys=[referred_user_id], back_populates="referred_by")


'''Commissions Table'''
class Commission(Base):
    __tablename__ = "commissions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    referred_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=True)
    amount = Column(Numeric(precision=10, scale=2), nullable=False)
    original_amount = Column(Numeric(precision=10, scale=2), nullable=True)
    currency = Column(String(10), nullable=True)
    commission_rate = Column(Numeric(precision=5, scale=2), nullable=True)
    status = Column(String, nullable=False)  # pending, processing, paid, failed
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    approved_at = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    payout_id = Column(Integer, nullable=True)

    # Relationships
    user = relationship("User", foreign_keys=[user_id])
    referred_user = relationship("User", foreign_keys=[referred_user_id])
    subscription = relationship("Subscriptions", foreign_keys=[subscription_id])


'''Payouts Table'''
class Payout(Base):
    __tablename__ = "payouts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Numeric(precision=10, scale=2), nullable=False)
    currency = Column(String(10), default="USD")
    status = Column(String, nullable=False)  # pending, processing, completed, failed
    provider = Column(String(50), nullable=True)  # stripe, paypal, etc.
    payment_method = Column(String(50), nullable=True)
    provider_payout_id = Column(String(255), nullable=True)
    provider_response = Column(Text, nullable=True)
    recipient_email = Column(String(255), nullable=True)
    recipient_name = Column(String(255), nullable=True)
    account_details = Column(Text, nullable=True)
    failure_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    processed_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    requested_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User", foreign_keys=[user_id])


'''Payout Account Table'''
class PayoutAccount(Base):
    __tablename__ = "payout_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)
    payment_method = Column("default_payout_method", String(50), nullable=False)

    # Stripe fields
    stripe_account_id = Column(String(255), nullable=True)
    stripe_account_status = Column(String(50), nullable=True)

    # Flutterwave/Bank fields
    bank_name = Column(String(255), nullable=True)
    account_number = Column(String(100), nullable=True)
    account_name = Column(String(255), nullable=True)
    bank_code = Column(String(50), nullable=True)
    flutterwave_recipient_code = Column(String(255), nullable=True)

    # PayPal fields
    paypal_email = Column(String(255), nullable=True)


    is_verified = Column(Boolean, default=False)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    user = relationship("User", foreign_keys=[user_id])


class CommissionSummary(Base):
    """
    Monthly summary of commissions per user (for reporting and analytics)
    """
    __tablename__ = "commission_summaries"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Period
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)

    # Summary metrics
    total_commissions = Column(Numeric(10, 2), default=0.00)
    paid_commissions = Column(Numeric(10, 2), default=0.00)
    pending_commissions = Column(Numeric(10, 2), default=0.00)
    commission_count = Column(Integer, default=0)

    # Currency
    currency = Column(String(10), nullable=False, default='USD')

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationship
    user = relationship("User")

    # Unique constraint and indexes
    __table_args__ = (
        Index('idx_commission_summary_user_period', 'user_id', 'year', 'month', unique=True),
    )


class ReferralResponse(BaseModel):
    id: int
    referred_user_id: int
    referred_user_email: str
    referred_user_name: str
    chops_awarded: int
    created_at: str
    is_active: bool

    class Config:
        from_attributes = True

class ReferralStats(BaseModel):
    total_referrals: int
    total_chops_earned: int
    referrals_this_month: int
    recent_referrals: List[dict]

    class Config:
        from_attributes = True


class ReferralCreate(BaseModel):
    referred_user_id: int
    chops_awarded: int = 0

    class Config:
        from_attributes = True

class UserCreate(BaseModel):
    name: str
    email: str
    subscription_status: str = "free"
    referrer_name: Optional[str] = None

class AlertCreate(BaseModel):
    title: str
    category: str
    priority: str
    score: int
    time_remaining: str
    why_act_now: str
    potential_reward: str
    action_required: str
    source: Optional[str] = None
    date: str
    url: Optional[str] = None

class AlertResponse(BaseModel):
    id: int
    title: str
    category: str
    priority: str
    score: int
    time_remaining: str
    why_act_now: str
    potential_reward: str
    action_required: str
    source: Optional[str]
    url: Optional[str] = None
    date: str
    posted_date: Optional[str] = None  # crawled/posted date derived from created_at
    total_views: int
    total_shares: int
    has_viewed: bool = False
    has_shared: bool = False
    is_attended: bool = False
    is_pinned: bool = False

    class Config:
        from_attributes = True

class ViewAlertRequest(BaseModel):
    alert_id: int

class ShareAlertRequest(BaseModel):
    alert_id: int


'''Insights Tables and Schema'''
class Insight(Base):
    __tablename__ = "insights"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    category = Column(String)
    read_time = Column(String)
    date = Column(String, nullable=False)
    source = Column(String)
    url = Column(String(500), nullable=True)
    what_changed = Column(Text)
    why_it_matters = Column(Text)
    action_to_take = Column(Text)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = Column(Boolean, default=True)
    total_views = Column(Integer, default=0)
    total_shares = Column(Integer, default=0)

    user_insights = relationship("UserInsight", back_populates="insight")


class UserInsight(Base):
    __tablename__ = "user_insights"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    insight_id = Column(Integer, ForeignKey("insights.id"))
    has_viewed = Column(Boolean, default=False)
    has_shared = Column(Boolean, default=False)
    is_attended = Column(Boolean, default=False)
    viewed_at = Column(DateTime, nullable=True)
    shared_at = Column(DateTime, nullable=True)
    chops_earned_from_view = Column(Integer, default=0)
    chops_earned_from_share = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="user_insights")
    insight = relationship("Insight", back_populates="user_insights")


class UserPinnedInsight(Base):
    __tablename__ = "user_pinned_insights"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    insight_id = Column(Integer, ForeignKey("insights.id"))
    pinned_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="pinned_insights")


class UserPinnedAlert(Base):
    __tablename__ = "user_pinned_alerts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    alert_id = Column(Integer, ForeignKey("alerts.id"))
    pinned_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="pinned_alerts")


class Trend(Base):
    """
    Viral AI Trends model for storing trending topics
    """
    __tablename__ = "trends"

    id = Column(Integer, primary_key=True, index=True)

    # Basic Information
    title = Column(String(255), nullable=False)
    industry = Column(String(100), nullable=False)
    description = Column(Text, nullable=False)

    # Engagement Metrics
    engagement = Column(String(50), nullable=True)  # e.g., "12.5M"
    growth = Column(String(50), nullable=True)  # e.g., "+245%"
    viral_score = Column(Integer, nullable=False)  # 0-100
    search_volume = Column(String(50), nullable=True)  # e.g., "450,000/month"

    # Timing & Competition
    peak_time = Column(String(50), nullable=True)  # e.g., "2:00 PM EST"
    competition = Column(String(20), default="medium")  # low, medium, high
    opportunity = Column(String(50), nullable=True)  # e.g., "94%"
    nature = Column(String(50), nullable=False)  # Explosive, Growing, Emerging, Mainstream

    # Social Data (JSON)
    hashtags = Column(JSON, nullable=True)  # ["#AIAvatars", "#ContentCreation"]
    platforms = Column(JSON, nullable=True)  # ["LinkedIn", "Twitter", "TikTok"]

    # Actionable Content
    action_items = Column(Text, nullable=False)

    # Metadata
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, nullable=True, onupdate=lambda: datetime.now(timezone.utc))
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)


class InsightItems(BaseModel):
    id: int
    title: str
    category: str
    read_time: str
    time_remaining: str
    why_changed: str
    why_it_matters: str
    action_to_take: str
    source: Optional[str]
    url: Optional[str] = None
    date: str
    total_views: int
    total_shares: int
    has_viewed: bool = False
    has_shared: bool = False
    is_attended: bool = False
    is_pinned: bool = False
    class Config:
        from_attributes = True


class InsightResponse(BaseModel):
    insights: List[InsightItems]
    current_page: int
    total_pages: int
    total_insights: int
    is_pro: bool


class InsightCreate(BaseModel):
    title: str
    category: str
    read_time: str
    what_changed: str
    why_it_matters: str
    action_to_take: str
    source: Optional[str] = None
    date: str
    url: Optional[str] = None


class ViewInsightRequest(BaseModel):
    insight_id: int


class ShareInsightRequest(BaseModel):
    insight_id: int

    class Config:
        extra = "ignore"


class PinInsightRequest(BaseModel):
    insight_id: int


class PinAlertRequest(BaseModel):
    alert_id: int


class ChopsBreakdown(BaseModel):
    total_chops: int
    alert_reading_chops: int
    alert_sharing_chops: int
    insight_reading_chops: int
    insight_sharing_chops: int
    referral_chops: int
    referral_count: int


class TrendCreate(BaseModel):
    """Pydantic model for creating a trend"""
    title: str
    industry: str
    description: str
    engagement: Optional[str] = None
    growth: Optional[str] = None
    viral_score: int
    search_volume: Optional[str] = None
    peak_time: Optional[str] = None
    competition: Optional[str] = "medium"
    opportunity: Optional[str] = None
    nature: str
    hashtags: Optional[List[str]] = None
    platforms: Optional[List[str]] = None
    action_items: str


class TrendResponse(BaseModel):
    """Pydantic model for trend response"""
    id: int
    title: str
    industry: str
    description: str
    engagement: Optional[str]
    growth: Optional[str]
    viral_score: int
    search_volume: Optional[str]
    peak_time: Optional[str]
    competition: str
    opportunity: Optional[str]
    nature: str
    hashtags: Optional[List[str]]
    platforms: Optional[List[str]]
    action_items: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True




'''Commission and Payout Pydantic Models'''
class ApproveCommissionsRequest(BaseModel):
    """Request model for approving commissions"""
    commission_ids: Optional[List[int]] = None
    payment_method: Optional[str] = 'stripe'
    amount: Optional[Decimal] = None

    class Config:
        from_attributes = True






'''Security Architecture Tables'''
# User Session Table
class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(String(64), primary_key=True)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    ip_address = Column(INET, nullable=False)
    user_agent = Column(Text)
    is_active = Column(Boolean, default=True)
    last_activity = Column(DateTime(timezone=True), server_default=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    revoked_at = Column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_user_sessions_user_id", "user_id"),
        Index("idx_user_sessions_active", "is_active"),
    )


# Security Event Table
class SecurityEvent(Base):
    __tablename__ = "security_events"

    id = Column(Integer, primary_key=True, index=True)
    type = Column(String(50), nullable=False)
    severity = Column(String(20), nullable=False)
    user_id = Column(UUID(as_uuid=True))
    ip_address = Column(INET, nullable=False)
    location = Column(String(255))
    description = Column(Text, nullable=False)
    status = Column(String(50), nullable=False)
    details = Column(JSONB)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_security_events_type", "type"),
        Index("idx_security_events_severity", "severity"),
        Index("idx_security_events_ip", "ip_address"),
        Index("idx_security_events_created", created_at.desc()),
    )


# IP Blacklist Table
class IPBlacklist(Base):
    __tablename__ = "ip_blacklist"

    id = Column(Integer, primary_key=True, index=True)
    ip_address = Column(INET, unique=True, nullable=False)
    reason = Column(Text, nullable=False)
    email = Column(String(255), nullable=True)  # Email that attempted login from this IP
    is_active = Column(Boolean, default=True)
    blocked_at = Column(DateTime(timezone=True), server_default=func.now())
    blocked_by = Column(UUID(as_uuid=True))
    expires_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_ip_blacklist_ip", "ip_address"),
        Index("idx_ip_blacklist_active", "is_active"),
    )


# Failed Login Attempt Table
class FailedLoginAttempt(Base):
    __tablename__ = "failed_login_attempts"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), nullable=False)
    ip_address = Column(INET, nullable=False)
    user_agent = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_failed_logins_email", "email"),
        Index("idx_failed_logins_ip", "ip_address"),
        Index("idx_failed_logins_time", created_at.desc()),
    )


# Firewall Rule Table
class FirewallRule(Base):
    __tablename__ = "firewall_rules"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    type = Column(String(50), nullable=False)
    status = Column(String(20), default="active")
    is_active = Column(Boolean, default=True)
    priority = Column(String(20), nullable=False)
    description = Column(Text)
    rule_config = Column(JSONB, nullable=False)
    hits = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_firewall_rules_status", "status"),
        Index("idx_firewall_rules_priority", "priority"),
    )


# Vulnerability Scan Table
class VulnerabilityScan(Base):
    __tablename__ = "vulnerability_scans"

    id = Column(Integer, primary_key=True, index=True)
    scan_type = Column(String(100), nullable=False)
    status = Column(String(50), nullable=False)
    severity = Column(String(20))
    findings = Column(Integer, default=0)
    scan_results = Column(JSONB)
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True))
    duration_seconds = Column(Integer)

    __table_args__ = (
        Index("idx_vulnerability_scans_status", "status"),
        Index("idx_vulnerability_scans_started", started_at.desc()),
    )


# Audit Log Table
class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    action = Column(String(100), nullable=False)
    resource_type = Column(String(50), nullable=False)
    resource_id = Column(String(255))
    ip_address = Column(INET, nullable=False)
    user_agent = Column(Text)
    changes = Column(JSONB)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_audit_log_user", "user_id"),
        Index("idx_audit_log_action", "action"),
        Index("idx_audit_log_created", created_at.desc()),
    )


# Password Reset Token Table
class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    token = Column(String(255), unique=True, nullable=False)
    ip_address = Column(INET, nullable=False)
    used = Column(Boolean, default=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_password_reset_token", "token"),
        Index("idx_password_reset_user", "user_id"),
    )


# Security Metrics Summary View-Model
class SecurityMetricsSummary(Base):
    """
    ORM Model for the security_metrics_summary view.
    Used for retrieving aggregate security data.
    """
    __tablename__ = "security_metrics_summary"

    # Views don't have PKs, but SQLAlchemy requires one
    # Using total_events_24h as a dummy PK since it's likely unique enough for reading
    total_events_24h = Column(Integer, primary_key=True)
    high_severity_events_24h = Column(Integer)
    blocked_attacks_24h = Column(Integer)
    failed_logins_24h = Column(Integer)
    active_blacklisted_ips = Column(Integer)
    active_firewall_rules = Column(Integer)


class SecurityMetricsResponse(BaseModel):
    threatLevel: str
    blockedAttacks: int
    failedLogins: int
    suspiciousActivity: int
    activeFirewallRules: int
    lastSecurityScan: str

    model_config = ConfigDict(from_attributes=True)


class CommissionResponse(BaseModel):
    """Response model for commission data"""
    id: int
    user_id: int
    referred_user_id: Optional[int]
    subscription_id: Optional[int]
    amount: float
    currency: Optional[str]
    status: str
    created_at: datetime
    approved_at: Optional[datetime]
    paid_at: datetime

    class Config:
        from_attributes = True


class PayoutResponse(BaseModel):
    """Response model for payout data"""
    id: int
    user_id: int
    amount: float
    currency: str
    status: str
    provider: Optional[str]
    provider_payout_id: Optional[str]
    created_at: datetime
    processed_at: Optional[datetime]

    class Config:
        from_attributes = True


class CommissionSummaryResponse(BaseModel):
    """Summary model for commission statistics"""
    total_commissions: float
    paid_commissions: float
    pending_commissions: float
    commission_count: int

    class Config:
        from_attributes = True


class PayoutAccountCreate(BaseModel):
    """Request model for creating/updating payout account"""
    payment_method: str  # stripe, flutterwave, paypal
    stripe_account_id: Optional[str] = None
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    account_name: Optional[str] = None
    bank_code: Optional[str] = None
    paypal_email: Optional[str] = None

    class Config:
        from_attributes = True


class PayoutRequest(BaseModel):
    """Request model for requesting a payout"""
    amount: float
    payment_method: str  # stripe, flutterwave, paypal

    class Config:
        from_attributes = True


'''System Settings Table'''
class SystemSettings(Base):
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, index=True)

    # General
    site_name = Column(String(255), default="AI Business Analyst")
    support_email = Column(String(255), default="support@aitugo.com")
    default_language = Column(String(10), default="en")
    timezone = Column(String(50), default="UTC")

    # Limits
    max_analyses_basic = Column(Integer, default=5)
    max_analyses_pro = Column(Integer, default=50)
    max_analyses_premium = Column(Integer, default=500)

    # AI Settings
    primary_ai_model = Column(String(100), default="gpt-4")
    analysis_timeout = Column(Integer, default=120)  # seconds
    max_tokens = Column(Integer, default=2000)
    temperature = Column(Float, default=0.7)
    enable_predictive_analytics = Column(Boolean, default=True)
    generate_recommendations = Column(Boolean, default=True)
    include_confidence_scores = Column(Boolean, default=True)
    enable_experimental_features = Column(Boolean, default=False)

    # Security
    require_mfa_admin = Column(Boolean, default=False)
    force_password_reset_90 = Column(Boolean, default=False)
    lock_accounts_after_failed_attempts = Column(Boolean, default=True)
    data_retention_days = Column(Integer, default=90)
    backup_frequency = Column(String(50), default="daily")

    # Billing
    monthly_price = Column(Float, default=29.99)
    quarterly_price = Column(Float, default=79.99)
    yearly_price = Column(Float, default=299.99)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


# ============================================================================
# MISSIONS SYSTEM MODELS
# ============================================================================

class Mission(Base):
    """
    Missions are structured action plans that guide users through tasks.
    Each mission has multiple steps and rewards chops upon completion.
    """
    __tablename__ = "missions"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    category = Column(String(100), nullable=True)  # sales, marketing, product, etc.
    difficulty = Column(String(50), default="beginner")  # beginner, intermediate, advanced
    total_steps = Column(Integer, nullable=False)
    points_reward = Column(Integer, default=0)
    estimated_days = Column(Integer, nullable=False)
    is_active = Column(Boolean, default=True)
    order_index = Column(Integer, default=0)
    icon = Column(String(100), nullable=True)
    color_theme = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    steps = relationship("MissionStep", back_populates="mission", cascade="all, delete-orphan", order_by="MissionStep.order_index")
    user_missions = relationship("UserMission", back_populates="mission")


class MissionStep(Base):
    """
    Individual steps within a mission.
    Each step has a specific task and can award points.
    """
    __tablename__ = "mission_steps"

    id = Column(Integer, primary_key=True, index=True)
    mission_id = Column(Integer, ForeignKey("missions.id", ondelete="CASCADE"), nullable=False)
    day = Column(Integer, nullable=False)
    label = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    points = Column(Integer, default=0)
    order_index = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    mission = relationship("Mission", back_populates="steps")
    user_step_completions = relationship("UserMissionStep", back_populates="step")


class UserMission(Base):
    """
    Tracks which missions a user has started/completed.
    """
    __tablename__ = "user_missions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    mission_id = Column(Integer, ForeignKey("missions.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(50), default="active")  # active, completed, abandoned
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    progress_percentage = Column(Integer, default=0)
    completed_steps = Column(Integer, default=0)

    # Relationships
    mission = relationship("Mission", back_populates="user_missions")
    step_completions = relationship("UserMissionStep", back_populates="user_mission", cascade="all, delete-orphan")

    # Indexes
    __table_args__ = (
        Index('idx_user_mission_user_id', 'user_id'),
        Index('idx_user_mission_status', 'status'),
    )


class UserMissionStep(Base):
    """
    Tracks completion of individual mission steps by users.
    """
    __tablename__ = "user_mission_steps"

    id = Column(Integer, primary_key=True, index=True)
    user_mission_id = Column(Integer, ForeignKey("user_missions.id", ondelete="CASCADE"), nullable=False, index=True)
    step_id = Column(Integer, ForeignKey("mission_steps.id", ondelete="CASCADE"), nullable=False, index=True)
    completed = Column(Boolean, default=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    reflection = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    user_mission = relationship("UserMission", back_populates="step_completions")
    step = relationship("MissionStep", back_populates="user_step_completions")


# ============================================================================
# USER SETTINGS MODEL
# ============================================================================

class UserSettings(Base):
    """
    Stores user preferences and settings.
    """
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    # Notification Settings
    email_notifications = Column(Boolean, default=True)
    push_notifications = Column(Boolean, default=True)
    analysis_reminders = Column(Boolean, default=True)
    community_notifications = Column(Boolean, default=True)

    # Decision Engine Settings
    active_constraint_mode = Column(Boolean, default=True)
    pending_task_reminders = Column(Boolean, default=True)

    # Display Settings
    dark_mode = Column(Boolean, default=False)
    compact_view = Column(Boolean, default=False)

    # Privacy Settings
    profile_visibility = Column(String(50), default="community")  # public, community, private
    show_earnings = Column(Boolean, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


# ──────────────────────────────────────────────
# Community Models
# ──────────────────────────────────────────────

class CommunityChannel(Base):
    __tablename__ = "community_channels"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    slug = Column(String(100), unique=True, index=True, nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(50), nullable=False, default="General")
    member_count = Column(Integer, default=0)
    post_count = Column(Integer, default=0)
    icon = Column(String(20), nullable=True)
    is_public = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    members = relationship("ChannelMember", back_populates="channel", cascade="all, delete-orphan")
    discussions = relationship("CommunityDiscussion", back_populates="channel", cascade="all, delete-orphan")


class ChannelMember(Base):
    __tablename__ = "channel_members"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    channel_id = Column(Integer, ForeignKey("community_channels.id", ondelete="CASCADE"), nullable=False)
    is_moderator = Column(Boolean, default=False)
    joined_at = Column(DateTime(timezone=True), server_default=func.now())
    channel = relationship("CommunityChannel", back_populates="members")
    user = relationship("User")


class CommunityDiscussion(Base):
    __tablename__ = "community_discussions"
    id = Column(Integer, primary_key=True, index=True)
    channel_id = Column(Integer, ForeignKey("community_channels.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    tags = Column(JSON, nullable=True)
    like_count = Column(Integer, default=0)
    reply_count = Column(Integer, default=0)
    view_count = Column(Integer, default=0)
    is_pinned = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    channel = relationship("CommunityChannel", back_populates="discussions")
    user = relationship("User")
    replies = relationship("DiscussionReply", back_populates="discussion", cascade="all, delete-orphan")
    likes = relationship("DiscussionLike", back_populates="discussion", cascade="all, delete-orphan")


class DiscussionReply(Base):
    __tablename__ = "discussion_replies"
    id = Column(Integer, primary_key=True, index=True)
    discussion_id = Column(Integer, ForeignKey("community_discussions.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content = Column(Text, nullable=False)
    like_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    discussion = relationship("CommunityDiscussion", back_populates="replies")
    user = relationship("User")


class DiscussionLike(Base):
    __tablename__ = "discussion_likes"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    discussion_id = Column(Integer, ForeignKey("community_discussions.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    discussion = relationship("CommunityDiscussion", back_populates="likes")


class CommunityEvent(Base):
    __tablename__ = "community_events"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    event_type = Column(String(50), nullable=False, default="Webinar")
    scheduled_at = Column(DateTime(timezone=True), nullable=True)
    duration_minutes = Column(Integer, default=60)
    max_attendees = Column(Integer, nullable=True)
    attendee_count = Column(Integer, default=0)
    host_name = Column(String(100), nullable=True)
    meeting_link = Column(String(500), nullable=True)
    is_published = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    registrations = relationship("EventRegistration", back_populates="event", cascade="all, delete-orphan")


class EventRegistration(Base):
    __tablename__ = "event_registrations"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_id = Column(Integer, ForeignKey("community_events.id", ondelete="CASCADE"), nullable=False)
    registered_at = Column(DateTime(timezone=True), server_default=func.now())
    event = relationship("CommunityEvent", back_populates="registrations")
    user = relationship("User")


class CommunityActivity(Base):
    __tablename__ = "community_activities"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    action_type = Column(String(50), nullable=False)
    target_id = Column(Integer, nullable=True)
    target_type = Column(String(50), nullable=True)
    target_name = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    user = relationship("User")


class SavedItem(Base):
    __tablename__ = "saved_items"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    item_id = Column(Integer, nullable=False)
    item_type = Column(String(50), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    user = relationship("User")

# ─── Marketplace Models ──────────────────────────────────────────────────────

class MarketplaceTool(Base):
    __tablename__ = "marketplace_tools"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    author = Column(String(100), nullable=False)
    description = Column(Text, nullable=False)
    full_description = Column(Text, nullable=True)
    category = Column(String(100), nullable=False, default="AI Tools")
    price = Column(Float, default=0.0)
    tags = Column(JSON, nullable=True)
    features = Column(JSON, nullable=True)  # list of strings
    icon_name = Column(String(50), nullable=False, default="Cpu")
    color_theme = Column(String(30), nullable=False, default="orange")
    sales_count = Column(Integer, default=0)
    rating = Column(Float, default=0.0)
    review_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    purchase_url = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    purchases = relationship("MarketplacePurchase", back_populates="tool", cascade="all, delete-orphan")


class MarketplacePurchase(Base):
    __tablename__ = "marketplace_purchases"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    tool_id = Column(Integer, ForeignKey("marketplace_tools.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(50), nullable=False, default="pending")  # pending, completed, refunded
    amount_paid = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    user = relationship("User")
    tool = relationship("MarketplaceTool", back_populates="purchases")


class MarketplaceRequest(Base):
    __tablename__ = "marketplace_requests"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    budget = Column(String(100), nullable=True)
    timeline = Column(String(100), nullable=True)
    status = Column(String(50), nullable=False, default="open")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    user = relationship("User")


class CreatorListing(Base):
    __tablename__ = "creator_listings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    full_description = Column(Text, nullable=True)
    listing_type = Column(String(50), nullable=False)  # Template|Playbook|Service|Tool|Course|Automation|Consulting|Strategy
    category = Column(String(100), nullable=False)
    price = Column(Float, default=0.0)
    tags = Column(JSON, nullable=True)
    features = Column(JSON, nullable=True)
    icon_name = Column(String(50), nullable=False, default="Cpu")
    color_theme = Column(String(30), nullable=False, default="orange")
    purchase_url = Column(String(500), nullable=True)
    sales_count = Column(Integer, default=0)
    rating = Column(Float, default=0.0)
    review_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    user = relationship("User")


class MvpFeature(Base):
    """Feature flag controlling which pages/features are visible in the customer app."""
    __tablename__ = "mvp_features"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=True)
    directory = Column(String(500), nullable=False, unique=True)
    is_in_mvp = Column(Boolean, nullable=False, default=False)
    has_sub_pages = Column(Boolean, nullable=False, default=False)
    launch_date = Column(String(100), nullable=True)
    is_in_next_feature_launch = Column(Boolean, nullable=False, default=False)
    # JSON array of sub-pages: [{name, directory, is_in_mvp, ...}]
    sub_pages = Column(JSON, nullable=True, default=list)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
