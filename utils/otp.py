"""OTP utilities for generating and verifying codes"""
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib
import os
import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from fastapi import HTTPException, status

from models.otp import OTP, OTPSettings, OTPAdminSettings
from models.user import User
from config import get_settings
from services.sms_service import sms_service

settings = get_settings()
logger = logging.getLogger(__name__)


def generate_otp_code(length: int = 6) -> str:
    """Generate a random 6-digit OTP code"""
    return ''.join(random.choices(string.digits, k=length))


async def create_otp(
    session: AsyncSession,
    user_id: str,
    expires_in_minutes: int = 10
) -> Tuple[str, str]:
    """
    Create a new OTP for a user.
    Returns: (otp_code, otp_id)
    """
    print(f"[OTP-CREATE] Creating OTP for user {user_id}")
    
    # Invalidate any existing unused OTPs for this user
    result = await session.execute(
        select(OTP).where(
            (OTP.user_id == user_id) & 
            (OTP.is_used == False)
        )
    )
    old_otps = result.scalars().all()
    for otp in old_otps:
        otp.is_used = True
    
    otp_code = generate_otp_code()
    # Use naive datetime (TIMESTAMP WITHOUT TIME ZONE in DB)
    now = datetime.now()
    expires_at = now + timedelta(minutes=expires_in_minutes)
    
    print(f"[OTP-CREATE] Generated code: {otp_code}, expires at: {expires_at}")
    
    otp = OTP(
        user_id=user_id,
        code=otp_code,
        created_at=now,
        expires_at=expires_at
    )
    
    session.add(otp)
    await session.commit()
    await session.refresh(otp)
    
    print(f"[OTP-CREATE] OTP saved to database with ID: {otp.id}")
    
    return otp_code, otp.id


async def verify_otp(
    session: AsyncSession,
    user_id: str,
    otp_code: str
) -> bool:
    """Verify an OTP code for a user"""
    result = await session.execute(
        select(OTP).where(
            (OTP.user_id == user_id) &
            (OTP.code == otp_code) &
            (OTP.is_used == False) &
            (OTP.attempts < OTP.max_attempts)
        )
    )
    otp = result.scalar_one_or_none()
    
    if not otp:
        # Log failed attempt
        result = await session.execute(
            select(OTP).where(
                (OTP.user_id == user_id) &
                (OTP.code == otp_code)
            )
        )
        otp_record = result.scalar_one_or_none()
        if otp_record:
            otp_record.attempts += 1
            session.add(otp_record)
            await session.commit()
        
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired OTP"
        )
    
    # Check if OTP has expired
    if datetime.now() > otp.expires_at:
        otp.is_used = True
        session.add(otp)
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OTP has expired"
        )
    
    # Mark OTP as used
    otp.is_used = True
    session.add(otp)
    await session.commit()
    
    return True


async def get_otp_settings(
    session: AsyncSession,
    user_id: str
) -> Optional[OTPSettings]:
    """Get OTP settings for a user"""
    result = await session.execute(
        select(OTPSettings).where(OTPSettings.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def create_or_update_otp_settings(
    session: AsyncSession,
    user_id: str,
    is_enabled: bool,
    method: str = "sms"
) -> OTPSettings:
    """Create or update OTP settings for a user"""
    settings_record = await get_otp_settings(session, user_id)
    
    if settings_record:
        settings_record.is_enabled = is_enabled
        settings_record.method = method
        settings_record.updated_at = datetime.now()
    else:
        settings_record = OTPSettings(
            user_id=user_id,
            is_enabled=is_enabled,
            method=method
        )
    
    session.add(settings_record)
    await session.commit()
    await session.refresh(settings_record)
    
    return settings_record


def send_otp_email(email: str, otp_code: str, user_name: str = "User") -> bool:
    """
    Send OTP via email
    Uses SMTP configuration from environment variables
    """
    print(f"[OTP-EMAIL] Sending OTP to {email}")
    
    try:
        sender_email = os.getenv("SMTP_EMAIL", "noreply@schoolerp.com")
        sender_password = os.getenv("SMTP_PASSWORD", "")
        smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        
        print(f"[OTP-EMAIL] SMTP config - server: {smtp_server}, port: {smtp_port}, from: {sender_email}")
        
        # Create message
        message = MIMEMultipart("alternative")
        message["Subject"] = "Your School ERP Login Code"
        message["From"] = sender_email
        message["To"] = email
        
        # Email body
        text = f"""Your login code is: {otp_code}
        
This code will expire in 10 minutes.

Do not share this code with anyone.
"""
        
        html = f"""\
<html>
  <body>
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <div style="background-color: #f8f9fa; padding: 20px; text-align: center; border-radius: 8px;">
        <h2 style="color: #333;">School ERP Login</h2>
        <p style="color: #666; margin: 20px 0;">Hi {user_name},</p>
        <p style="color: #666;">Your verification code is:</p>
        <div style="background-color: #fff; padding: 20px; border-radius: 8px; margin: 20px 0; border: 2px solid #e9ecef;">
          <h1 style="color: #007bff; letter-spacing: 5px; margin: 0;">{otp_code}</h1>
        </div>
        <p style="color: #999; font-size: 14px;">This code will expire in 10 minutes.</p>
        <p style="color: #999; font-size: 12px;">Do not share this code with anyone.</p>
      </div>
    </div>
  </body>
</html>
"""
        
        part1 = MIMEText(text, "plain")
        part2 = MIMEText(html, "html")
        message.attach(part1)
        message.attach(part2)
        
        # Send email
        print(f"[OTP-EMAIL] Connecting to SMTP server...")
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, email, message.as_string())
        
        print(f"[OTP-EMAIL] OK: Email sent successfully to {email}")
        return True

    except Exception as e:
        print(f"[OTP-EMAIL] FAILED: Error sending OTP email: {str(e)}")
        logger.error(f"OTP email send failed: {str(e)}")
        return False


async def send_otp_sms(phone_number: str, otp_code: str) -> bool:
    """
    Send OTP via SMS using SMS Service (USMS-GH integration)
    
    Args:
        phone_number: Phone number in any format (0534..., 233..., +233...)
        otp_code: 6-digit OTP code
    
    Returns:
        bool: True if SMS sent successfully, False otherwise
    
    Note:
        This function uses the existing SMSService which handles:
        - Phone number formatting for Ghana
        - USMS-GH API integration
        - Error handling and logging
    """
    try:
        if not phone_number:
            print(f"[OTP-SMS] No phone number provided")
            logger.warning("No phone number provided for OTP SMS")
            return False
        
        print(f"[OTP-SMS] Sending OTP to {phone_number}")
        
        # Prepare OTP message (keep under 160 characters)
        message = f"Your School ERP login code is: {otp_code}. Valid for 10 minutes."
        
        # Use the existing SMS service to send OTP
        # sms_service.send_sms() is async and handles all the complexity
        print(f"[OTP-SMS] Calling SMS service...")
        result = await sms_service.send_sms(
            phone_numbers=[phone_number],
            message=message,
            message_type="plain"
        )
        
        # Check if SMS was sent successfully
        if result.get("success"):
            print(f"[OTP-SMS] OK: SMS sent successfully to {phone_number}")
            logger.info(f"OTP SMS sent successfully to {phone_number}")
            return True
        else:
            error = result.get("error", "Unknown error")
            print(f"[OTP-SMS] FAILED: Failed to send SMS: {error}")
            logger.error(f"Failed to send OTP SMS: {error}")
            return False

    except Exception as e:
        print(f"[OTP-SMS] FAILED: Exception: {str(e)}")
        logger.error(f"Error sending OTP SMS: {str(e)}")
        return False


async def get_admin_otp_settings(
    session: AsyncSession,
    school_id: str
) -> Optional[OTPAdminSettings]:
    """Get OTP admin settings for a school"""
    result = await session.execute(
        select(OTPAdminSettings).where(OTPAdminSettings.school_id == school_id)
    )
    return result.scalar_one_or_none()


async def create_or_update_admin_otp_settings(
    session: AsyncSession,
    school_id: str,
    is_enabled: bool = True,
    expiry_minutes: int = 10,
    max_attempts: int = 3,
    default_method: str = "sms",
    require_for_roles: list[str] = None
) -> OTPAdminSettings:
    """Create or update admin OTP settings for a school"""
    if require_for_roles is None:
        require_for_roles = ["school_admin", "hr"]
    
    settings = await get_admin_otp_settings(session, school_id)
    
    # Validate inputs
    if expiry_minutes < 1 or expiry_minutes > 60:
        raise ValueError("expiry_minutes must be between 1 and 60")
    
    if max_attempts < 1 or max_attempts > 10:
        raise ValueError("max_attempts must be between 1 and 10")
    
    if default_method not in ["email", "sms"]:
        raise ValueError("default_method must be 'email' or 'sms'")
    
    roles_str = ",".join(require_for_roles)
    
    if settings:
        settings.is_enabled = is_enabled
        settings.expiry_minutes = expiry_minutes
        settings.max_attempts = max_attempts
        settings.default_method = default_method
        settings.require_for_roles = roles_str
        settings.updated_at = datetime.now()
    else:
        settings = OTPAdminSettings(
            school_id=school_id,
            is_enabled=is_enabled,
            expiry_minutes=expiry_minutes,
            max_attempts=max_attempts,
            default_method=default_method,
            require_for_roles=roles_str
        )
    
    session.add(settings)
    await session.commit()
    await session.refresh(settings)
    
    logger.info(f"Updated OTP admin settings for school {school_id}")
    return settings
