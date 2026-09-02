"""
MailerLite Email Service
Transactional email service using MailerLite API
"""

import requests
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
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
        self.support_email = os.getenv("SUPPORT_EMAIL", "support@lavoo.io")
        self.from_email = os.getenv("FROM_EMAIL", self.support_email)
        self.from_name = os.getenv("FROM_NAME", "Lavoo | The Business Doctor")
        self.frontend_url = os.getenv("FRONTEND_URL", "https://lavoo.io")
        self.base_url = "https://connect.mailerlite.com/api"

        # SMTP configuration (optional fallback or primary transport)
        self.smtp_host = os.getenv("SMTP_HOST")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587")) if os.getenv("SMTP_PORT") else 587
        self.smtp_user = os.getenv("SMTP_USER")
        self.smtp_password = os.getenv("SMTP_PASSWORD")
        self.smtp_tls = os.getenv("SMTP_TLS", "true").lower() == "true"
        self.smtp_ssl = os.getenv("SMTP_SSL", "false").lower() == "true"

        if not self.api_key and not self.smtp_host:
            logger.warning("⚠️ Neither MAILERLITE_API_KEY nor SMTP_HOST is set - emails will be logged only")

    def _send_email(
        self,
        to_email: str,
        to_name: str,
        subject: str,
        html_content: str,
        text_content: Optional[str] = None,
        reply_to: Optional[str] = None
    ):
        """
        Send email via MailerLite API or SMTP transport with graceful fallback.
        Ensures delivery does not raise unhandled exceptions.
        """
        # 1. Try MailerLite API if configured
        if self.api_key:
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

                if reply_to:
                    payload["reply_to"] = {
                        "email": reply_to,
                        "name": to_name
                    }

                response = requests.post(
                    f"{self.base_url}/emails",
                    headers=headers,
                    json=payload,
                    timeout=10
                )

                if response.status_code in [200, 201, 202]:
                    logger.info(f"✅ Email sent via MailerLite to {to_email}: {subject}")
                    return {
                        "success": True,
                        "message_id": response.json().get("data", {}).get("id", ""),
                        "status": "sent"
                    }
                else:
                    logger.warning(f"⚠️ MailerLite delivery returned {response.status_code}: {response.text[:200]}")
            except Exception as e:
                logger.warning(f"⚠️ MailerLite send failed: {str(e)}")

        # 2. Try SMTP if configured
        if self.smtp_host and self.smtp_user and self.smtp_password:
            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = formataddr((self.from_name, self.from_email))
                msg["To"] = formataddr((to_name, to_email))
                if reply_to:
                    msg["Reply-To"] = reply_to

                part_text = MIMEText(text_content or subject, "plain", "utf-8")
                part_html = MIMEText(html_content, "html", "utf-8")
                msg.attach(part_text)
                msg.attach(part_html)

                if self.smtp_ssl:
                    with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=10) as server:
                        server.login(self.smtp_user, self.smtp_password)
                        server.sendmail(self.from_email, [to_email], msg.as_string())
                else:
                    with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                        if self.smtp_tls:
                            server.starttls()
                        server.login(self.smtp_user, self.smtp_password)
                        server.sendmail(self.from_email, [to_email], msg.as_string())

                logger.info(f"✅ Email sent via SMTP to {to_email}: {subject}")
                return {
                    "success": True,
                    "message_id": f"smtp_{datetime.now(timezone.utc).timestamp()}",
                    "status": "sent"
                }
            except Exception as e:
                logger.warning(f"⚠️ SMTP send failed: {str(e)}")

        # 3. Fallback: Log email in non-configured / local environments
        logger.info(f"📧 [LOGGED] TO: {to_email} | REPLY-TO: {reply_to or 'N/A'} | SUBJECT: {subject}")
        return {
            "success": True,
            "message_id": f"logged_{datetime.now(timezone.utc).timestamp()}",
            "status": "logged"
        }

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

    def send_contact_inquiry_to_admin(
        self,
        name: str,
        email: str,
        company: Optional[str],
        reason: str,
        subject: Optional[str],
        message: str
    ):
        """Send contact form submission to Lavoo team inbox (support@lavoo.io)"""
        recipient_email = os.getenv("CONTACT_RECIPIENT_EMAIL", os.getenv("SUPPORT_EMAIL", "support@lavoo.io"))
        clean_reason = (reason or "general").replace("_", " ").title()
        email_subject = f"[Lavoo Contact] {clean_reason}: {name}" if not subject else f"[Lavoo Contact] {clean_reason}: {subject}"

        formatted_date = datetime.now(timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC")

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1f2937; margin: 0; padding: 0; background-color: #f9fafb; }}
                .container {{ max-width: 600px; margin: 20px auto; background: #ffffff; border-radius: 12px; border: 1px solid #e5e7eb; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05); }}
                .header {{ background: #18181b; color: #ffffff; padding: 24px 30px; border-bottom: 3px solid #e87a02; }}
                .header h1 {{ margin: 0; font-size: 20px; font-weight: 700; }}
                .badge {{ display: inline-block; background: #e87a02; color: #ffffff; font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 9999px; margin-top: 8px; }}
                .content {{ padding: 30px; }}
                .meta-table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; }}
                .meta-table td {{ padding: 8px 0; font-size: 14px; border-bottom: 1px solid #f3f4f6; }}
                .meta-label {{ color: #6b7280; font-weight: 600; width: 110px; }}
                .meta-value {{ color: #111827; font-weight: 500; }}
                .message-box {{ background: #fffaf0; border-left: 4px solid #e87a02; border-radius: 6px; padding: 20px; margin: 20px 0; font-size: 15px; color: #374151; white-space: pre-wrap; line-height: 1.6; }}
                .action-wrap {{ text-align: center; margin: 30px 0 10px; }}
                .reply-btn {{ display: inline-block; background: #e87a02; color: #ffffff !important; text-decoration: none; padding: 12px 28px; border-radius: 8px; font-weight: 600; font-size: 14px; }}
                .footer {{ background: #f9fafb; padding: 20px 30px; text-align: center; color: #9ca3af; font-size: 12px; border-top: 1px solid #f3f4f6; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>📩 New Contact Form Submission</h1>
                    <span class="badge">{clean_reason}</span>
                </div>
                <div class="content">
                    <table class="meta-table">
                        <tr>
                            <td class="meta-label">From:</td>
                            <td class="meta-value"><strong>{name}</strong> (&lt;a href="mailto:{email}" style="color: #e87a02;"&gt;{email}&lt;/a&gt;)</td>
                        </tr>
                        {f'<tr><td class="meta-label">Company:</td><td class="meta-value">{company}</td></tr>' if company else ''}
                        <tr>
                            <td class="meta-label">Category:</td>
                            <td class="meta-value">{clean_reason}</td>
                        </tr>
                        <tr>
                            <td class="meta-label">Submitted:</td>
                            <td class="meta-value">{formatted_date}</td>
                        </tr>
                    </table>

                    <div style="font-size: 13px; font-weight: 700; color: #4b5563; text-transform: uppercase; letter-spacing: 0.05em; margin-top: 20px;">
                        Message:
                    </div>
                    <div class="message-box">{message}</div>

                    <div class="action-wrap">
                        <a href="mailto:{email}?subject=Re:%20{clean_reason}%20Inquiry%20-%20Lavoo" class="reply-btn">Reply to {name}</a>
                    </div>
                </div>
                <div class="footer">
                    <p>This message was sent via the Lavoo contact form (https://lavoo.io/contact).</p>
                    <p>&copy; {datetime.now().year} Lavoo | The Business Doctor.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"""New Contact Form Submission on Lavoo
From: {name} ({email})
Company: {company or 'N/A'}
Category: {clean_reason}
Date: {formatted_date}

Message:
{message}
"""
        return self._send_email(
            to_email=recipient_email,
            to_name="Lavoo Team",
            subject=email_subject,
            html_content=html_content,
            text_content=text_content,
            reply_to=email
        )

    def send_contact_confirmation_to_user(self, user_email: str, name: str):
        """Send immediate confirmation receipt to the user who submitted the contact form"""
        subject = "We've received your message — Lavoo"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1f2937; margin: 0; padding: 0; background-color: #f9fafb; }}
                .container {{ max-width: 600px; margin: 20px auto; background: #ffffff; border-radius: 12px; border: 1px solid #e5e7eb; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05); }}
                .header {{ background: #18181b; color: #ffffff; padding: 30px; text-align: center; border-bottom: 3px solid #e87a02; }}
                .header h1 {{ margin: 0; font-size: 24px; font-weight: 700; }}
                .content {{ padding: 32px; }}
                .highlight-box {{ background: #fffaf0; border-left: 4px solid #e87a02; border-radius: 6px; padding: 18px 20px; margin: 24px 0; font-size: 15px; color: #374151; }}
                .btn {{ display: inline-block; background: #e87a02; color: #ffffff !important; text-decoration: none; padding: 12px 28px; border-radius: 8px; font-weight: 600; font-size: 14px; margin-top: 15px; }}
                .footer {{ background: #f9fafb; padding: 20px 30px; text-align: center; color: #9ca3af; font-size: 12px; border-top: 1px solid #f3f4f6; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Lavoo</h1>
                </div>
                <div class="content">
                    <h2 style="margin-top: 0; color: #111827; font-size: 20px;">Hi {name},</h2>
                    <p style="font-size: 16px; color: #4b5563;">
                        Thanks for reaching out! We've received your message and will respond within 24 hours.
                    </p>
                    
                    <div class="highlight-box">
                        <strong>What's next?</strong><br />
                        Our team is reviewing your inquiry and will follow up with you directly at this email address (<strong>{user_email}</strong>).
                    </div>

                    <p style="color: #6b7280; font-size: 14px;">
                        In the meantime, feel free to explore our platform or learn more about how Lavoo acts as your business doctor.
                    </p>

                    <div style="text-align: center; margin-top: 25px;">
                        <a href="{self.frontend_url}" class="btn">Explore Lavoo</a>
                    </div>
                </div>
                <div class="footer">
                    <p>&copy; {datetime.now().year} Lavoo | The Business Doctor. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_content = f"""Hi {name},

Thanks for reaching out! We've received your message and will respond within 24 hours.

Our team is reviewing your note and will follow up directly at this email address ({user_email}).

Best regards,
The Lavoo Team
https://lavoo.io
"""
        return self._send_email(
            to_email=user_email,
            to_name=name,
            subject=subject,
            html_content=html_content,
            text_content=text_content
        )


email_service = MailerLiteEmailService()

router = APIRouter(prefix="/api/email", tags=["email"])


@router.post("/test")
async def test_email(background_tasks: BackgroundTasks):
    """Test email endpoint"""
    return {
        "success": True,
        "message": "MailerLite email service is active" if email_service.api_key else "MailerLite API key not configured - emails will be logged only"
    }

