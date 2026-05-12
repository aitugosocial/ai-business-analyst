"""
Google OAuth Authentication Routes

Handles Google OAuth 2.0 login flow for user authentication.

Endpoints:
    - GET /auth/google/login - Initiate Google OAuth flow
    - GET /auth/google/callback - Handle OAuth callback

Requirements:
    - GOOGLE_CLIENT_ID environment variable
    - GOOGLE_CLIENT_SECRET environment variable
    - GOOGLE_REDIRECT_URI environment variable (e.g., http://localhost:8000/api/v1/auth/google/callback)
"""

import os
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import RedirectResponse
import httpx
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone

from database.pg_connections import get_db
from database.pg_models import User
from api.routes.auth.login import create_access_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/google", tags=["Google OAuth"])

# Google OAuth settings
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/api/v1/auth/google/callback")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

# Google OAuth URLs
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


@router.get("/login")
async def google_login(ref: Optional[str] = Query(None)):
    """
    Initiate Google OAuth flow.
    
    Args:
        ref (str, optional): Referral code to pass through OAuth flow
        
    Returns:
        RedirectResponse: Redirect to Google OAuth consent page
    """
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Google OAuth not configured (missing GOOGLE_CLIENT_ID)")
    
    # Build OAuth authorization URL
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent",
    }
    
    # Add referral code to state if provided
    if ref:
        params["state"] = f"ref={ref}"
    
    # Construct authorization URL
    auth_url = f"{GOOGLE_AUTH_URL}?{'&'.join([f'{k}={v}' for k, v in params.items()])}"
    
    logger.info(f"Initiating Google OAuth flow{' with referral: ' + ref if ref else ''}")
    
    return RedirectResponse(url=auth_url)


@router.get("/callback")
async def google_callback(
    request: Request,
    code: str = Query(...),
    state: Optional[str] = Query(None)
):
    """
    Handle Google OAuth callback.
    
    Exchanges authorization code for access token, fetches user info,
    creates/updates user in database, and redirects to frontend with JWT.
    
    Args:
        code (str): Authorization code from Google
        state (str, optional): State parameter (contains referral code if passed)
        
    Returns:
        RedirectResponse: Redirect to frontend with access token
    """
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Google OAuth not configured")
    
    try:
        # Extract referral code from state if present
        referral_code = None
        if state and "ref=" in state:
            referral_code = state.split("ref=")[1].split("&")[0]
        
        # Exchange authorization code for access token
        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uri": GOOGLE_REDIRECT_URI,
                    "grant_type": "authorization_code",
                }
            )
            
            if token_response.status_code != 200:
                logger.error(f"Google token exchange failed: {token_response.text}")
                raise HTTPException(status_code=400, detail="Failed to exchange authorization code")
            
            tokens = token_response.json()
            access_token = tokens.get("access_token")
            
            # Fetch user info from Google
            userinfo_response = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"}
            )
            
            if userinfo_response.status_code != 200:
                logger.error(f"Failed to fetch Google user info: {userinfo_response.text}")
                raise HTTPException(status_code=400, detail="Failed to fetch user information")
            
            user_info = userinfo_response.json()
        
        # Extract user details
        google_id = user_info.get("id")
        email = user_info.get("email")
        name = user_info.get("name")
        picture = user_info.get("picture")
        
        if not google_id or not email:
            raise HTTPException(status_code=400, detail="Missing required user information from Google")
        
        # Get database session
        db: Session = next(get_db())
        
        try:
            # Check if user exists by email or Google ID
            user = db.query(User).filter(
                (User.email == email) | (User.google_id == google_id)
            ).first()
            
            if user:
                # Update existing user with Google info
                user.google_id = google_id
                user.profile_image_url = picture or user.profile_image_url
                user.last_login = datetime.now(timezone.utc)
                logger.info(f"Existing user logged in via Google: {email}")
            else:
                # Create new user
                user = User(
                    email=email,
                    name=name or email.split("@")[0],
                    google_id=google_id,
                    profile_image_url=picture,
                    hashed_password="",  # No password for OAuth users
                    role="user",
                    is_active=True,
                    email_verified=True,  # Google emails are pre-verified
                    created_at=datetime.now(timezone.utc),
                    last_login=datetime.now(timezone.utc),
                )
                
                # Handle referral code if provided
                if referral_code:
                    referrer = db.query(User).filter(User.referral_code == referral_code).first()
                    if referrer:
                        user.referrer_id = referrer.id
                        logger.info(f"New user created via Google OAuth with referral: {referral_code}")
                
                db.add(user)
                logger.info(f"New user created via Google OAuth: {email}")
            
            db.commit()
            db.refresh(user)
            
            # Generate JWT access token
            jwt_token = create_access_token(
                data={
                    "sub": str(user.id),
                    "email": user.email,
                    "role": user.role
                },
                expires_delta=timedelta(days=30)
            )
            
            # Redirect to frontend with token
            redirect_url = f"{FRONTEND_URL}/auth/callback?token={jwt_token}&user_id={user.id}&role={user.role}"
            
            logger.info(f"Google OAuth successful for user {email}, redirecting to frontend")
            
            return RedirectResponse(url=redirect_url)
            
        finally:
            db.close()
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Google OAuth callback error: {e}", exc_info=True)
        # Redirect to frontend with error
        error_url = f"{FRONTEND_URL}/login?error=oauth_failed"
        return RedirectResponse(url=error_url)
