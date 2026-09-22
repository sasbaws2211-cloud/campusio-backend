"""Email Service using Resend API"""
import httpx
import logging
from typing import List, Dict, Optional
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

class EmailService:
    """Resend Email Service for sending transactional emails"""
    
    BASE_URL = "https://api.resend.com"
    
    def __init__(self):
        self.api_key = settings.resend_api_key
        self.from_email = settings.resend_from_email
        self.from_name = settings.resend_from_name
        
    def _get_headers(self) -> Dict:
        """Get request headers with API key"""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
    
    async def send_email(
        self,
        to: List[str],
        subject: str,
        html_body: str,
        text_body: Optional[str] = None,
        reply_to: Optional[str] = None,
        attachments: Optional[List[Dict]] = None
    ) -> Dict:
        """
        Send a transactional email

        Args:
            to: List of recipient email addresses
            subject: Email subject line
            html_body: HTML content of the email
            text_body: Plain text alternative (optional)
            reply_to: Reply-to email address (optional)
            attachments: Optional list of {"filename": str, "content": str} — content
                is base64-encoded file bytes, passed through to Resend as-is.

        Returns:
            Response from Resend API
        """
        if not self.api_key:
            logger.warning("Resend API key not configured - email not sent")
            return {"success": False, "error": "Email service not configured"}

        try:
            payload = {
                "from": f"{self.from_name} <{self.from_email}>",
                "to": to,
                "subject": subject,
                "html": html_body,
            }

            if text_body:
                payload["text"] = text_body

            if reply_to:
                payload["reply_to"] = reply_to

            if attachments:
                payload["attachments"] = attachments
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.BASE_URL}/emails",
                    json=payload,
                    headers=self._get_headers(),
                    timeout=30.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"Email sent successfully to {len(to)} recipient(s)")
                    return {"success": True, "message_id": data.get("id")}
                else:
                    error_msg = response.text
                    logger.error(f"Failed to send email: {error_msg}")
                    return {"success": False, "error": error_msg}
                    
        except Exception as e:
            logger.error(f"Error sending email: {str(e)}")
            return {"success": False, "error": str(e)}

    
    async def send_announcement_email(
        self,
        to: List[str],
        title: str,
        content: str,
        announcement_type: str = "general",
        school_name: str = "School ERP"
    ) -> Dict:
        """Send an announcement email with formatted template"""
        
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'IBM Plex Sans', Arial, sans-serif; color: #1A1A1A; line-height: 1.6; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; background-color: #FAFAED; }}
                .header {{ background-color: #064E3B; color: white; padding: 24px; text-align: center; border-radius: 12px 12px 0 0; }}
                .header h1 {{ margin: 0; font-size: 24px; font-family: 'Manrope', sans-serif; }}
                .content {{ background-color: white; padding: 24px; border: 1px solid #E4E4E7; border-top: none; border-radius: 0 0 12px 12px; }}
                .badge {{ display: inline-block; padding: 4px 12px; border-radius: 9999px; font-size: 12px; font-weight: 500; background-color: #064E3B; color: white; text-transform: capitalize; margin-bottom: 16px; }}
                .title {{ font-size: 20px; font-weight: 600; color: #1A1A1A; margin-bottom: 12px; }}
                .message {{ color: #52525B; margin-bottom: 24px; }}
                .footer {{ text-align: center; font-size: 12px; color: #A1A1AA; margin-top: 24px; padding-top: 16px; border-top: 1px solid #E4E4E7; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>{school_name}</h1>
                </div>
                <div class="content">
                    <span class="badge">{announcement_type}</span>
                    <h2 class="title">{title}</h2>
                    <p class="message">{content}</p>
                </div>
                <div class="footer">
                    <p>This is an automated message from {school_name}.</p>
                    <p>Please do not reply directly to this email.</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        text_body = f"""
{school_name} - {announcement_type.upper()} Announcement

{title}

{content}

---
This is an automated message. Please do not reply directly to this email.
        """
        
        return await self.send_email(
            to=to,
            subject=f"[{announcement_type.capitalize()}] {title}",
            html_body=html_body,
            text_body=text_body
        )
    
    async def send_qr_email(
        self,
        to: str,
        student_name: str,
        qr_token: str,
        expires_at: str,
        school_name: str = "School ERP",
    ) -> Dict:
        """Email today's pickup QR code to a parent"""

        # Use a public QR render URL so the image shows inline in email clients
        qr_image_url = f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={qr_token}"

        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'IBM Plex Sans', Arial, sans-serif; color: #1A1A1A; margin: 0; padding: 0; background: #F5F7F4; }}
                .container {{ max-width: 520px; margin: 40px auto; background: #fff; border-radius: 16px; border: 1px solid #D8DDD6; overflow: hidden; }}
                .header {{ background: #3D5C3A; color: white; padding: 28px 36px; }}
                .header h1 {{ margin: 0 0 4px 0; font-size: 20px; }}
                .header p {{ margin: 0; font-size: 13px; opacity: 0.8; }}
                .body {{ padding: 28px 36px; text-align: center; }}
                .body p {{ margin: 0 0 12px 0; color: #374151; font-size: 14px; text-align: left; }}
                .qr-wrap {{ display: inline-block; padding: 16px; border: 2px solid #D8DDD6; border-radius: 12px; margin: 16px auto; }}
                .qr-wrap img {{ display: block; }}
                .expiry {{ font-size: 12px; color: #6b7280; margin-top: 8px; }}
                .token-code {{ font-family: monospace; font-size: 11px; color: #9ca3af; word-break: break-all; margin-top: 8px; }}
                .note {{ background: #FEF3C7; border-left: 3px solid #F59E0B; padding: 10px 14px; border-radius: 0 6px 6px 0; font-size: 12px; color: #92400E; margin: 16px 0; text-align: left; }}
                .footer {{ padding: 16px 36px; background: #F9FAF8; border-top: 1px solid #E5E7EB; font-size: 11px; color: #9CA3AF; text-align: center; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>{school_name}</h1>
                    <p>Today's Pickup QR Code</p>
                </div>
                <div class="body">
                    <p>Show the QR code below to the security officer when picking up <strong>{student_name}</strong>.</p>
                    <div class="qr-wrap">
                        <img src="{qr_image_url}" width="200" height="200" alt="Pickup QR Code" />
                    </div>
                    <div class="expiry">Expires at {expires_at} today</div>
                    <div class="token-code">{qr_token}</div>
                    <div class="note">
                        This QR code is valid for today only. A new code is generated each school day. Open your parent portal to get tomorrow's code.
                    </div>
                </div>
                <div class="footer">
                    <p>Automated message from {school_name}. Do not reply.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_body = f"""
{school_name} — Today's Pickup QR for {student_name}

Show this code to the security officer at pickup. Expires at {expires_at}.

Token: {qr_token}

This code is valid today only.
        """

        return await self.send_email(
            to=[to],
            subject=f"Today's Pickup QR – {student_name} ({school_name})",
            html_body=html_body,
            text_body=text_body,
        )

    async def send_multi_qr_email(
        self,
        to: str,
        children: List[Dict],
        school_name: str = "School ERP",
    ) -> Dict:
        """Email today's pickup QR codes for ALL of a parent's children in one message.

        `children` is a list of dicts: {"name": str, "qr_token": str, "expires_at": str}
        """
        cards_html = ""
        cards_text = ""
        for child in children:
            qr_image_url = f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={child['qr_token']}"
            cards_html += f"""
                <div class="child-card">
                    <p class="child-name">{child['name']}</p>
                    <div class="qr-wrap">
                        <img src="{qr_image_url}" width="180" height="180" alt="Pickup QR Code" />
                    </div>
                    <div class="expiry">Expires at {child['expires_at']} today</div>
                    <div class="token-code">{child['qr_token']}</div>
                </div>
            """
            cards_text += f"\n{child['name']}\nExpires at {child['expires_at']}\nToken: {child['qr_token']}\n"

        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'IBM Plex Sans', Arial, sans-serif; color: #1A1A1A; margin: 0; padding: 0; background: #F5F7F4; }}
                .container {{ max-width: 520px; margin: 40px auto; background: #fff; border-radius: 16px; border: 1px solid #D8DDD6; overflow: hidden; }}
                .header {{ background: #3D5C3A; color: white; padding: 28px 36px; }}
                .header h1 {{ margin: 0 0 4px 0; font-size: 20px; }}
                .header p {{ margin: 0; font-size: 13px; opacity: 0.8; }}
                .body {{ padding: 28px 36px; text-align: center; }}
                .body > p {{ margin: 0 0 12px 0; color: #374151; font-size: 14px; text-align: left; }}
                .child-card {{ border: 1px solid #E5E7EB; border-radius: 12px; padding: 16px; margin-bottom: 16px; }}
                .child-name {{ font-weight: 600; font-size: 15px; color: #111827; margin: 0 0 8px 0; }}
                .qr-wrap {{ display: inline-block; padding: 12px; border: 2px solid #D8DDD6; border-radius: 12px; margin: 4px auto; }}
                .qr-wrap img {{ display: block; }}
                .expiry {{ font-size: 12px; color: #6b7280; margin-top: 8px; }}
                .token-code {{ font-family: monospace; font-size: 11px; color: #9ca3af; word-break: break-all; margin-top: 8px; }}
                .note {{ background: #FEF3C7; border-left: 3px solid #F59E0B; padding: 10px 14px; border-radius: 0 6px 6px 0; font-size: 12px; color: #92400E; margin: 16px 0; text-align: left; }}
                .footer {{ padding: 16px 36px; background: #F9FAF8; border-top: 1px solid #E5E7EB; font-size: 11px; color: #9CA3AF; text-align: center; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>{school_name}</h1>
                    <p>Today's Pickup QR Codes</p>
                </div>
                <div class="body">
                    <p>Show the matching QR code to the security officer for each child you're picking up today.</p>
                    {cards_html}
                    <div class="note">
                        Each code is valid for today only and tied to that specific child. New codes are generated every school day.
                    </div>
                </div>
                <div class="footer">
                    <p>Automated message from {school_name}. Do not reply.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_body = f"""
{school_name} — Today's Pickup QR Codes
{cards_text}
Each code is valid today only and tied to that specific child.
        """

        return await self.send_email(
            to=[to],
            subject=f"Today's Pickup QR Codes ({school_name})",
            html_body=html_body,
            text_body=text_body,
        )

    async def send_portal_credentials(
        self,
        to: str,
        parent_name: str,
        student_name: str,
        login_email: str,
        temp_password: str,
        school_name: str = "School ERP",
        portal_url: str = ""
    ) -> Dict:
        """Send parent portal login credentials after account creation"""

        login_link_html = (
            f'<a href="{portal_url}" class="button">Log In to Parent Portal</a>'
            if portal_url else
            "<p>Visit your school's parent portal to log in.</p>"
        )

        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'IBM Plex Sans', Arial, sans-serif; color: #1A1A1A; line-height: 1.6; margin: 0; padding: 0; background-color: #F5F7F4; }}
                .container {{ max-width: 600px; margin: 40px auto; background: #fff; border-radius: 16px; border: 1px solid #D8DDD6; overflow: hidden; }}
                .header {{ background-color: #3D5C3A; color: white; padding: 32px 40px; }}
                .header h1 {{ margin: 0 0 4px 0; font-size: 22px; font-family: 'Manrope', sans-serif; }}
                .header p {{ margin: 0; font-size: 14px; opacity: 0.85; }}
                .body {{ padding: 32px 40px; }}
                .body p {{ margin: 0 0 16px 0; color: #374151; }}
                .credentials-box {{ background: #F5F7F4; border: 1px solid #D8DDD6; border-radius: 10px; padding: 20px 24px; margin: 24px 0; }}
                .credentials-box .label {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.6px; color: #6B7280; margin-bottom: 4px; }}
                .credentials-box .value {{ font-family: 'Courier New', monospace; font-size: 15px; color: #1B2820; font-weight: 600; word-break: break-all; }}
                .credentials-box .row {{ margin-bottom: 16px; }}
                .credentials-box .row:last-child {{ margin-bottom: 0; }}
                .note {{ background: #FEF3C7; border-left: 3px solid #F59E0B; padding: 12px 16px; border-radius: 0 6px 6px 0; font-size: 13px; color: #92400E; margin: 20px 0; }}
                .button {{ display: inline-block; background-color: #3D5C3A; color: white; padding: 12px 28px; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 14px; margin-top: 8px; }}
                .footer {{ padding: 20px 40px; background: #F9FAF8; border-top: 1px solid #E5E7EB; font-size: 12px; color: #9CA3AF; text-align: center; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>{school_name}</h1>
                    <p>Parent Portal Access</p>
                </div>
                <div class="body">
                    <p>Dear {parent_name},</p>
                    <p>A parent portal account has been created for you to monitor <strong>{student_name}</strong>'s academic progress, fees, attendance, and more.</p>

                    <div class="credentials-box">
                        <div class="row">
                            <div class="label">Login Email</div>
                            <div class="value">{login_email}</div>
                        </div>
                        <div class="row">
                            <div class="label">Temporary Password</div>
                            <div class="value">{temp_password}</div>
                        </div>
                    </div>

                    <div class="note">
                        You will be asked to set a new password the first time you log in. Please keep your credentials safe.
                    </div>

                    {login_link_html}
                </div>
                <div class="footer">
                    <p>This is an automated message from {school_name}. Please do not reply to this email.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_body = f"""
Dear {parent_name},

A parent portal account has been created for you at {school_name}.

Child: {student_name}
Login Email: {login_email}
Temporary Password: {temp_password}

You will be prompted to change your password on your first login.

{f'Portal: {portal_url}' if portal_url else 'Please visit your school portal to log in.'}

This is an automated message. Please do not reply.
        """

        return await self.send_email(
            to=[to],
            subject=f"Your Parent Portal Access – {school_name}",
            html_body=html_body,
            text_body=text_body
        )

    async def send_fee_reminder(
        self,
        to: str,
        student_name: str,
        student_id: str,
        amount_due: float,
        due_date: str,
        school_name: str = "School ERP"
    ) -> Dict:
        """Send a fee reminder email"""
        
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'IBM Plex Sans', Arial, sans-serif; color: #1A1A1A; line-height: 1.6; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; background-color: #FAFAED; }}
                .header {{ background-color: #064E3B; color: white; padding: 24px; text-align: center; border-radius: 12px 12px 0 0; }}
                .content {{ background-color: white; padding: 24px; border: 1px solid #E4E4E7; border-top: none; border-radius: 0 0 12px 12px; }}
                .amount {{ font-size: 32px; font-weight: 700; color: #9A3412; margin: 20px 0; font-family: 'JetBrains Mono', monospace; }}
                .info-table {{ width: 100%; margin: 20px 0; }}
                .info-table td {{ padding: 8px 0; }}
                .info-table td:first-child {{ color: #52525B; }}
                .info-table td:last-child {{ font-weight: 500; text-align: right; }}
                .button {{ display: inline-block; background-color: #064E3B; color: white; padding: 12px 32px; text-decoration: none; border-radius: 9999px; font-weight: 500; margin-top: 16px; }}
                .footer {{ text-align: center; font-size: 12px; color: #A1A1AA; margin-top: 24px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Fee Reminder</h1>
                </div>
                <div class="content">
                    <p>Dear Parent/Guardian,</p>
                    <p>This is a friendly reminder that school fees are due for your child.</p>
                    
                    <table class="info-table">
                        <tr>
                            <td>Student Name:</td>
                            <td>{student_name}</td>
                        </tr>
                        <tr>
                            <td>Student ID:</td>
                            <td style="font-family: 'JetBrains Mono', monospace;">{student_id}</td>
                        </tr>
                        <tr>
                            <td>Due Date:</td>
                            <td>{due_date}</td>
                        </tr>
                    </table>
                    
                    <p class="amount">GHS {amount_due:,.2f}</p>
                    
                    <p>Please make payment at the school's administration office or through our designated payment channels.</p>
                    
                    <p>If you have already made this payment, please disregard this reminder.</p>
                </div>
                <div class="footer">
                    <p>{school_name}</p>
                    <p>This is an automated message.</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        return await self.send_email(
            to=[to],
            subject=f"Fee Reminder: {student_name} - GHS {amount_due:,.2f} Due",
            html_body=html_body
        )

    async def send_admission_confirmation(
        self,
        to: str,
        applicant_name: str,
        school_name: str,
        fee_paid: bool = False,
        amount: Optional[float] = None,
    ) -> Dict:
        """Confirms receipt of a public admissions application to the
        guardian's email. `fee_paid`/`amount` are set when this confirms a
        completed application-fee payment rather than a free submission."""

        if fee_paid:
            status_line = f"Your application fee of GHS {amount:,.2f} has been received and your application is now complete." if amount else "Your application fee has been received and your application is now complete."
        else:
            status_line = "Your application has been received and is now with the school's admissions team."

        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'IBM Plex Sans', Arial, sans-serif; color: #1A1A1A; line-height: 1.6; margin: 0; padding: 0; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; background-color: #FAFAED; }}
                .header {{ background-color: #064E3B; color: white; padding: 24px; text-align: center; border-radius: 12px 12px 0 0; }}
                .content {{ background-color: white; padding: 24px; border: 1px solid #E4E4E7; border-top: none; border-radius: 0 0 12px 12px; }}
                .status {{ background: #ECFDF5; border-left: 3px solid #064E3B; padding: 12px 16px; border-radius: 0 6px 6px 0; margin: 20px 0; color: #065F46; }}
                .footer {{ text-align: center; font-size: 12px; color: #A1A1AA; margin-top: 24px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>{school_name}</h1>
                </div>
                <div class="content">
                    <p>Dear Parent/Guardian,</p>
                    <p>Thank you for applying to <strong>{school_name}</strong> on behalf of <strong>{applicant_name}</strong>.</p>
                    <div class="status">{status_line}</div>
                    <p>The school will be in touch regarding next steps. If you have any questions, please contact the school directly.</p>
                </div>
                <div class="footer">
                    <p>{school_name}</p>
                    <p>This is an automated message.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_body = f"""
{school_name} — Application Received

Dear Parent/Guardian,

Thank you for applying to {school_name} on behalf of {applicant_name}.

{status_line}

The school will be in touch regarding next steps.
        """

        return await self.send_email(
            to=[to],
            subject=f"Application Received — {applicant_name} ({school_name})",
            html_body=html_body,
            text_body=text_body,
        )


# Singleton instance
email_service = EmailService()
