"""SMS Service using USMS API"""
import httpx
import logging
from typing import List, Dict, Optional
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class SMSService:
    """USMS SMS Service for sending transactional SMS"""
    
    def __init__(self):
        self.token =settings.usms_token
        self.sender_id = settings.usms_sender_id
        self.base_url = settings.usms_base_url
    
    def _get_headers(self) -> Dict:
        """Get request headers"""
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}"
        }
    
    async def send_sms(
        self,
        phone_numbers: List[str],
        message: str,
        message_type: str = "plain"
    ) -> Dict:
        """
        Send SMS via USMS API
        
        Args:
            phone_numbers: List of recipient phone numbers (e.g., ["+255789123456"])
            message: SMS message content (max 160 chars for standard SMS)
            message_type: Type of message ('plain' for standard SMS)
        
        Returns:
            Response with message IDs and status
        """
        if not self.token:
            logger.warning("USMS token not configured - SMS not sent")
            return {"success": False, "error": "SMS service not configured"}

        if not phone_numbers:
            logger.warning("send_sms called with no phone numbers")
            return {"success": False, "error": "No phone numbers provided"}

        # Validate message length
        if len(message) > 160 and message_type == "plain":
            logger.warning(f"Message exceeds 160 chars: {len(message)}")

        try:
            # Format phone numbers with country code and create comma-separated string per USMS API spec
            formatted_phones = [self.format_phone_number(phone) for phone in phone_numbers]
            recipient = ",".join(formatted_phones)

            payload = {
                "recipient": recipient,
                "message": message,
                "sender_id": self.sender_id,
                "type": "plain"
            }

            logger.debug(f"Calling USMS API at {self.base_url}/api/sms/send for {len(formatted_phones)} recipient(s)")

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/api/sms/send",
                    json=payload,
                    headers=self._get_headers(),
                    timeout=30.0
                )

                logger.debug(f"USMS response {response.status_code}: {response.text}")

                if response.status_code in [200, 201]:
                    data = response.json()
                    logger.info(f"SMS sent successfully to {len(formatted_phones)} recipient(s): {formatted_phones}")
                    return {
                        "success": True,
                        "message_ids": data.get("message_ids", [f"msg_{phone}" for phone in formatted_phones]),
                        "status": "delivered",
                        "recipients": len(formatted_phones)
                    }
                else:
                    error_msg = response.text
                    logger.error(f"Failed to send SMS: {error_msg}")
                    return {
                        "success": False,
                        "error": error_msg,
                        "status_code": response.status_code
                    }

        except Exception as e:
            logger.error(f"Error sending SMS: {str(e)}")
            return {"success": False, "error": str(e)}
    
    async def send_fee_reminder_sms(
        self,
        phone_number: str,
        student_name: str,
        amount_due: float,
        due_date: str
    ) -> Dict:
        """Send fee reminder SMS"""
        # Plain text only - no newlines or special characters
        message = f"Dear Parent, {student_name} has outstanding fees: {amount_due}. Due: {due_date}. Please remit payment."
        
        # SMS message limit check - keep under 160 chars
        if len(message) > 160:
            message = f"{student_name} owes {amount_due} (due {due_date}). Please pay. -School"
        
        return await self.send_sms([phone_number], message)
    
    async def send_attendance_sms(
        self,
        phone_number: str,
        student_name: str,
        attendance_percentage: float
    ) -> Dict:
        """Send attendance notification SMS"""
        message = f"Alert: {student_name}'s attendance is {attendance_percentage}%. Please monitor. -School ERP"
        return await self.send_sms([phone_number], message)
    
    async def send_announcement_sms(
        self,
        phone_numbers: List[str],
        announcement_title: str,
        announcement_snippet: str
    ) -> Dict:
        """Send announcement SMS (bulk)"""
        # Keep message concise and plain - USMS requires plain ASCII text only
        # Limit to 160 chars for single SMS
        title = announcement_title[:40] if announcement_title else "Notice"
        snippet = announcement_snippet[:80] if len(announcement_snippet) > 80 else announcement_snippet
        # Simple plain text format - no special characters
        message = f"Announcement - {title}. {snippet}".strip()
        # Ensure under 160 chars
        if len(message) > 160:
            message = f"{title}: {snippet[:100]}"
        return await self.send_sms(phone_numbers, message)
    
    async def send_grade_notification_sms(
        self,
        phone_number: str,
        student_name: str,
        subject: str,
        grade: str
    ) -> Dict:
        """Send grade notification SMS"""
        message = f"Hi Parent, {student_name} just received a {grade} in {subject}. Login to view details. -School"
        return await self.send_sms([phone_number], message)
    
    async def check_sms_balance(self) -> Dict:
        """Check remaining SMS balance"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/api/balance",
                    headers=self._get_headers(),
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"SMS balance checked: {data}")
                    return {
                        "success": True,
                        "balance": data.get("balance", 0),
                        "currency": data.get("currency", "TZS"),
                        "unit": "SMS Credits"
                    }
                else:
                    return {
                        "success": False,
                        "error": response.text,
                        "status_code": response.status_code
                    }
        except Exception as e:
            logger.error(f"Error checking balance: {str(e)}")
            return {"success": False, "error": str(e)}
    
    def validate_phone_number(self, phone_number: str) -> bool:
        """Validate phone number format"""
        # Basic validation - can be expanded
        if not phone_number:
            return False
        # Remove common separators
        cleaned = phone_number.replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
        # Should have at least 10 digits (Ghana: 054... or +233...)
        return len(cleaned) >= 10 and any(c.isdigit() for c in cleaned)
    
    def format_phone_number(self, phone_number: str) -> str:
        """
        Format phone number to include Ghana country code (+233)
        
        Handles formats:
        - 0534484781 → 233534484781 (remove leading 0, add country code)
        - +233534484781 → 233534484781 (remove +)
        - 233534484781 → 233534484781 (already correct)
        """
        if not phone_number:
            return ""
        
        # Remove all non-digit characters
        cleaned = ''.join(c for c in phone_number if c.isdigit())
        
        # If starts with 0 (local Ghana format), replace with 233
        if cleaned.startswith("0"):
            return "233" + cleaned[1:]
        
        # If already has country code, return as-is
        if cleaned.startswith("233"):
            return cleaned
        
        # If starts with +, assume it's been normalized
        if '+' in phone_number:
            return phone_number.replace("+", "")
        
        # Default: assume local Ghana number
        return "233" + cleaned


# Global instance
sms_service = SMSService()
