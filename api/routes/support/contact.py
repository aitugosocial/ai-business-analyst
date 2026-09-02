"""
Contact Form API Route
Handles public contact form submissions, anti-spam honeypot checks,
database persistence in PostgreSQL, and background email dispatch to hello@lavoo.io
and user confirmation receipt.
"""

import logging
from typing import Optional, List
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from sqlalchemy import desc

from database.pg_connections import get_db
from database.pg_models import ContactMessage, User
from emailing.email_service import email_service
from api.routes.auth.login import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/support", tags=["support-contact"])


# ─── Request & Response Models ────────────────────────────────────────────────

class ContactFormRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100, description="Full name of the sender")
    email: EmailStr = Field(..., description="Valid email address of the sender")
    company: Optional[str] = Field(None, max_length=150, description="Optional company name")
    reason: Optional[str] = Field("general", max_length=50, description="Inquiry category or department")
    subject: Optional[str] = Field(None, max_length=200, description="Optional inquiry subject")
    message: str = Field(..., min_length=10, max_length=5000, description="Detailed inquiry message")
    honeypot: Optional[str] = Field(None, description="Anti-bot honeypot field, must be empty")


class ContactFormResponse(BaseModel):
    success: bool
    message: str


class ContactInquiryItem(BaseModel):
    id: int
    name: str
    email: str
    company: Optional[str] = None
    reason: str
    subject: Optional[str] = None
    message: str
    status: str
    created_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/contact", response_model=ContactFormResponse, status_code=status.HTTP_200_OK)
async def submit_contact_form(
    payload: ContactFormRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Submit a public contact form inquiry.
    
    1. Validates input fields and checks anti-bot honeypot.
    2. Persists inquiry into `contact_messages` table in PostgreSQL.
    3. Triggers background emails:
       - To admin (hello@lavoo.io) with reply-to set to user's email.
       - To user with immediate auto-receipt confirmation.
    """
    # 1. Anti-bot honeypot check: If filled, silently acknowledge without saving/sending
    if payload.honeypot and payload.honeypot.strip():
        logger.warning(f"🤖 Bot submission caught via honeypot field from {payload.email}")
        return ContactFormResponse(
            success=True,
            message="Thanks for reaching out! We've received your message and will respond within 24 hours."
        )

    clean_name = payload.name.strip()
    clean_email = payload.email.strip().lower()
    clean_company = payload.company.strip() if payload.company else None
    clean_reason = (payload.reason or "general").strip().lower()
    clean_subject = payload.subject.strip() if payload.subject else None
    clean_message = payload.message.strip()

    # 2. Persist in database
    try:
        contact_record = ContactMessage(
            name=clean_name,
            email=clean_email,
            company=clean_company,
            reason=clean_reason,
            subject=clean_subject,
            message=clean_message,
            status="unread"
        )
        db.add(contact_record)
        db.commit()
        db.refresh(contact_record)
        logger.info(f"✅ Contact inquiry #{contact_record.id} recorded from {clean_email}")
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Failed to persist contact message to DB: {str(e)}")
        # Continue to dispatch emails even if DB write encounters an error

    # 3. Dispatch background emails (non-blocking)
    # Email 1: To Lavoo team (hello@lavoo.io)
    background_tasks.add_task(
        email_service.send_contact_inquiry_to_admin,
        name=clean_name,
        email=clean_email,
        company=clean_company,
        reason=clean_reason,
        subject=clean_subject,
        message=clean_message
    )

    # Email 2: Confirmation auto-receipt to the user
    background_tasks.add_task(
        email_service.send_contact_confirmation_to_user,
        user_email=clean_email,
        name=clean_name
    )

    return ContactFormResponse(
        success=True,
        message="Thanks for reaching out! We've received your message and will respond within 24 hours."
    )


@router.get("/contact/inquiries", response_model=List[ContactInquiryItem])
async def list_contact_inquiries(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Admin-only endpoint to inspect contact inquiries stored in the database.
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrative privileges required"
        )

    query = db.query(ContactMessage)
    if status_filter:
        query = query.filter(ContactMessage.status == status_filter.lower())

    inquiries = query.order_by(desc(ContactMessage.created_at)).offset(skip).limit(limit).all()
    return inquiries
