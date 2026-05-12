
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
import os
from sqlalchemy.orm import Session
from database.pg_models import User
from dotenv import dotenv_values

class BetaService:
    
    @staticmethod
    def _get_dynamic_env() -> Dict[str, str]:
        """Manually read .env to pick up changes instantly without library caching"""
        env_vars = {}
        try:
            # Root is parent of subscriptions/
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env_path = os.path.join(root, ".env")
            if os.path.exists(env_path):
                with open(env_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        if '=' in line:
                            key, val = line.split('=', 1)
                            # Remove optional quotes
                            value = val.strip()
                            if (value.startswith('"') and value.endswith('"')) or \
                               (value.startswith("'") and value.endswith("'")):
                                value = value[1:-1]
                            env_vars[key.strip()] = value
        except:
            pass
        return env_vars

    @staticmethod
    def get_app_mode() -> str:
        """
        Get current application mode: 'beta', 'launch', or 'development'
        """
        env_vars = BetaService._get_dynamic_env()
        
        mode = env_vars.get("APP_MODE")
        if mode is None:
            mode = os.getenv("APP_MODE", "")
            
        mode = str(mode).lower().strip()
        if mode in ["beta", "launch", "development"]:
            return mode
            
        # Legacy support / Fallback
        legacy_beta = env_vars.get("BETA_MODE")
        if legacy_beta is None:
            legacy_beta = os.getenv("BETA_MODE", "false")
            
        if str(legacy_beta).lower().strip() == "true":
            return "beta"
            
        return "launch"

    @staticmethod
    def is_beta_mode() -> bool:
        """Check if system is in beta mode (or launch mode before launch date)"""
        mode = BetaService.get_app_mode()
        if mode == "beta":
            return True
            
        if mode == "launch":
            launch_date = BetaService.get_launch_date()
            if launch_date:
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                if now < launch_date:
                    return True
                    
        return False
    
    @staticmethod
    def get_launch_date() -> Optional[datetime]:
        """Get configured launch date (supports DD/MM/YYYY)"""
        env_vars = BetaService._get_dynamic_env()
        launch_str = env_vars.get("LAUNCH_DATE") or os.getenv("LAUNCH_DATE")
        if launch_str:
            try:
                # Try DD/MM/YYYY first as requested by user
                return datetime.strptime(launch_str, "%d/%m/%Y")
            except ValueError:
                try:
                    # Fallback to ISO format
                    return datetime.strptime(launch_str, "%Y-%m-%d")
                except ValueError:
                    return None
        return None
    
    @staticmethod
    def get_grace_period_days() -> int:
        """Get grace period duration (default: 5 days)"""
        env_vars = BetaService._get_dynamic_env()
        days = env_vars.get("GRACE_PERIOD_DAYS")
        if days is None:
            days = os.getenv("GRACE_PERIOD_DAYS", "5")
        try:
            return int(days)
        except:
            return 5
    
    @staticmethod
    def calculate_grace_period_end(launch_date: datetime) -> datetime:
        """Calculate when grace period ends"""
        grace_days = BetaService.get_grace_period_days()
        return launch_date + timedelta(days=grace_days)
    
    @staticmethod
    def is_in_grace_period(user: Optional[User] = None) -> bool:
        """
        Check if we're currently in grace period.
        If user is provided, checks their specific grace period.
        """
        # If in beta, we are NOT in grace period yet
        if BetaService.is_beta_mode():
            return False
        
        now = datetime.now(timezone.utc).replace(tzinfo=None) # Standardize to naive UTC

        # Check user-specific grace period first
        if user and user.grace_period_ends_at:
            grace_end = user.grace_period_ends_at
            if getattr(user, 'is_beta_user', False):
                launch_date = BetaService.get_launch_date()
                if launch_date:
                    grace_end = BetaService.calculate_grace_period_end(launch_date)
                    
            if now < grace_end:
                return True
        
        # Fallback to global launch date
        launch_date = BetaService.get_launch_date()
        if launch_date:
            # If we have a launch date and it hasn't happened yet, we aren't in grace
            if now < launch_date:
                return False
            
            grace_end = BetaService.calculate_grace_period_end(launch_date)
            if now < grace_end:
                return True
                
        return False
    
    @staticmethod
    def has_saved_card(user: User) -> bool:
        """Check if user has a saved payment method"""
        return bool(user.stripe_payment_method_id)
    
    @staticmethod
    def get_user_status(user: User) -> Dict:
        """
        Get comprehensive user status for dashboard display
        
        Returns dict with:
        - status: 'beta_no_card', 'beta_with_card', 'grace_no_card', 'grace_with_card', 'active', 'new_user'
        - message: Display message
        - action_required: Boolean
        - countdown_ends_at: Datetime or None
        - days_remaining: Integer or None
        """
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        # DEBUG: Check if .env is being read
        env_vars = BetaService._get_dynamic_env()
        debug_launch_date = env_vars.get("LAUNCH_DATE")
        
        launch_date = BetaService.get_launch_date()
        has_card = BetaService.has_saved_card(user)
        
        # Result dict
        result = {}
        
        # ── ACTIVE SUBSCRIPTION (PRIORITY) ──────────────────────────────────
        # Check active subscription FIRST — if user paid, no timer needed 
        # regardless of card status or grace period.
        if user.subscription_status == "active":
            days_rem = None
            if user.subscription_expires_at:
                 expires_naive = user.subscription_expires_at.replace(tzinfo=None) if user.subscription_expires_at.tzinfo else user.subscription_expires_at
                 days_rem = (expires_naive - now).days

            return {
                "status": "active",
                "message": "Your subscription is active",
                "action_required": False,
                "countdown_ends_at": user.subscription_expires_at,
                "days_remaining": days_rem,
                "show_card_info": True,
                "is_beta_user": getattr(user, 'is_beta_user', False)
            }

        # ── INDIVIDUAL BETA USER GRACE EXPIRY (PRIORITY OVER GLOBAL BETA MODE) ──
        # A beta user whose personal grace period has ended must be restricted
        # even if APP_MODE=beta is still set globally. Check this before the
        # global is_beta_mode() guard so expired users are never bypassed.
        if getattr(user, 'is_beta_user', False) and user.grace_period_ends_at and not has_card:
            grace_naive = user.grace_period_ends_at.replace(tzinfo=None) if user.grace_period_ends_at.tzinfo else user.grace_period_ends_at
            if now >= grace_naive:
                return {
                    "status": "grace_expired_no_card",
                    "message": "Your grace period has ended. Subscribe to restore full access.",
                    "action_required": True,
                    "countdown_ends_at": None,
                    "days_remaining": None,
                    "show_card_info": False,
                    "is_beta_user": True
                }

        # BETA PERIOD (Before Launch)
        if BetaService.is_beta_mode():
            if has_card:
                # Card saved in beta mode - banner hides, billing happens at launch
                app_mode = BetaService.get_app_mode()
                return {
                    "status": "beta_with_card",
                    "message": f"You're all set! Card saved. (Verified: {datetime.now().strftime('%H:%M:%S')})",
                    "action_required": False,
                    "countdown_ends_at": launch_date if app_mode == "launch" else None,
                    "days_remaining": None,
                    "show_card_info": True,
                    "is_beta_user": True
                }
            else:
                app_mode = BetaService.get_app_mode()
                return {
                    "status": "beta_no_card",
                    "message": f"Save your card now! (Verified: {datetime.now().strftime('%H:%M:%S')})",
                    "action_required": True,
                    "countdown_ends_at": launch_date if app_mode == "launch" else None,
                    "days_remaining": None,
                    "show_card_info": False,
                    "is_beta_user": True
                }
        
        # GRACE PERIOD (Launch Day + 5 Days OR Signup Day + 5 Days)
        

        if BetaService.is_in_grace_period(user):
            grace_end = user.grace_period_ends_at
            is_beta = getattr(user, 'is_beta_user', False)
            
            # If user has no personal grace end, or if they are a beta user (to ensure .env changes are instantly reflected),
            # we should calculate it dynamically.
            if not grace_end or is_beta:
                 launch_date = BetaService.get_launch_date()
                 if launch_date:
                     grace_end = BetaService.calculate_grace_period_end(launch_date)

            if not grace_end:
                 app_mode = BetaService.get_app_mode()
                 launch_date = BetaService.get_launch_date()
                 return {
                    "status": "new_user",
                    "message": "Subscribe to get started with Lavoo",
                    "action_required": True,
                    "countdown_ends_at": launch_date if app_mode == "launch" else None,
                    "days_remaining": None,
                    "show_card_info": False
                }

            time_remaining = grace_end - now
            days_rem = time_remaining.days
            
            is_beta = getattr(user, 'is_beta_user', False)
            if has_card:
                # Card saved - banner hides (billing is immediate in launch mode → user becomes active)
                return {
                    "status": "grace_with_card",
                    "message": "",
                    "action_required": False,
                    "countdown_ends_at": None,
                    "days_remaining": None,
                    "show_card_info": True,
                    "is_beta_user": is_beta
                }
            else:
                # No card saved — show the notice with countdown
                message = (
                    "Save your card now to secure your access after the beta period!"
                    if is_beta
                    else "Subscribe now to keep your access to Lavoo!"
                )
                return {
                    "status": "grace_no_card",
                    "message": message,
                    "action_required": True,
                    "countdown_ends_at": grace_end,
                    "days_remaining": days_rem,
                    "hours_remaining": (time_remaining.seconds // 3600),
                    "minutes_remaining": (time_remaining.seconds % 3600) // 60,
                    "seconds_remaining": (time_remaining.seconds % 60),
                    "show_card_info": False,
                    "is_beta_user": is_beta
                }
        
        # AFTER GRACE PERIOD (Access Paused)
        
        # Pause access if grace period is over and no card/active sub
        if user.grace_period_ends_at:
             grace_naive = user.grace_period_ends_at.replace(tzinfo=None) if user.grace_period_ends_at.tzinfo else user.grace_period_ends_at
             if now >= grace_naive:
                 return {
                    "status": "grace_expired_no_card",
                    "message": "Your access is paused. Add a payment method to reactivate.",
                    "action_required": True,
                    "countdown_ends_at": None,
                    "days_remaining": None,
                    "show_card_info": False
                }

        # NEW USER (No Grace Period set yet)
        app_mode = BetaService.get_app_mode()
        launch_date = BetaService.get_launch_date()
        
        status_data = {
            "status": "new_user",
            "message": "Subscribe to get started with Lavoo",
            "action_required": True,
            "countdown_ends_at": launch_date if app_mode == "launch" else None,
            "days_remaining": None,
            "show_card_info": False
        }
        
        # Add debug info to all returns by wrapping the logic better or just adding it here for the current branch
        # Since I can't easily refactor the whole thing with multi-replace without risking errors, 
        # I'll just add it to the final return for now. 
        # But wait, I should really add it to ALL returns.
        
        # ACTUALLY, I'll just return the status_data with debug info added.
        status_data.update({
            "debug_now": now.isoformat(),
            "debug_launch_date_raw": debug_launch_date,
            "debug_launch_date_parsed": launch_date.isoformat() if launch_date else None,
            "debug_app_mode": BetaService.get_app_mode()
        })
        return status_data
    
    @staticmethod
    def initialize_grace_period(user: User, db: Session):
        """
        Initialize grace period based on user type:
        - Beta users: 5 days from launch date
        - New users: 5 days from signup date
        """
        is_beta = BetaService.is_beta_mode()
        launch_date = BetaService.get_launch_date()
        grace_days = BetaService.get_grace_period_days()
        
        if is_beta:
            # Beta user: Grace period starts at launch
            user.is_beta_user = True
            user.beta_joined_at = datetime.now(timezone.utc)
            if launch_date:
                user.grace_period_ends_at = launch_date + timedelta(days=grace_days)
        else:
            # Post-launch user: Grace period starts now
            user.is_beta_user = False
            user.grace_period_ends_at = datetime.now(timezone.utc) + timedelta(days=grace_days)
            
        db.add(user)
        db.flush()

    @staticmethod
    def mark_as_beta_user(user: User, db: Session):
        """Mark user as beta participant (force beta status)"""
        user.is_beta_user = True
        user.beta_joined_at = datetime.now(timezone.utc)
        
        launch_date = BetaService.get_launch_date()
        if launch_date:
            user.grace_period_ends_at = BetaService.calculate_grace_period_end(launch_date)
        
        db.flush()
    
    @staticmethod
    def should_send_reminder(user: User, notification_type: str, db: Session) -> bool:
        """
        Check if we should send a reminder (prevent spam)
        Returns True if:
        - No notification of this type sent in last 24 hours
        - User hasn't completed the action
        """
        from database.pg_models import NotificationHistory
        
        # Check if notification was sent recently
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = db.query(NotificationHistory).filter(
            NotificationHistory.user_id == user.id,
            NotificationHistory.notification_type == notification_type,
            NotificationHistory.sent_at >= cutoff
        ).first()
        
        if recent:
            return False
        
        # Check if action is still needed
        if notification_type == "beta_card_reminder":
            return not BetaService.has_saved_card(user)
        elif notification_type == "grace_period_warning":
            return not BetaService.has_saved_card(user) and BetaService.is_in_grace_period()
        
        return False