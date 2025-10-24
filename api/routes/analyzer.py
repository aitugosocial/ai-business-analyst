
from fastapi import APIRouter, Depends
from api.routes.login import get_current_user
from db.models import User, UserResponse

router = APIRouter(
    prefix="/analyzer",
    tags = ["get_user"]
)


@router.get("", response_model = UserResponse)
def current(current_user: User = Depends(get_current_user)):
    return {
        "message": f"Welcome {current_user.email}, you have access to the analyzer API."
    }

@router.get("/me", response_model=UserResponse)
def get_my_details(current_user: User = Depends(get_current_user)):
    """Get detailed information about the current logged-in user"""
    return current_user

@router.get("/profile")
def get_full_profile(current_user: User = Depends(get_current_user)):
    """Get full user profile with custom message"""
    return {
        "id": current_user.id,
        "name": current_user.name,
        "email": current_user.email,
        "message": f"Welcome {current_user.name}! You have access to the analyzer API."
    }