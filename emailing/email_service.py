"""
MailerLite Email Service
Transactional email service using MailerLite API
"""

import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, EmailStr
from typing import List, Optional, Dict
from datetime import datetime, timezone
import os
from dotenv import load_dotenv
import logging

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MailerLiteEmailService:
    def __init__(self):
        self.api_key = os.getenv("MAILERLITE_API_KEY")
        self.from_email = os.getenv("FROM_EMAIL", "clintonemeka05@gmail.com")
        self.from_name = os.getenv("FROM_NAME", "Lavoo Business Intelligence Engine")
        self.frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        self.support_email = os.getenv("SUPPORT_EMAIL", "support@lavoo.ai")
        self.base_url = "https://connect.mailerlite.com/api"

        if not self.api_key:
            logger.warning("⚠️  MAILERLITE_API_KEY not set - emails will be logged only")

    def _send_email(self, to_email: str, to_name: str, subject: str, html_content: str, text_content: Optional[str] = None):
        """Send email via MailerLite API"""
        if not self.api_key:
            logger.info(f"📧 [LOGGED] TO: {to_email} | SUBJECT: {subject}")
            return {
                "success": True,
                "message_id": f"logged_{datetime.now(timezone.utc).timestamp()}",
                "status": "logged"
            }

        try:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            }

            payload = {
                "from": {
                    "email": self.from_email,
                    "name": self.from_name
                },
                "to": [
                    {
                        "email": to_email,
                        "name": to_name
                    }
                ],
                "subject": subject,
                "html": html_content,
                "text": text_content or subject
            }

            response = requests.post(
                f"{self.base_url}/emails",
                headers=headers,
                json=payload,
                timeout=10
            )

            if response.status_code in [200, 201, 202]:
                logger.info(f"✅ Email sent to {to_email}: {subject}")
                return {
                    "success": True,
                    "message_id": response.json().get("data", {}).get("id", ""),
                    "status": "sent"
                }
            else:
                # Non-fatal: email delivery failures must never break payment or subscription flows.
                # 404 = sender/resource not configured in MailerLite account.
                # Fix: verify the FROM_EMAIL address in your MailerLite account and enable
                # transactional emails under Settings → Transactional emails.
                logger.warning(f"⚠️ MailerLite delivery skipped ({response.status_code}): {response.text[:200]}")
                return {"success": False, "status": "skipped", "reason": response.text[:200]}

        except Exception as e:
            logger.warning(f"⚠️ Email send failed (non-fatal): {str(e)}")
            return {"success": False, "status": "error", "reason": str(e)}

    def send_welcome_email(self, user_email: str, name: str):
        """Send welcome email on signup"""
        subject = "Welcome to Lavoo Business Intelligence Engine!"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{
                    background: linear-gradient(135deg, #f97316 0%, #ea580c 100%);
                    color: white; padding: 30px; text-align: center;
                    border-radius: 10px 10px 0 0;
                }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .button {{
                    display: inline-block; padding: 12px 30px; background: #f97316;
                    color: white; text-decoration: none; border-radius: 5px; margin: 20px 0;
                }}
                .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
                ul {{ padding-left: 20px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">Welcome to Lavoo!</h1>
                </div>
                <div class="content">
                    <h2>Hi {name},</h2>
                    <p>We're thrilled to have you on board! Your account has been successfully created.</p>
                    <p><strong>With Lavoo Business Intelligence Engine, you can:</strong></p>
                    <ul>
                        <li>Run powerful AI-driven business analyses</li>
                        <li>Detect bottlenecks and get actionable solutions</li>
                        <li>Generate comprehensive ROI projections</li>
                        <li>Earn commissions through referrals</li>
                    </ul>
                    <p style="text-align: center;">
                        <a href="{self.frontend_url}/dashboard" class="button">Get Started</a>
                    </p>
                    <p>If you have any questions, feel free to reach out to our support team at {self.support_email}.</p>
                </div>
                <div class="footer">
                    <p>&copy; {datetime.now().year} Lavoo Business Intelligence Engine. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"Welcome to Lavoo Business Intelligence Engine!\n\nHi {name},\n\nWe're thrilled to have you on board!"

        return self._send_email(user_email, name, subject, html_content, text_content)

    def send_password_reset_email(self, user_email: str, name: str, reset_token: str):
        """Send password reset email with token link"""
        subject = "Reset Your Lavoo Password"
        reset_link = f"{self.frontend_url}/reset-password/{reset_token}"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{
                    background: linear-gradient(135deg, #f97316 0%, #ea580c 100%);
                    color: white; padding: 30px; text-align: center;
                    border-radius: 10px 10px 0 0;
                }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .button {{
                    display: inline-block; padding: 12px 30px; background: #f97316;
                    color: white; text-decoration: none; border-radius: 5px; margin: 20px 0;
                }}
                .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
                .warning {{ background: #fef3c7; border-left: 4px solid #f59e0b; padding: 15px; margin: 20px 0; border-radius: 4px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">Password Reset Request</h1>
                </div>
                <div class="content">
                    <h2>Hi {name},</h2>
                    <p>We received a request to reset your Lavoo account password.</p>
                    <p>Click the button below to create a new password:</p>
                    <p style="text-align: center;">
                        <a href="{reset_link}" class="button">Reset Password</a>
                    </p>
                    <div class="warning">
                        <strong>⚠️ Security Notice:</strong><br>
                        • This link expires in 30 minutes<br>
                        • If you didn't request this reset, please ignore this email<br>
                        • Your password won't change until you create a new one
                    </div>
                    <p>If the button doesn't work, copy and paste this link into your browser:</p>
                    <p style="word-break: break-all; color: #666; font-size: 12px;">{reset_link}</p>
                    <p>If you need assistance, contact us at {self.support_email}.</p>
                </div>
                <div class="footer">
                    <p>&copy; {datetime.now().year} Lavoo Business Intelligence Engine. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"Password Reset Request\n\nHi {name},\n\nClick this link to reset your password:\n{reset_link}\n\nThis link expires in 30 minutes."

        return self._send_email(user_email, name, subject, html_content, text_content)


    def send_subscription_confirmation(self, user_email: str, name: str, plan_type: str,
                                      amount: float, currency: str, next_billing_date: str):
        """Send subscription confirmation"""
        subject = f"Subscription Confirmed - {plan_type.title()} Plan"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{
                    background: linear-gradient(135deg, #10b981 0%, #059669 100%);
                    color: white; padding: 30px; text-align: center;
                    border-radius: 10px 10px 0 0;
                }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .info-box {{ background: white; border-left: 4px solid #10b981; padding: 20px; margin: 20px 0; }}
                .button {{
                    display: inline-block; padding: 12px 30px; background: #10b981;
                    color: white; text-decoration: none; border-radius: 5px; margin: 20px 0;
                }}
                .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">✓ Subscription Confirmed!</h1>
                </div>
                <div class="content">
                    <h2>Hi {name},</h2>
                    <p>Your subscription has been successfully activated!</p>

                    <div class="info-box">
                        <h3 style="margin-top: 0;">Subscription Details</h3>
                        <p><strong>Plan:</strong> {plan_type.title()}</p>
                        <p><strong>Amount:</strong> {currency}{amount:.2f}</p>
                        <p><strong>Next Billing Date:</strong> {next_billing_date}</p>
                    </div>

                    <p>You now have full access to all premium features!</p>

                    <p style="text-align: center;">
                        <a href="{self.frontend_url}/dashboard" class="button">Go to Dashboard</a>
                    </p>
                </div>
                <div class="footer">
                    <p>&copy; {datetime.now().year} Lavoo Business Intelligence Engine. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"Subscription Confirmed!\n\nPlan: {plan_type.title()}\nAmount: {currency}{amount:.2f}\nNext Billing: {next_billing_date}"

        return self._send_email(user_email, name, subject, html_content, text_content)

    def send_payment_success_email(self, user_email: str, name: str, amount: float, plan_type: str, next_billing_date: str):
        """Wrapper for subscription confirmation used in stripe.py"""
        return self.send_subscription_confirmation(user_email, name, plan_type, amount, "$", next_billing_date)

    def send_beta_card_saved_email(self, user_email: str, name: str, card_last4: str, card_brand: str, grace_period_days: int):
        """Send confirmation when card is saved in beta mode"""
        subject = "Card Saved - Welcome to Lavoo Beta!"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{
                    background: linear-gradient(135deg, #f97316 0%, #ea580c 100%);
                    color: white; padding: 30px; text-align: center;
                    border-radius: 10px 10px 0 0;
                }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .info-box {{ background: white; border-left: 4px solid #f97316; padding: 20px; margin: 20px 0; }}
                .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">💳 Card Saved Successfully!</h1>
                </div>
                <div class="content">
                    <h2>Hi {name},</h2>
                    <p>Your card has been securely saved for the Lavoo Beta.</p>

                    <div class="info-box">
                        <p><strong>Card:</strong> {card_brand.upper()} ending in {card_last4}</p>
                        <p><strong>Grace Period:</strong> {grace_period_days} days after launch</p>
                    </div>

                    <p>You won't be charged today. We'll notify you before your first official billing begins at launch.</p>
                    
                    <p>Thank you for being an early supporter!</p>
                </div>
                <div class="footer">
                    <p>&copy; {datetime.now().year} Lavoo Business Intelligence Engine. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"Card Saved!\n\nHi {name},\n\nYour {card_brand} card ending in {card_last4} has been saved. You have a {grace_period_days} day grace period after launch before billing begins."

        return self._send_email(user_email, name, subject, html_content, text_content)

    def send_payment_receipt(self, user_email: str, name: str, amount: float,
                            currency: str, payment_date: str, transaction_id: str,
                            plan_type: str):
        """Send payment receipt"""
        subject = "Payment Receipt - Lavoo Subscription"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{
                    background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
                    color: white; padding: 30px; text-align: center;
                    border-radius: 10px 10px 0 0;
                }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .receipt-box {{ background: white; border: 1px solid #e5e7eb; padding: 20px; margin: 20px 0; }}
                .receipt-row {{ display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #e5e7eb; }}
                .total {{ font-size: 18px; font-weight: bold; color: #3b82f6; }}
                .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">Payment Receipt</h1>
                </div>
                <div class="content">
                    <h2>Hi {name},</h2>
                    <p>Thank you for your payment!</p>

                    <div class="receipt-box">
                        <h3 style="margin-top: 0;">Receipt Details</h3>
                        <div class="receipt-row">
                            <span>Plan</span>
                            <span>{plan_type.title()}</span>
                        </div>
                        <div class="receipt-row">
                            <span>Date</span>
                            <span>{payment_date}</span>
                        </div>
                        <div class="receipt-row">
                            <span>Transaction ID</span>
                            <span>{transaction_id}</span>
                        </div>
                        <div class="receipt-row total">
                            <span>Total Paid</span>
                            <span>{currency}{amount:.2f}</span>
                        </div>
                    </div>

                    <p>This payment will appear on your statement as "Lavoo Business Intelligence".</p>
                    <p>If you have any questions, contact us at {self.support_email}.</p>
                </div>
                <div class="footer">
                    <p>&copy; {datetime.now().year} Lavoo Business Intelligence Engine. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"Payment Receipt\n\nAmount: {currency}{amount:.2f}\nDate: {payment_date}\nTransaction ID: {transaction_id}"

        return self._send_email(user_email, name, subject, html_content, text_content)

    def send_subscription_renewal(self, user_email: str, name: str, plan_type: str,
                                 amount: float, currency: str, renewal_date: str):
        """Send subscription renewal notice"""
        subject = "Your Subscription Has Been Renewed"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{
                    background: linear-gradient(135deg, #10b981 0%, #059669 100%);
                    color: white; padding: 30px; text-align: center;
                    border-radius: 10px 10px 0 0;
                }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .info-box {{ background: white; border-left: 4px solid #10b981; padding: 20px; margin: 20px 0; }}
                .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">Subscription Renewed</h1>
                </div>
                <div class="content">
                    <h2>Hi {name},</h2>
                    <p>Your subscription has been successfully renewed!</p>

                    <div class="info-box">
                        <p><strong>Plan:</strong> {plan_type.title()}</p>
                        <p><strong>Amount Charged:</strong> {currency}{amount:.2f}</p>
                        <p><strong>Next Renewal:</strong> {renewal_date}</p>
                    </div>

                    <p>You continue to have full access to all premium features.</p>
                    <p>Thank you for being a valued member!</p>
                </div>
                <div class="footer">
                    <p>&copy; {datetime.now().year} Lavoo Business Intelligence Engine. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"Subscription Renewed\n\nPlan: {plan_type.title()}\nAmount: {currency}{amount:.2f}\nNext Renewal: {renewal_date}"

        return self._send_email(user_email, name, subject, html_content, text_content)

    def send_payment_failed(self, user_email: str, name: str, plan_type: str,
                           amount: float, currency: str, retry_date: str, reason: str):
        """Send payment failure notification"""
        subject = "Payment Failed - Action Required"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{
                    background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
                    color: white; padding: 30px; text-align: center;
                    border-radius: 10px 10px 0 0;
                }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .warning-box {{ background: #fef2f2; border-left: 4px solid #ef4444; padding: 20px; margin: 20px 0; }}
                .button {{
                    display: inline-block; padding: 12px 30px; background: #ef4444;
                    color: white; text-decoration: none; border-radius: 5px; margin: 20px 0;
                }}
                .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">⚠️ Payment Failed</h1>
                </div>
                <div class="content">
                    <h2>Hi {name},</h2>
                    <p>We were unable to process your payment for your {plan_type.title()} subscription.</p>

                    <div class="warning-box">
                        <p><strong>Amount:</strong> {currency}{amount:.2f}</p>
                        <p><strong>Reason:</strong> {reason}</p>
                        <p><strong>Retry Date:</strong> {retry_date}</p>
                    </div>

                    <p>Please update your payment method to continue your subscription.</p>

                    <p style="text-align: center;">
                        <a href="{self.frontend_url}/dashboard/upgrade" class="button">Update Payment Method</a>
                    </p>

                    <p>If you need help, contact us at {self.support_email}.</p>
                </div>
                <div class="footer">
                    <p>&copy; {datetime.now().year} Lavoo Business Intelligence Engine. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"Payment Failed\n\nAmount: {currency}{amount:.2f}\nReason: {reason}\nPlease update your payment method."

        return self._send_email(user_email, name, subject, html_content, text_content)

    def send_subscription_cancelled(self, user_email: str, name: str, plan_type: str,
                                   end_date: str, cancellation_reason: Optional[str] = None):
        """Send subscription cancellation confirmation"""
        subject = "Subscription Cancelled"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{
                    background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%);
                    color: white; padding: 30px; text-align: center;
                    border-radius: 10px 10px 0 0;
                }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .info-box {{ background: white; border-left: 4px solid #6366f1; padding: 20px; margin: 20px 0; }}
                .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">Subscription Cancelled</h1>
                </div>
                <div class="content">
                    <h2>Hi {name},</h2>
                    <p>Your {plan_type.title()} subscription has been cancelled.</p>

                    <div class="info-box">
                        <p><strong>Access Until:</strong> {end_date}</p>
                        {f'<p><strong>Reason:</strong> {cancellation_reason}</p>' if cancellation_reason else ''}
                    </div>

                    <p>You'll continue to have access to premium features until {end_date}.</p>
                    <p>We're sorry to see you go! If you change your mind, you can reactivate anytime.</p>
                    <p>Contact us at {self.support_email} if you have questions.</p>
                </div>
                <div class="footer">
                    <p>&copy; {datetime.now().year} Lavoo Business Intelligence Engine. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"Subscription Cancelled\n\nYour subscription has been cancelled. Access until: {end_date}"

        return self._send_email(user_email, name, subject, html_content, text_content)

    def send_commission_notification(self, user_email: str, name: str,
                                    amount: float, currency: str, referred_user_name: str,
                                    commission_date: str):
        """Send commission earned notification"""
        subject = f"You've Earned {currency}{amount:.2f} Commission!"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{
                    background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
                    color: white; padding: 30px; text-align: center;
                    border-radius: 10px 10px 0 0;
                }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .commission-box {{
                    background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%);
                    border-left: 4px solid #f59e0b;
                    padding: 20px;
                    margin: 20px 0;
                    text-align: center;
                }}
                .amount {{ font-size: 32px; font-weight: bold; color: #d97706; }}
                .button {{
                    display: inline-block; padding: 12px 30px; background: #f59e0b;
                    color: white; text-decoration: none; border-radius: 5px; margin: 20px 0;
                }}
                .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">🎉 Commission Earned!</h1>
                </div>
                <div class="content">
                    <h2>Congratulations {name}!</h2>
                    <p>You've earned a commission from your referral!</p>

                    <div class="commission-box">
                        <div class="amount">{currency}{amount:.2f}</div>
                        <p>Earned from {referred_user_name}'s subscription</p>
                        <p><small>Date: {commission_date}</small></p>
                    </div>

                    <p>Keep referring more users to earn even more!</p>

                    <p style="text-align: center;">
                        <a href="{self.frontend_url}/dashboard/earnings" class="button">View Earnings</a>
                    </p>
                </div>
                <div class="footer">
                    <p>&copy; {datetime.now().year} Lavoo Business Intelligence Engine. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"Commission Earned!\n\nAmount: {currency}{amount:.2f}\nFrom: {referred_user_name}\nDate: {commission_date}"

        return self._send_email(user_email, name, subject, html_content, text_content)

    def send_payout_processed(self, user_email: str, name: str, amount: float,
                             currency: str, payment_method: str, transaction_id: str,
                             processing_date: str):
        """Send payout processed notification"""
        subject = "Payout Processed Successfully"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{
                    background: linear-gradient(135deg, #10b981 0%, #059669 100%);
                    color: white; padding: 30px; text-align: center;
                    border-radius: 10px 10px 0 0;
                }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .payout-box {{ background: white; border-left: 4px solid #10b981; padding: 20px; margin: 20px 0; }}
                .amount {{ font-size: 24px; font-weight: bold; color: #10b981; }}
                .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">✓ Payout Processed</h1>
                </div>
                <div class="content">
                    <h2>Hi {name},</h2>
                    <p>Your payout has been processed successfully!</p>

                    <div class="payout-box">
                        <div class="amount">{currency}{amount:.2f}</div>
                        <p><strong>Payment Method:</strong> {payment_method}</p>
                        <p><strong>Transaction ID:</strong> {transaction_id}</p>
                        <p><strong>Date:</strong> {processing_date}</p>
                    </div>

                    <p>The funds should arrive in your account within 2-5 business days.</p>
                    <p>Thank you for being part of Lavoo!</p>
                </div>
                <div class="footer">
                    <p>&copy; {datetime.now().year} Lavoo Business Intelligence Engine. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"Payout Processed\n\nAmount: {currency}{amount:.2f}\nMethod: {payment_method}\nTransaction ID: {transaction_id}"

        return self._send_email(user_email, name, subject, html_content, text_content)

    def send_payout_failed(self, user_email: str, name: str, amount: float,
                          currency: str, failure_reason: str):
        """Send payout failure notification"""
        subject = "Payout Failed - Action Required"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{
                    background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
                    color: white; padding: 30px; text-align: center;
                    border-radius: 10px 10px 0 0;
                }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .warning-box {{ background: #fef2f2; border-left: 4px solid #ef4444; padding: 20px; margin: 20px 0; }}
                .button {{
                    display: inline-block; padding: 12px 30px; background: #ef4444;
                    color: white; text-decoration: none; border-radius: 5px; margin: 20px 0;
                }}
                .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">⚠️ Payout Failed</h1>
                </div>
                <div class="content">
                    <h2>Hi {name},</h2>
                    <p>We encountered an issue processing your payout.</p>

                    <div class="warning-box">
                        <p><strong>Amount:</strong> {currency}{amount:.2f}</p>
                        <p><strong>Reason:</strong> {failure_reason}</p>
                    </div>

                    <p>Please verify your payout account details and try again.</p>

                    <p style="text-align: center;">
                        <a href="{self.frontend_url}/dashboard/earnings" class="button">Update Payout Details</a>
                    </p>

                    <p>Contact us at {self.support_email} if you need assistance.</p>
                </div>
                <div class="footer">
                    <p>&copy; {datetime.now().year} Lavoo Business Intelligence Engine. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"Payout Failed\n\nAmount: {currency}{amount:.2f}\nReason: {failure_reason}"

        return self._send_email(user_email, name, subject, html_content, text_content)

    def send_beta_reminder(self, user_email: str, name: str, days_until_billing: int):
        """Send beta user reminder to save payment method"""
        subject = f"Save Your Payment Method - {days_until_billing} Days Remaining"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{
                    background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
                    color: white; padding: 30px; text-align: center;
                    border-radius: 10px 10px 0 0;
                }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .reminder-box {{ background: #fffbeb; border-left: 4px solid #f59e0b; padding: 20px; margin: 20px 0; }}
                .button {{
                    display: inline-block; padding: 12px 30px; background: #f59e0b;
                    color: white; text-decoration: none; border-radius: 5px; margin: 20px 0;
                }}
                .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">⏰ Reminder: Save Your Card</h1>
                </div>
                <div class="content">
                    <h2>Hi {name},</h2>
                    <p>Your beta access has been amazing! To ensure uninterrupted service, please save your payment method.</p>

                    <div class="reminder-box">
                        <p><strong>Time Remaining:</strong> {days_until_billing} days</p>
                        <p>Save your card now to avoid service interruption.</p>
                    </div>

                    <p>Billing will begin automatically once saved.</p>

                    <p style="text-align: center;">
                        <a href="{self.frontend_url}/dashboard/upgrade" class="button">Save Payment Method</a>
                    </p>
                </div>
                <div class="footer">
                    <p>&copy; {datetime.now().year} Lavoo Business Intelligence Engine. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"Reminder: Save your payment method. Time remaining: {days_until_billing} days."

        return self._send_email(user_email, name, subject, html_content, text_content)

    def send_grace_period_warning(self, user_email: str, name: str, days_remaining: int):
        """Send grace period warning"""
        subject = f"Urgent: Save Your Payment Method - {days_remaining} Days Left"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{
                    background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
                    color: white; padding: 30px; text-align: center;
                    border-radius: 10px 10px 0 0;
                }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .urgent-box {{ background: #fef2f2; border-left: 4px solid #ef4444; padding: 20px; margin: 20px 0; }}
                .button {{
                    display: inline-block; padding: 12px 30px; background: #ef4444;
                    color: white; text-decoration: none; border-radius: 5px; margin: 20px 0;
                }}
                .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">⚠️ Urgent: Action Required</h1>
                </div>
                <div class="content">
                    <h2>Hi {name},</h2>
                    <p>Your grace period is ending soon!</p>

                    <div class="urgent-box">
                        <p><strong>Days Remaining:</strong> {days_remaining}</p>
                        <p>Save your payment method NOW to avoid losing access.</p>
                    </div>

                    <p>Without a payment method on file, your account will be suspended in {days_remaining} days.</p>

                    <p style="text-align: center;">
                        <a href="{self.frontend_url}/dashboard/upgrade" class="button">Save Payment Method Now</a>
                    </p>

                    <p>Questions? Contact {self.support_email}</p>
                </div>
                <div class="footer">
                    <p>&copy; {datetime.now().year} Lavoo Business Intelligence Engine. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"Urgent: Save your payment method. Only {days_remaining} days left before account suspension."

        return self._send_email(user_email, name, subject, html_content, text_content)

    def send_referral_notification(self, user_email: str, name: str, referred_user_name: str):
        """Send referral notification"""
        subject = f"{referred_user_name} Joined Using Your Referral!"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{
                    background: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%);
                    color: white; padding: 30px; text-align: center;
                    border-radius: 10px 10px 0 0;
                }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .success-box {{ background: #f5f3ff; border-left: 4px solid #8b5cf6; padding: 20px; margin: 20px 0; }}
                .button {{
                    display: inline-block; padding: 12px 30px; background: #8b5cf6;
                    color: white; text-decoration: none; border-radius: 5px; margin: 20px 0;
                }}
                .footer {{ text-align: center; padding: 20px; color: #666; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">🎉 New Referral!</h1>
                </div>
                <div class="content">
                    <h2>Great news {name}!</h2>
                    <p>{referred_user_name} just signed up using your referral link!</p>

                    <div class="success-box">
                        <p>You'll earn 50% commission when they subscribe.</p>
                        <p>Keep sharing your link to earn more!</p>
                    </div>

                    <p style="text-align: center;">
                        <a href="{self.frontend_url}/dashboard/referrals" class="button">View Referrals</a>
                    </p>
                </div>
                <div class="footer">
                    <p>&copy; {datetime.now().year} Lavoo Business Intelligence Engine. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"New Referral!\n\n{referred_user_name} signed up using your referral link!"

        return self._send_email(user_email, name, subject, html_content, text_content)


email_service = MailerLiteEmailService()

router = APIRouter(prefix="/api/email", tags=["email"])


@router.post("/test")
async def test_email(background_tasks: BackgroundTasks):
    """Test email endpoint"""
    return {
        "success": True,
        "message": "MailerLite email service is active" if email_service.api_key else "MailerLite API key not configured - emails will be logged only"
    }

