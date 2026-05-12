
from fastapi import APIRouter, HTTPException, Depends, WebSocket, WebSocketDisconnect, Cookie
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from typing import List, Dict
from jose import jwt, JWTError
from api.routes.auth.login import SECRET_KEY, ALGORITHM

from database.pg_connections import get_db
from database.pg_models import User, Ticket, TicketMessage, TicketCreate, MessageCreate, TicketResponse, MessageResponse
from api.routes.auth.login import get_current_user

from typing import Optional
import json

router = APIRouter(prefix="/customer-service", tags=["customer-service"])

class NotificationManager:
    def __init__(self):
        self.active_connections: Dict[int, List[WebSocket]] = {}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)

    def disconnect(self, user_id: int, websocket: WebSocket):
        if user_id in self.active_connections:
            if websocket in self.active_connections[user_id]:
                self.active_connections[user_id].remove(websocket)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]

    async def send_personal_message(self, message: str, user_id: int):
        if user_id in self.active_connections:
            for connection in self.active_connections[user_id]:
                try:
                    await connection.send_text(message)
                except:
                    pass

notification_manager = NotificationManager()

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except:
                pass

manager = ConnectionManager()

@router.websocket("/ws/admin/reports")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@router.websocket("/ws/notifications")
async def notification_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time notifications.
    Uses a local session context for auth to avoid holding DB connections open.
    """
    # ALWAYS accept first to avoid "closed before established" errors
    await websocket.accept()
    
    # Authenticate via Cookie or Query Param
    token = websocket.cookies.get("access_token")
    if not token:
        token = websocket.query_params.get("token")
    
    if not token:
        print("WS Auth Fail: No token provided")
        await websocket.close(code=1008)
        return

    # Handle 'Bearer ' prefix just in case
    if token.startswith("Bearer "):
        token = token.replace("Bearer ", "")

    from database.pg_connections import SessionLocal

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        if not email:
            print("WS Auth Fail: No sub in payload")
            await websocket.close(code=1008)
            return
        
        # Use a short-lived session for authentication
        with SessionLocal() as session:
            user = session.query(User).filter(User.email == email).first()
            if not user:
                print(f"WS Auth Fail: User {email} not found")
                await websocket.close(code=1008)
                return
            user_id = user.id

        # Now connect to manager (note: managerial handshake already accepted)
        # We need a tailored way to add to active_connections since we already accepted
        if user_id not in notification_manager.active_connections:
            notification_manager.active_connections[user_id] = []
        notification_manager.active_connections[user_id].append(websocket)
        
        print(f"✓ Notifications WS established for user {user_id}")
        try:
            while True:
                # Receive messages but ignore them (keeps connection alive)
                await websocket.receive_text()
        except WebSocketDisconnect:
            notification_manager.disconnect(user_id, websocket)

    except Exception as e:
        print(f"WebSocket auth error: {e}")
        try:
            await websocket.close(code=1008)
        except:
            pass

def extract_user_id(current_user):
    """Helper function to extract user_id from current_user"""
    if isinstance(current_user, dict):
        if "user" in current_user:
            user_data = current_user
            if isinstance(user_data, dict):
                return user_data.get("id") or user_data.get("user_id")
            elif hasattr(user_data, 'id'):
                return user_data.id
            else:
                return user_data
        else:
            return current_user.get("id") or current_user.get("user_id") or current_user.get("sub")
    else:
        return current_user.id

# USER ENDPOINTS

@router.post("/tickets")
@router.post("/tickets/create")
async def create_ticket(
    ticket_data: TicketCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    print(f"Current user: {current_user}")
    print(f"User ID: {extract_user_id(current_user)}")
    """
    Create a new support ticket
    """
    try:
        user_id = extract_user_id(current_user)
        
        if not ticket_data.issue.strip():
            raise HTTPException(status_code=400, detail="Issue description is required")
        
        # Create ticket
        new_ticket = Ticket(
            user_id=user_id,
            issue=ticket_data.issue.strip(),
            category=ticket_data.category,
            status="open",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        
        db.add(new_ticket)
        db.commit()
        db.refresh(new_ticket)
        
        # Create initial message from user
        initial_message = TicketMessage(
            ticket_id=new_ticket.id,
            sender_id=user_id,
            sender_role="user",
            message=ticket_data.issue.strip(),
            is_read=False,  # Unread by Admin
            created_at=datetime.now(timezone.utc)
        )
        
        db.add(initial_message)
        db.commit()
        
        return {
            "message": "Ticket created successfully",
            "ticket_id": new_ticket.id
        }
        
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        print(f"Error creating ticket: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/tickets")
@router.get("/tickets/my-tickets")
async def get_my_tickets(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get all tickets for the current user with unread message counts
    """
    try:
        user_id = extract_user_id(current_user)
        
        tickets = db.query(Ticket).filter(
            Ticket.user_id == user_id
        ).order_by(Ticket.updated_at.desc()).all()
        
        result = []
        for ticket in tickets:
            # Get unread message count (messages from admin that user hasn't read)
            unread_count = db.query(TicketMessage).filter(
                TicketMessage.ticket_id == ticket.id,
                TicketMessage.sender_role == "admin",
                TicketMessage.is_read == False
            ).count()
            
            # Get last message
            last_message = db.query(TicketMessage).filter(
                TicketMessage.ticket_id == ticket.id
            ).order_by(TicketMessage.created_at.desc()).first()
            
            result.append({
                "id": ticket.id,
                "user_id": ticket.user_id,
                "issue": ticket.issue,
                "category": ticket.category,
                "status": ticket.status,
                "created_at": ticket.created_at,
                "updated_at": ticket.updated_at,
                "unread_count": unread_count,
                "last_message": last_message.message if last_message else None,
                "last_message_at": last_message.created_at if last_message else None
            })
        
        return {"tickets": result}
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/tickets/{ticket_id}/messages")
async def get_ticket_messages( ticket_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Get all messages for a specific ticket
    """
    try:
        user_id = extract_user_id(current_user)
        print(f"Loading messages for ticket {ticket_id}, user {user_id}")  # Debug log
        
        # Check if user is admin (allow access) or ticket owner
        user = db.query(User).filter(User.id == user_id).first()
        is_admin = user and user.is_admin

        # Verify ticket belongs to user OR user is admin
        query = db.query(Ticket).filter(Ticket.id == ticket_id)
        if not is_admin:
            query = query.filter(Ticket.user_id == user_id)
        
        ticket = query.first()
        
        if not ticket:
            print(f"Ticket {ticket_id} not found for user {user_id}")  # Debug log
            raise HTTPException(status_code=404, detail="Ticket not found")
        
        # Get all messages
        messages = db.query(TicketMessage).filter(
            TicketMessage.ticket_id == ticket_id
        ).order_by(TicketMessage.created_at.asc()).all()
        
        print(f"Found {len(messages)} messages")  # Debug log
        
        # Mark admin messages as read
        try:
            for message in messages:
                if message.sender_role == "admin" and not message.is_read:
                    message.is_read = True
                    print(f"Marking message {message.id} as read")  # Debug log
            
            db.commit()
        except Exception as e:
            print(f"Error marking messages as read: {str(e)}")  # Debug log
            db.rollback()
            # Don't fail the request if we can't mark as read
        
        # Format messages with sender info
        result = []
        for msg in messages:
            try:
                sender = db.query(User).filter(User.id == msg.sender_id).first()
                if sender:
                    # Try these in order: full_name, name, email
                    sender_name = (
                        getattr(sender, 'full_name', None) or 
                        getattr(sender, 'name', None) or 
                        getattr(sender, 'email', 'Admin')
                    )
                else:
                    sender_name = "Admin"
                
                result.append({
                    "id": msg.id,
                    "ticket_id": msg.ticket_id,
                    "sender_id": msg.sender_id,
                    "sender_name": sender_name,
                    "sender_role": msg.sender_role,
                    "message": msg.message,
                    "is_read": msg.is_read,
                    "created_at": msg.created_at.isoformat() if msg.created_at else None
                })
            except Exception as e:
                print(f"Error formatting message {msg.id}: {str(e)}")  # Debug log
                # Skip malformed messages
                continue
        
        return {
            "ticket": {
                "id": ticket.id,
                "issue": ticket.issue,
                "status": ticket.status,
                "created_at": ticket.created_at.isoformat() if ticket.created_at else None
            },
            "messages": result
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in get_ticket_messages: {str(e)}")  # Debug log
        import traceback
        traceback.print_exc()  # Print full traceback
        raise HTTPException(status_code=400, detail=f"Error loading messages: {str(e)}")


@router.post("/tickets/{ticket_id}/reply")
async def reply_to_ticket(
    ticket_id: int, 
    message_data: MessageCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    User replies to a ticket
    """
    try:
        user_id = extract_user_id(current_user)
        
        # Verify ticket belongs to user
        ticket = db.query(Ticket).filter(
            Ticket.id == ticket_id,
            Ticket.user_id == user_id
        ).first()
        
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        
        if not message_data.message.strip():
            raise HTTPException(status_code=400, detail="Message cannot be empty")
        
        # Get user info
        user = db.query(User).filter(User.id == user_id).first()
        user_name = user.name if user and hasattr(user, 'name') else "User"
        
        # Create message
        new_message = TicketMessage(
            ticket_id=ticket_id,
            sender_id=user_id,
            sender_role="user",
            message=message_data.message.strip(),
            is_read=False,
            created_at=datetime.now(timezone.utc)
        )
        
        db.add(new_message)
        
        # Update ticket status to in_progress if it was resolved/closed
        if ticket.status in ["resolved", "closed"]:
            ticket.status = "in_progress"
        
        ticket.updated_at = datetime.now(timezone.utc)
        
        db.commit()
        db.refresh(new_message)
        
        # Notify Admins via WebSocket
        await manager.broadcast(
            json.dumps({
                "type": "new_message",
                "payload": {
                    "id": new_message.id,
                    "user_id": user_id,
                    "ticket_id": ticket_id,
                    "sender_role": "user",
                    "sender_name": user_name,
                    "content": new_message.message,
                    "created_at": new_message.created_at.isoformat()
                }
            })
        )
        
        return {"message": "Reply sent successfully", "message_id": new_message.id}
        
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        print(f"Error in reply_to_ticket: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
        

# ADMIN ENDPOINTS

@router.get("/admin/tickets")
async def get_all_tickets(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    status: Optional[str] = None
):
    """
    Get all tickets (admin only)
    """
    try:
        user_id = extract_user_id(current_user)
        
        # Check if user is admin
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")
        
        # Query tickets
        query = db.query(Ticket)
        if status:
            query = query.filter(Ticket.status == status)
        
        tickets = query.order_by(Ticket.updated_at.desc()).all()
        
        result = []
        for ticket in tickets:
            # Get user info
            ticket_user = db.query(User).filter(User.id == ticket.user_id).first()
            
            # Get unread message count (messages from user that admin hasn't read)
            unread_count = db.query(TicketMessage).filter(
                TicketMessage.ticket_id == ticket.id,
                TicketMessage.sender_role == "user",
                TicketMessage.is_read == False
            ).count()
            
            # Get last message
            last_message = db.query(TicketMessage).filter(
                TicketMessage.ticket_id == ticket.id
            ).order_by(TicketMessage.created_at.desc()).first()
            
            result.append({
                "id": ticket.id,
                "user_id": ticket.user_id,
                "user_name": ticket_user.name if ticket_user else "Unknown",
                "user_email": ticket_user.email if ticket_user else "Unknown",
                "issue": ticket.issue,
                "category": ticket.category,
                "status": ticket.status,
                "created_at": ticket.created_at,
                "updated_at": ticket.updated_at,
                "unread_count": unread_count,
                "last_message": last_message.message if last_message else None,
                "last_message_at": last_message.created_at if last_message else None
            })
        
        return {"tickets": result}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/admin/tickets/{ticket_id}/reply")
async def admin_reply_to_ticket(
    ticket_id: int,
    message_data: MessageCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Admin replies to a ticket
    """
    try:
        user_id = extract_user_id(current_user)
        
        # Check if user is admin
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")
        
        # Verify ticket exists
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        
        if not message_data.message.strip():
            raise HTTPException(status_code=400, detail="Message cannot be empty")
        
        # Create message
        new_message = TicketMessage(
            ticket_id=ticket_id,
            sender_id=user_id,
            sender_role="admin",
            message=message_data.message.strip(),
            is_read=False,
            created_at=datetime.now(timezone.utc)
        )
        
        db.add(new_message)
        
        # Update ticket status to in_progress if it's open or resolved
        if ticket.status in ["open", "resolved"]:
            ticket.status = "in_progress"
        
        ticket.updated_at = datetime.now(timezone.utc)
        
        db.commit()
        db.refresh(new_message)
        
        # Get sender name for WebSocket notification
        sender_name = user.name if hasattr(user, 'name') else "Admin"
        
        # Prepare message data as dict for proper JSON serialization
        message_payload = {
            "id": new_message.id,
            "ticket_id": ticket_id,
            "sender_role": "admin",
            "sender_name": sender_name,
            "message": new_message.message,
            "created_at": new_message.created_at.isoformat()
        }
        
        # Notify user via WebSocket
        await notification_manager.send_personal_message(
            json.dumps({
                "type": "new_message",
                "message": message_payload
            }),
            ticket.user_id
        )
        
        # Notify other admins (NOT the sender)
        await manager.broadcast(
            json.dumps({
                "type": "admin_message_sent",
                "message": {
                    **message_payload,
                    "user_id": ticket.user_id
                },
                "sender_id": user_id  # Include sender ID so they can ignore their own message
            })
        )
        
        return {"message": "Reply sent successfully", "message_id": new_message.id}
        
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        print(f"Error in admin_reply_to_ticket: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/admin/tickets/{ticket_id}/status")
async def update_ticket_status(
    ticket_id: int,
    status: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Update ticket status (admin only)
    """
    try:
        user_id = extract_user_id(current_user)
        
        # Check if user is admin
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")
        
        # Validate status
        valid_statuses = ["open", "in_progress", "resolved", "closed"]
        if status not in valid_statuses:
            raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}")
        
        # Update ticket
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        
        ticket.status = status
        ticket.updated_at = datetime.now(timezone.utc)
        
        # If status is resolved, add a system message
        if status == "resolved":
            system_msg = TicketMessage(
                ticket_id=ticket.id,
                sender_id=user_id, # Admin ID
                sender_role="system",
                message="Ticket Resolved",
                is_read=True, 
                created_at=datetime.now(timezone.utc)
            )
            db.add(system_msg)
            
        db.commit()
        
        return {"message": f"Ticket status updated to {status}"}
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/admin/conversations")
async def get_conversations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get all conversations grouped by user (Admin view)
    Sort by: 
    1. Unread count > 0 (New Messages) DESC
    2. Last message timestamp DESC
    """
    try:
        user_id = extract_user_id(current_user)
        # Check admin
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.is_admin:
             raise HTTPException(status_code=403, detail="Admin access required")
             
        # Get users who have tickets
        # Efficient way: Query distinct user_ids from Ticket
        ticket_users = db.query(Ticket.user_id).distinct().all()
        # Use set to ensure absolute uniqueness (though distinct should handle it)
        user_ids = list(set([t[0] for t in ticket_users if t[0] is not None]))
        
        results = []
        for uid in user_ids:
            u = db.query(User).filter(User.id == uid).first()
            if not u: continue
            
            # Get all tickets for this user
            user_tickets = db.query(Ticket).filter(Ticket.user_id == uid).all()
            ticket_ids = [t.id for t in user_tickets]
            
            if not ticket_ids: continue
            
            # Count unread messages (from user, not read by admin)
            unread_count = db.query(TicketMessage).filter(
                TicketMessage.ticket_id.in_(ticket_ids),
                TicketMessage.sender_role == 'user',
                TicketMessage.is_read == False
            ).count()
            
            # Get last message
            last_msg = db.query(TicketMessage).filter(
                TicketMessage.ticket_id.in_(ticket_ids)
            ).order_by(TicketMessage.created_at.desc()).first()
            
            # Determine status based on LAST ticket status, not just unread count
            # Find the most recently updated ticket for this user
            latest_ticket = db.query(Ticket).filter(Ticket.user_id == uid)\
                .order_by(Ticket.updated_at.desc()).first()
            
            ticket_status = latest_ticket.status if latest_ticket else "closed"

            # Logic:
            # - If unread_count > 0: "active" (New Messages)
            # - If unread_count == 0:
            #    - If ticket_status is resolved: "resolved"
            #    - Else: "caught_up"
            
            final_status = "active" if unread_count > 0 else ("resolved" if ticket_status == 'resolved' else "caught_up")

            # FIXED: Only append once with all the data
            results.append({
                "user_id": u.id,
                "user_name": u.name,
                "user_email": u.email,
                "unread_count": unread_count,
                "last_message": last_msg.message if last_msg else None,
                "last_message_at": last_msg.created_at.isoformat() if last_msg else None,
                "status": final_status
            })
            
        # Sort keys: 
        # 1. Unread > 0 (Primary) - Boolean sort (False=0, True=1). We want True first, so reverse.
        # 2. Last message time (Secondary)
        results.sort(key=lambda x: (x['unread_count'] > 0, x['last_message_at'] or ""), reverse=True)
        
        return {"conversations": results}
        
    except Exception as e:
        print(f"Error getting conversations: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/users/{target_user_id}/messages")
async def get_user_messages(
    target_user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get combined message history for a user across all tickets
    """
    try:
         # Check admin
        user = db.query(User).filter(User.id == extract_user_id(current_user)).first()
        if not user or not user.is_admin:
             raise HTTPException(status_code=403, detail="Admin access required")
             
        # Get all tickets
        tickets = db.query(Ticket).filter(Ticket.user_id == target_user_id).all()
        ticket_ids = [t.id for t in tickets]
        
        if not ticket_ids:
            return {"messages": []}
            
        # Get all messages
        messages = db.query(TicketMessage).filter(
            TicketMessage.ticket_id.in_(ticket_ids)
        ).order_by(TicketMessage.created_at.asc()).all()
        
        # Mark user messages as read
        try:
            for msg in messages:
                if msg.sender_role == 'user' and not msg.is_read:
                    msg.is_read = True
            db.commit()
        except:
            db.rollback()
            
        result = []
        for msg in messages:
            result.append({
                "id": msg.id,
                "sender_role": msg.sender_role,
                "message": msg.message,
                "created_at": msg.created_at.isoformat(),
                "ticket_id": msg.ticket_id
            })
            
        return {"messages": result}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tickets/unread-count")
async def get_user_unread_count(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get count of unread messages for the current user
    """
    try:
        user_id = extract_user_id(current_user)
        
        # Get user's tickets
        user_tickets = db.query(Ticket).filter(Ticket.user_id == user_id).all()
        ticket_ids = [t.id for t in user_tickets]
        
        if not ticket_ids:
            return {"count": 0}
            
        # Count unread messages (from admin, not read)
        count = db.query(TicketMessage).filter(
            TicketMessage.ticket_id.in_(ticket_ids),
            TicketMessage.sender_role == 'admin',
            TicketMessage.is_read == False
        ).count()
        
        return {"count": count}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/unread-count")
async def get_admin_unread_count(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get count of users with unread messages (Admin sidebar badge)
    """
    try:
        # Check admin
        user = db.query(User).filter(User.id == extract_user_id(current_user)).first()
        if not user or not user.is_admin:
             raise HTTPException(status_code=403, detail="Admin access required")
             
        # Count messages from users that are unread
        # We want the number of *tickets* or *users* that need attention?
        # Usually notifications show "N new messages".
        # But the sidebar badge usually shows "N users have messaged" or "N total unread msgs".
        # Let's count total unread messages from users.
        
        count = db.query(TicketMessage).filter(
            TicketMessage.sender_role == 'user',
            TicketMessage.is_read == False
        ).count()
        
        # Also could return breakdown if needed later
        return {"count": count}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/users/{target_user_id}/resolve_all")
async def resolve_all_user_tickets(
    target_user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Mark all open tickets for a user as resolved and create a persistent system message
    """
    try:
        # Check admin
        user_id = extract_user_id(current_user)
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")
            
        # Get all open/in_progress tickets
        tickets = db.query(Ticket).filter(
            Ticket.user_id == target_user_id,
            Ticket.status.in_(['open', 'in_progress'])
        ).all()
        
        count = 0
        system_messages = []
        
        for ticket in tickets:
            ticket.status = 'resolved'
            ticket.updated_at = datetime.now(timezone.utc)
            count += 1
            
            # Create a PERSISTENT system message for each resolved ticket
            system_message = TicketMessage(
                ticket_id=ticket.id,
                sender_id=user_id,
                sender_role="system",
                message="Ticket Resolved",
                is_read=True,
                created_at=datetime.now(timezone.utc)
            )
            db.add(system_message)
            system_messages.append((ticket.id, system_message))
            
        db.commit()
        
        # Refresh all system messages and notify via WebSocket
        for ticket_id, sys_msg in system_messages:
            db.refresh(sys_msg)
            
            await manager.broadcast(
                json.dumps({
                    "type": "ticket_resolved",
                    "payload": {
                        "user_id": target_user_id,
                        "ticket_id": ticket_id,
                        "message": {
                            "id": sys_msg.id,
                            "sender_role": "system",
                            "message": "Ticket Resolved",
                            "created_at": sys_msg.created_at.isoformat(),
                            "ticket_id": ticket_id
                        }
                    }
                })
            )
        
        return {"message": f"Resolved {count} tickets", "count": count}
        
    except Exception as e:
        db.rollback()
        print(f"Error in resolve_all_user_tickets: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))