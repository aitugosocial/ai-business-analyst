
"""
Handle automatic billing of beta users whose grace period has ended.
Run daily via cron.
"""

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
import stripe
from sqlalchemy.orm import Session

# Add parent directory to Python path to resolve imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.pg_connections import get_db
from database.pg_models import User, Subscriptions
from subscriptions.beta_service import BetaService
from subscriptions.stripe_service import StripeService

# Set Stripe API key
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

def process_beta_billing():
    """
    Find users whose grace period has ended and charge their saved cards.
    """
    db = next(get_db())
    
    try:
        # Determine if we are in beta mode
        is_beta = BetaService.is_beta_mode()
        
        # 1. Base filter: Beta users with card, not active
        query = db.query(User).filter(
            User.is_beta_user == True,
            User.subscription_status != "active",
            User.stripe_payment_method_id.isnot(None)
        )
        
        # 2. Apply timing filter
        if is_beta:
            # If still in beta, ONLY charge if grace period specifically ended (unlikely in beta but safe)
            query = query.filter(User.grace_period_ends_at <= datetime.now(timezone.utc))
        else:
            # IF LAUNCHED: Charge everyone with a card immediately
            # The grace period is only for users WITHOUT cards to add one.
            pass
            
        users_to_charge = query.all()
        
        print(f"[{'BETA' if is_beta else 'LAUNCH'}] Found {len(users_to_charge)} users to charge")
        
        for user in users_to_charge:
            try:
                print(f"Charging user {user.id} ({user.email})...")
                
                # Default to monthly plan if not specified
                plan_type = user.subscription_plan or "monthly"
                price_id = os.getenv(f"STRIPE_{plan_type.upper()}_PRICE_ID")
                
                if not price_id:
                    print(f"❌ Missing Price ID for {plan_type}")
                    continue
                
                # Create subscription in Stripe
                # This will automatically charge the default payment method
                # We use 'create_subscription_with_saved_card' from StripeService
                result = StripeService.create_subscription_with_saved_card(
                    customer_id=user.stripe_customer_id,
                    price_id=price_id,
                    payment_method_id=user.stripe_payment_method_id,
                    metadata={
                        "user_id": str(user.id),
                        "plan_type": plan_type,
                        "beta_billing": "true"
                    }
                )
                
                if result.get("status") in ["active", "trialing", "incomplete"]:
                    # Create subscription record in our DB
                    # Calculate dates
                    from subscriptions.stripe import calculate_subscription_dates
                    start_date, end_date = calculate_subscription_dates(plan_type)
                    
                    # Store subscription ID in User model
                    user.stripe_subscription_id = result["subscription_id"]
                    user.subscription_status = "active"
                    user.subscription_plan = plan_type
                    user.subscription_expires_at = end_date
                    
                    # Create Subscriptions entry
                    subscription = Subscriptions(
                        user_id=user.id,
                        subscription_plan=plan_type,
                        transaction_id=result["subscription_id"],
                        tx_ref=f"BETA-{user.id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}",
                        amount=Decimal("29.99"), # Hardcoded for now, should ideally get from price_id
                        currency="USD",
                        status="completed",
                        subscription_status="active",
                        payment_provider="stripe",
                        start_date=start_date,
                        end_date=end_date
                    )
                    db.add(subscription)
                    db.flush()
                    
                    # Calculate commission
                    from subscriptions.commission_service import CommissionService
                    CommissionService.calculate_commission(subscription=subscription, db=db)
                    
                    # Send success email
                    from emailing.email_service import email_service
                    email_service.send_payment_success_email(
                        user.email,
                        user.name,
                        float(29.99),
                        plan_type,
                        end_date.strftime("%B %d, %Y")
                    )
                    
                    print(f"✅ [CRON] Successfully charged and activated user {user.id} ({user.email}). Plan: {plan_type}")
                    logger.info(f"✅ [CRON] Automatic billing success for {user.email}. Transaction: {result['subscription_id']}")
                else:
                    print(f"⚠️ [CRON] Subscription status: {result.get('status')} for user {user.id}")
                    logger.warning(f"⚠️ [CRON] Non-active status for {user.email}: {result.get('status')}")
                    
            except Exception as e:
                print(f"❌ [CRON] Failed to charge user {user.id}: {str(e)}")
                logger.error(f"❌ [CRON] Charge failure for {user.email}: {str(e)}")
        
        db.commit()
        
    except Exception as e:
        print(f"❌ Error in beta billing process: {str(e)}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    process_beta_billing()
