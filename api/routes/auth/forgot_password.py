"""
Password reset endpoints for forgot password functionality
"""
import os
import secrets
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from passlib.context import CryptContext

from database.pg_connections import get_db
from database.pg_models import User
from emailing.email_service import MailerLiteEmailService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["password-reset"])

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Email service
email_service = MailerLiteEmailService()


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    new_password: str
    confirm_password: str


@router.post("/forgot-password", status_code=status.HTTP_200_OK)
async def forgot_password(
    request: ForgotPasswordRequest,
    db: Session = Depends(get_db)
):
    """
    Request password reset link via email.
    Generates a secure token and sends reset link.
    """
    try:
        # Find user by email
        user = db.query(User).filter(User.email == request.email).first()
        
        # Always return success even if user not found (security best practice)
        # This prevents email enumeration attacks
        if not user:
            logger.info(f"Password reset requested for non-existent email: {request.email}")
            return {
                "success": True,
                "message": "If an account with that email exists, a password reset link has been sent."
            }
        
        # Generate secure reset token (URL-safe)
        reset_token = secrets.token_urlsafe(32)
        
        # Set expiry to 30 minutes from now
        reset_expires = datetime.now(timezone.utc) + timedelta(minutes=30)
        
        # Update user with reset token and expiry
        user.password_reset_token = reset_token
        user.password_reset_expires = reset_expires
        user.password_reset_used_at = None  # Reset used_at field
        
        db.commit()
        
        # Send password reset email
        try:
            email_service.send_password_reset_email(
                user_email=user.email,
                name=user.name,
                reset_token=reset_token
            )
            logger.info(f"✅ Password reset email sent to {user.email}")
        except Exception as email_error:
            logger.error(f"❌ Failed to send reset email: {str(email_error)}")
            # Continue anyway - token is saved in DB
        
        return {
            "success": True,
            "message": "If an account with that email exists, a password reset link has been sent."
        }
        
    except Exception as e:
        logger.error(f"❌ Forgot password error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while processing your request. Please try again."
        )


@router.post("/reset-password/{token}", status_code=status.HTTP_200_OK)
async def reset_password(
    token: str,
    request: ResetPasswordRequest,
    db: Session = Depends(get_db)
):
    """
    Reset password using the token from email link.
    Validates token and updates user password.
    """
    try:
        # Validate passwords match
        if request.new_password != request.confirm_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Passwords do not match"
            )
        
        # Validate password strength (minimum 8 characters)
        if len(request.new_password) < 8:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password must be at least 8 characters long"
            )
        
        # Find user by reset token
        user = db.query(User).filter(User.password_reset_token == token).first()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired reset token"
            )
        
        # Check if token has expired
        if user.password_reset_expires < datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Reset token has expired. Please request a new password reset link."
            )
        
        # Check if token was already used
        if user.password_reset_used_at is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This reset link has already been used. Please request a new password reset link."
            )
        
        # Hash the new password
        hashed_password = pwd_context.hash(request.new_password)
        
        # Update user password and mark token as used
        user.password = hashed_password
        user.confirm_password = hashed_password  # Keep confirm_password in sync
        user.password_reset_used_at = datetime.now(timezone.utc)
        user.updated_at = datetime.now(timezone.utc)
        
        db.commit()
        
        logger.info(f"✅ Password reset successful for user: {user.email}")
        
        return {
            "success": True,
            "message": "Password reset successful. You can now log in with your new password."
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Password reset error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while resetting your password. Please try again."
        )
