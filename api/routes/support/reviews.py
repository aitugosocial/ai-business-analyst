from fastapi import APIRouter, HTTPException, Depends, Header, Cookie
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy import func, desc
from jose import jwt, JWTError
import traceback

from database.pg_connections import get_db
from database.pg_models import User, Review, Conversation

# Try to import from login.py, with detailed error handling
try:
    from api.routes.auth.login import SECRET_KEY, ALGORITHM
    print("✅ Successfully imported SECRET_KEY and ALGORITHM from login.py")
except ImportError as e:
    print(f"⚠️ Could not import from login.py: {e}")
    import os
    import secrets
    SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)
    ALGORITHM = "HS256"
    print(f"⚠️ Using fallback SECRET_KEY")


router = APIRouter(tags=["reviews"])

# --------------------------
# Public Endpoint for Homepage
# --------------------------

@router.get("/reviews/displayed")
async def get_displayed_reviews(db: Session = Depends(get_db)):
    """
    Public endpoint: Get reviews selected by admin for homepage display.
    No authentication required - this is for public visitors.
    Returns reviews ordered by display_order.
    """
    from database.pg_models import DisplayedReview, Review, User
    
    # Get all displayed reviews ordered by display_order
    displayed_reviews = db.query(DisplayedReview).order_by(DisplayedReview.display_order).all()
    
    results = []
    for dr in displayed_reviews:
        review = db.query(Review).filter(Review.id == dr.review_id).first()
        if review:  # Show all reviews explicitly added to display
            user = db.query(User).filter(User.id == review.user_id).first()
            results.append({
                "id": review.id,
                "user_name": user.name if user else "Anonymous",
                "business_name": review.business_name,
                "review_title": review.review_title,
                "rating": review.rating,
                "review_text": review.review_text,
                "date_submitted": review.date_submitted.isoformat() if review.date_submitted else None,
                "verified": review.verified or False
            })
    
    return results


def get_current_user(
    authorization: Optional[str] = Header(None),
    access_token_cookie: Optional[str] = Cookie(None, alias="access_token"),
    db: Session = Depends(get_db)
) -> User:
    """Extract and validate user from request"""
    token = None
    
    if authorization:
        try:
            parts = authorization.split()
            if len(parts) != 2:
                raise HTTPException(status_code=401, detail="Invalid authorization header format")
            scheme, token = parts
            if scheme.lower() != 'bearer':
                raise HTTPException(status_code=401, detail="Invalid authentication scheme")
        except ValueError:
            raise HTTPException(status_code=401, detail="Invalid authorization header")
    elif access_token_cookie:
        token = access_token_cookie
    
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("id")
        
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        
        return user
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except JWTError as e:
        print(f"JWT Error: {str(e)}")
        raise HTTPException(status_code=401, detail="Invalid token")


# Helper function
def format_review_response(review: Review, db: Session) -> dict:
    """Format review with conversation metadata"""
    try:
        conversation_count = db.query(Conversation).filter(
            Conversation.review_id == review.id
        ).count()
        
        admin_response = db.query(Conversation).filter(
            Conversation.review_id == review.id,
            Conversation.sender_type == "admin"
        ).count() > 0
        
        unread_messages = db.query(Conversation).filter(
            Conversation.review_id == review.id,
            Conversation.sender_type == "admin",
            Conversation.is_read == False
        ).count()
        
        return {
            "id": review.id,
            "business_name": review.business_name,
            "review_title": review.review_title,
            "rating": review.rating,
            "review_text": review.review_text,
            "date_submitted": review.date_submitted.isoformat() if review.date_submitted else None,
            "status": review.status,
            "category": review.category,
            "helpful": review.helpful or 0,
            "verified": review.verified or False,
            "admin_response": admin_response,
            "conversation_count": conversation_count,
            "unread_messages": unread_messages,
            "has_conversation": conversation_count > 0
        }
    except Exception as e:
        print(f"Error formatting review response: {str(e)}")
        traceback.print_exc()
        raise


# API Endpoints

@router.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "Reviews API is running",
        "version": "1.0.0",
        "docs": "/docs"
    }


@router.post("/reviews")
async def create_review(
    review: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Submit a new review"""
    try:
        print(f"Creating review for user {current_user.id}")
        
        db_review = Review(
            user_id=current_user.id,
            business_name=review.get("business_name"),
            review_title=review.get("review_title"),
            rating=review.get("rating"),
            review_text=review.get("review_text"),
            category=review.get("category", "General")
        )
        db.add(db_review)
        db.commit()
        db.refresh(db_review)
        
        print(f"✅ Review created successfully: {db_review.id}")
        return format_review_response(db_review, db)
        
    except Exception as e:
        db.rollback()
        print(f"❌ Error creating review: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error creating review: {str(e)}")


@router.get("/reviews")
async def get_reviews(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all reviews for the current user"""
    try:
        print(f"Fetching reviews for user {current_user.id}")
        
        reviews = db.query(Review).filter(
            Review.user_id == current_user.id
        ).order_by(
            Review.date_submitted.desc()
        ).all()
        
        print(f"✅ Found {len(reviews)} reviews for user {current_user.id}")
        
        formatted_reviews = []
        for review in reviews:
            try:
                formatted_reviews.append(format_review_response(review, db))
            except Exception as e:
                print(f"⚠️ Error formatting review {review.id}: {str(e)}")
                continue
        
        return formatted_reviews
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error fetching reviews: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error fetching reviews: {str(e)}")


@router.get("/reviews/conversations")
async def get_reviews_with_conversations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get only reviews that have conversations"""
    try:
        print(f"Fetching reviews with conversations for user {current_user.id}")
        
        reviews = db.query(Review).filter(
            Review.user_id == current_user.id
        ).all()
        
        reviews_with_conversations = []
        for review in reviews:
            try:
                conversation_count = db.query(Conversation).filter(
                    Conversation.review_id == review.id
                ).count()
                
                if conversation_count > 0:
                    reviews_with_conversations.append(format_review_response(review, db))
            except Exception as e:
                print(f"⚠️ Error processing review {review.id}: {str(e)}")
                continue
        
        print(f"✅ Found {len(reviews_with_conversations)} reviews with conversations")
        return reviews_with_conversations
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error fetching conversations: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error fetching conversations: {str(e)}")


@router.get("/reviews/unread-count")
async def get_unread_count(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get total unread message count for the current user"""
    try:
        print(f"Fetching unread count for user {current_user.id}")
        
        # Get all reviews for the current user
        reviews = db.query(Review).filter(
            Review.user_id == current_user.id
        ).all()
        
        print(f"Found {len(reviews)} reviews for user {current_user.id}")
        
        total_unread = 0
        reviews_with_unread = 0
        
        for review in reviews:
            try:
                unread = db.query(Conversation).filter(
                    Conversation.review_id == review.id,
                    Conversation.sender_type == "admin",
                    Conversation.is_read == False
                ).count()
                
                if unread > 0:
                    total_unread += unread
                    reviews_with_unread += 1
            except Exception as e:
                print(f"⚠️ Error counting unread for review {review.id}: {str(e)}")
                continue
        
        print(f"✅ Total unread: {total_unread}, Reviews with unread: {reviews_with_unread}")
        
        return {
            "total_unread": total_unread,
            "reviews_with_unread": reviews_with_unread
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error fetching unread count: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error fetching unread count: {str(e)}")


@router.get("/reviews/{review_id}")
async def get_review(
    review_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get a specific review"""
    try:
        print(f"Fetching review {review_id} for user {current_user.id}")
        
        review = db.query(Review).filter(
            Review.id == review_id,
            Review.user_id == current_user.id
        ).first()
        
        if not review:
            raise HTTPException(status_code=404, detail="Review not found")
        
        return format_review_response(review, db)
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error fetching review: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error fetching review: {str(e)}")


@router.get("/reviews/{review_id}/conversations")
async def get_conversations(
    review_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all conversations for a review"""
    try:
        print(f"Fetching conversations for review {review_id}")
        
        # Verify the review belongs to the current user
        review = db.query(Review).filter(
            Review.id == review_id,
            Review.user_id == current_user.id
        ).first()
        
        if not review:
            raise HTTPException(status_code=404, detail="Review not found")
        
        conversations = db.query(Conversation).filter(
            Conversation.review_id == review_id
        ).order_by(Conversation.timestamp.asc()).all()
        
        print(f"✅ Found {len(conversations)} conversations")
        
        # Convert to dict manually to avoid Pydantic issues
        return [
            {
                "id": conv.id,
                "review_id": conv.review_id,
                "sender_type": conv.sender_type,
                "message": conv.message,
                "timestamp": conv.timestamp.isoformat() if conv.timestamp else None,
                "is_read": conv.is_read or False
            }
            for conv in conversations
        ]
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error fetching conversations: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error fetching conversations: {str(e)}")


@router.post("/conversations")
async def create_conversation(
    conversation: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Add a message to a conversation"""
    try:
        print(f"Creating conversation for review {conversation.get('review_id')}")
        
        review_id = conversation.get("review_id")
        
        # Verify the review belongs to the current user
        review = db.query(Review).filter(
            Review.id == review_id,
            Review.user_id == current_user.id
        ).first()
        
        if not review:
            raise HTTPException(status_code=404, detail="Review not found")
        
        db_conversation = Conversation(
            review_id=review_id,
            sender_type=conversation.get("sender_type"),
            message=conversation.get("message")
        )
        db.add(db_conversation)
        db.commit()
        db.refresh(db_conversation)
        
        print(f"✅ Conversation created: {db_conversation.id}")
        
        return {
            "id": db_conversation.id,
            "review_id": db_conversation.review_id,
            "sender_type": db_conversation.sender_type,
            "message": db_conversation.message,
            "timestamp": db_conversation.timestamp.isoformat() if db_conversation.timestamp else None,
            "is_read": db_conversation.is_read or False
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"❌ Error creating conversation: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error creating conversation: {str(e)}")


@router.put("/conversations/{review_id}/mark-read")
async def mark_conversations_read(
    review_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Mark all admin messages in a review as read"""
    try:
        print(f"Marking conversations as read for review {review_id}")
        
        # Verify the review belongs to the current user
        review = db.query(Review).filter(
            Review.id == review_id,
            Review.user_id == current_user.id
        ).first()
        
        if not review:
            raise HTTPException(status_code=404, detail="Review not found")
        
        conversations = db.query(Conversation).filter(
            Conversation.review_id == review_id,
            Conversation.sender_type == "admin",
            Conversation.is_read == False
        ).all()
        
        for conversation in conversations:
            conversation.is_read = True
        
        db.commit()
        
        print(f"✅ Marked {len(conversations)} conversations as read")
        
        return {
            "message": "Conversations marked as read",
            "count": len(conversations)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"❌ Error marking conversations: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error marking conversations: {str(e)}")


# Health check endpoint
@router.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "Reviews API",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

# --------------------------
# Admin Endpoints
# --------------------------

def format_admin_review_response(review: Review, db: Session) -> dict:
    """Format review for admin with user details"""
    user = db.query(User).filter(User.id == review.user_id).first()
    base_response = format_review_response(review, db)
    base_response.update({
        "user_name": user.name if user else "Unknown User",
        "user_email": user.email if user else "Unknown Email"
    })
    return base_response

@router.get("/admin/reviews")
async def admin_get_all_reviews(
    status: Optional[str] = None,
    page: int = 1,
    limit: int = 10,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Admin: Get all reviews with pagination"""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    query = db.query(Review)
    if status and status != 'all':
        query = query.filter(Review.status == status)
    
    # Calculate total for pagination
    total = query.count()
    
    # Apply pagination
    offset = (page - 1) * limit
    reviews = query.order_by(Review.date_submitted.desc()).offset(offset).limit(limit).all()
    
    return {
        "reviews": [format_admin_review_response(r, db) for r in reviews],
        "total": total,
        "page": page,
        "limit": limit,
        "totalPages": (total + limit - 1) // limit
    }

@router.get("/admin/reviews/{review_id}/conversations")
async def admin_get_conversations(
    review_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Admin: Get conversations for a review"""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    conversations = db.query(Conversation).filter(
        Conversation.review_id == review_id
    ).order_by(Conversation.timestamp.asc()).all()

    return [
            {
                "id": conv.id,
                "review_id": conv.review_id,
                "sender_type": conv.sender_type,
                "message": conv.message,
                "timestamp": conv.timestamp.isoformat() if conv.timestamp else None,
                "is_read": conv.is_read or False
            }
            for conv in conversations
        ]

@router.post("/admin/reviews/{review_id}/reply")
async def admin_reply_review(
    review_id: int,
    message_data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Admin: Reply to a review"""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    review = db.query(Review).filter(Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    
    message = message_data.get("message")
    if not message:
         raise HTTPException(status_code=400, detail="Message required")

    conv = Conversation(
        review_id=review_id,
        sender_type="admin",
        message=message,
        is_read=False # User hasn't read it
    )
    db.add(conv)
    
    # Auto-update status if needed? Maybe 'in_progress' or 'resolved'?
    # User didn't specify, but often replying implies working on it.
    if review.status == 'open': # Assuming 'open' exists
        review.status = 'in_progress'
        
    db.commit()
    db.refresh(conv)
    return {"status": "success", "conversation_id": conv.id}

@router.patch("/admin/reviews/{review_id}/status")
async def admin_update_review_status(
    review_id: int,
    status_data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Admin: Update review status"""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
        
    review = db.query(Review).filter(Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
        
    new_status = status_data.get("status")
    if new_status:
        review.status = new_status
        
    db.commit()
    new_status = status_data.get("status")
    if new_status:
        review.status = new_status
        
    db.commit()
    return {"status": "success", "new_status": review.status}

@router.get("/admin/reviews/users")
async def admin_get_reviews_by_user(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Admin: Get reviews grouped by user with stats"""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
        
    # Aggregate stats: Count, Avg Rating, Last Date
    stats = db.query(
        Review.user_id,
        func.count(Review.id).label('review_count'),
        func.avg(Review.rating).label('average_rating'),
        func.max(Review.date_submitted).label('last_review_date')
    ).group_by(Review.user_id).all()
    
    results = []
    for stat in stats:
        user = db.query(User).filter(User.id == stat.user_id).first()
        if user:
            # Count unread messages for this user's reviews
            # This might be expensive if many users. Optimization: Subquery or separate count.
            # detailed method:
            user_reviews = db.query(Review.id).filter(Review.user_id == user.id).all()
            review_ids = [r.id for r in user_reviews]
            
            unread_count = 0
            if review_ids:
                unread_count = db.query(Conversation).filter(
                    Conversation.review_id.in_(review_ids),
                    Conversation.sender_type == 'user',
                    Conversation.is_read == False 
                ).count()
            
            results.append({
                "user_id": user.id,
                "user_name": user.name,
                "user_email": user.email,
                "review_count": stat.review_count,
                "average_rating": round(float(stat.average_rating or 0), 1),
                "last_review_date": stat.last_review_date.isoformat() if stat.last_review_date else None,
                "unread_messages": unread_count
            })
            
    # Sort by unread messages desc, then last date desc
    results.sort(key=lambda x: (x['unread_messages'], x['last_review_date'] or ''), reverse=True)
    
    return results

@router.post("/admin/reviews/{review_id}/attended")
async def admin_toggle_review_attended(
    review_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Admin: Toggle review attended status"""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    review = db.query(Review).filter(Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
        
    # Toggle (initialize if None)
    current_val = review.is_attended if hasattr(review, 'is_attended') else False
    review.is_attended = not current_val
    
    db.commit()
    
    return {
        "status": "success", 
        "is_attended": review.is_attended,
        "message": "Review marked as attended" if review.is_attended else "Review marked as unattended"
    }


@router.get("/admin/reviews/user/{target_user_id}")
async def admin_get_user_reviews(
    target_user_id: int,
    page: int = 1,
    limit: int = 10,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Admin: Get all reviews for a specific user with pagination"""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
        
    query = db.query(Review).filter(
        Review.user_id == target_user_id
    )
    
    total = query.count()
    offset = (page - 1) * limit
    
    reviews = query.order_by(Review.date_submitted.desc()).offset(offset).limit(limit).all()
    
    formatted_reviews = []
    for r in reviews:
        resp = format_admin_review_response(r, db)
        # Add is_attended manually if not in helper yet (it might be added to helper later)
        resp['is_attended'] = getattr(r, 'is_attended', False)
        formatted_reviews.append(resp)

    return {
        "reviews": formatted_reviews,
        "total": total,
        "page": page,
        "limit": limit,
        "totalPages": (total + limit - 1) // limit
    }


# --------------------------
# Displayed Reviews Endpoints (Homepage Management)
# --------------------------

@router.post("/admin/reviews/{review_id}/display")
async def admin_add_review_to_display(
    review_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Admin: Add a review to the homepage display collection.
    This makes the review visible to all visitors on the home page.
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    # Import here to avoid circular dependency
    from database.pg_models import DisplayedReview
    
    # Check if review exists
    review = db.query(Review).filter(Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    
    # Check if already displayed
    existing = db.query(DisplayedReview).filter(DisplayedReview.review_id == review_id).first()
    if existing:
        return {"status": "already_displayed", "message": "Review is already in display collection"}
    
    # Get the next display order (highest + 1)
    max_order = db.query(func.max(DisplayedReview.display_order)).scalar() or 0
    
    # Add to displayed reviews
    displayed = DisplayedReview(
        review_id=review_id,
        display_order=max_order + 1,
        added_by=current_user.id
    )
    db.add(displayed)
    db.commit()
    
    return {
        "status": "success",
        "message": "Review added to homepage display",
        "display_order": max_order + 1
    }


@router.delete("/admin/reviews/{review_id}/display")
async def admin_remove_review_from_display(
    review_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Admin: Remove a review from the homepage display collection.
    This hides the review from visitors on the home page.
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    from database.pg_models import DisplayedReview
    
    # Find and delete the displayed review entry
    displayed = db.query(DisplayedReview).filter(DisplayedReview.review_id == review_id).first()
    if not displayed:
        raise HTTPException(status_code=404, detail="Review is not in display collection")
    
    db.delete(displayed)
    db.commit()
    
    return {
        "status": "success",
        "message": "Review removed from homepage display"
    }


@router.get("/admin/reviews/displayed")
async def admin_get_displayed_reviews(
    page: int = 1,
    limit: int = 10,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Admin: Get all reviews currently displayed on the homepage.
    Returns reviews ordered by display_order (lower = higher priority).
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    from database.pg_models import DisplayedReview
    
    # Query displayed reviews with their review data
    query = db.query(DisplayedReview).order_by(DisplayedReview.display_order)
    
    total = query.count()
    offset = (page - 1) * limit
    displayed_reviews = query.offset(offset).limit(limit).all()
    
    # Format response with full review details
    results = []
    for dr in displayed_reviews:
        review = db.query(Review).filter(Review.id == dr.review_id).first()
        if review:
            review_data = format_admin_review_response(review, db)
            review_data['display_order'] = dr.display_order
            review_data['added_at'] = dr.added_at.isoformat() if dr.added_at else None
            results.append(review_data)
    
    return {
        "reviews": results,
        "total": total,
        "page": page,
        "limit": limit,
        "totalPages": (total + limit - 1) // limit if total > 0 else 1
    }

