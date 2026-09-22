"""Paystack payment gateway integration"""
import httpx
import logging
import os
from typing import Dict, Optional
from datetime import datetime
import hmac
import hashlib
import json

logger = logging.getLogger(__name__)


class PaystackService:
    """Low-level Paystack API client"""

    BASE_URL = "https://api.paystack.co"

    # Explicit channel list rather than leaving it to whatever's configured
    # on the Paystack dashboard — this is a Ghana-market platform, and USSD
    # in particular (paying from a basic phone, no app/bank-app needed) is
    # a real channel parents here actually use, not a nice-to-have. Applies
    # to every caller of initialize_payment: parent fee payments and
    # school subscription payments alike.
    DEFAULT_CHANNELS = ["card", "bank", "ussd", "mobile_money", "bank_transfer"]

    def __init__(self, secret_key: str):
        self.secret_key = secret_key

    async def initialize_payment(
        self,
        amount_kobo: int,
        email: str,
        reference: str,
        metadata: Dict = None,
        subaccount: Optional[str] = None,
        channels: Optional[list] = None,
    ) -> Dict:
        """
        Initialize payment with Paystack

        Args:
            amount_kobo: Amount in kobo (GHS 100 = 10000 kobo)
            email: Parent email address
            reference: Unique reference for this payment
            metadata: Additional data to pass through
            subaccount: Paystack subaccount code. When set, this single
                transaction splits automatically: the subaccount's configured
                percentage_charge determines the main account's cut and the
                subaccount gets the remainder, settled straight to the
                subaccount's own bank/MoMo account — no group/split object
                needed for a single-subaccount split.
            channels: Which payment methods Paystack's checkout offers —
                defaults to DEFAULT_CHANNELS (card/bank/ussd/mobile_money/
                bank_transfer) rather than the dashboard's configured
                default, so a merchant-account setting change can't
                silently drop USSD or mobile money for parents.

        Returns:
            {
                "success": True,
                "authorization_url": "https://checkout.paystack.com/...",
                "access_code": "...",
                "reference": "..."
            }
        """
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "amount": amount_kobo,
            "email": email,
            "reference": reference,
            "metadata": metadata or {},
            "channels": channels if channels is not None else self.DEFAULT_CHANNELS,
        }
        if subaccount:
            payload["subaccount"] = subaccount
        
        logger.info(f"Paystack request - Amount: {amount_kobo}, Email: {email}, Ref: {reference}")
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.BASE_URL}/transaction/initialize",
                    headers=headers,
                    json=payload,
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"Payment initialized: {reference}")
                    return {
                        "success": True,
                        "authorization_url": data["data"]["authorization_url"],
                        "access_code": data["data"]["access_code"],
                        "reference": data["data"]["reference"]
                    }
                else:
                    error_data = response.json()
                    error_msg = error_data.get("message", "Payment initialization failed")
                    logger.error(f"Paystack initialize failed (status {response.status_code}): {error_msg}")
                    logger.error(f"Full response: {response.text}")
                    return {
                        "success": False,
                        "error": error_msg
                    }
        
        except Exception as e:
            logger.error(f"Paystack API error: {str(e)}")
            return {
                "success": False,
                "error": f"Connection error: {str(e)}"
            }

    async def charge_authorization(
        self,
        authorization_code: str,
        amount_kobo: int,
        email: str,
        reference: str,
        metadata: Dict = None,
    ) -> Dict:
        """
        Charge a previously-saved card without the customer re-entering
        details — the auto-renewal path (services/scheduler.py). Unlike
        initialize_payment, this returns the charge outcome synchronously
        (no checkout redirect); Paystack also fires the usual charge.success
        / charge.failed webhook for it, which routers/payments.py already
        handles idempotently, so both paths are safe to leave active.

        Args:
            authorization_code: from a prior successful charge's
                `data.authorization.authorization_code`, saved only when
                that authorization was `reusable`.
            amount_kobo: amount in kobo (GHS 100 = 10000 kobo)
            email: must match the email the authorization was created under
            reference: unique reference for this charge

        Returns:
            {"success": True, "status": "success"|"failed", "reference": "...", "data": {...}}
            or {"success": False, "error": "..."} if the request itself failed
            (network error, invalid authorization, etc. — not a declined card,
            which comes back as success=True, status="failed").
        """
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "authorization_code": authorization_code,
            "amount": amount_kobo,
            "email": email,
            "reference": reference,
            "metadata": metadata or {}
        }

        logger.info(f"Paystack charge_authorization - Amount: {amount_kobo}, Email: {email}, Ref: {reference}")

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.BASE_URL}/transaction/charge_authorization",
                    headers=headers,
                    json=payload,
                    timeout=15.0
                )

                if response.status_code == 200:
                    data = response.json()
                    charge_status = data.get("data", {}).get("status")
                    logger.info(f"charge_authorization result for {reference}: {charge_status}")
                    return {
                        "success": True,
                        "status": charge_status,
                        "reference": data.get("data", {}).get("reference", reference),
                        "data": data.get("data", {}),
                    }
                else:
                    error_data = response.json()
                    error_msg = error_data.get("message", "Charge failed")
                    logger.error(f"Paystack charge_authorization failed (status {response.status_code}): {error_msg}")
                    return {"success": False, "error": error_msg}

        except Exception as e:
            logger.error(f"Paystack charge_authorization error: {str(e)}")
            return {"success": False, "error": f"Connection error: {str(e)}"}

    async def list_banks(self, country: str = "ghana") -> Dict:
        """Lists banks + mobile money providers Paystack supports for a country, for a settlement-account dropdown."""
        headers = {"Authorization": f"Bearer {self.secret_key}"}
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.BASE_URL}/bank",
                    headers=headers,
                    params={"country": country, "currency": "GHS"},
                    timeout=10.0,
                )
                if response.status_code == 200:
                    data = response.json()
                    return {
                        "success": True,
                        "banks": [
                            {"name": b.get("name"), "code": b.get("code"), "type": b.get("type")}
                            for b in data.get("data", [])
                        ],
                    }
                return {"success": False, "error": response.json().get("message", "Failed to list banks")}
        except Exception as e:
            logger.error(f"Paystack list_banks error: {str(e)}")
            return {"success": False, "error": str(e)}

    async def resolve_account_number(self, account_number: str, bank_code: str) -> Dict:
        """Confirms an account number against a bank/MoMo code and returns the real account holder's
        name, straight from the bank — this is the actual anti-fraud check: it catches a typo'd or
        made-up account number immediately, before an admin ever has to eyeball it."""
        headers = {"Authorization": f"Bearer {self.secret_key}"}
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.BASE_URL}/bank/resolve",
                    headers=headers,
                    params={"account_number": account_number, "bank_code": bank_code},
                    timeout=10.0,
                )
                if response.status_code == 200:
                    data = response.json().get("data", {})
                    return {"success": True, "account_name": data.get("account_name"), "account_number": data.get("account_number")}
                return {"success": False, "error": response.json().get("message", "Could not resolve account number — check the number and bank")}
        except Exception as e:
            logger.error(f"Paystack resolve_account_number error: {str(e)}")
            return {"success": False, "error": str(e)}

    async def create_subaccount(
        self,
        business_name: str,
        settlement_bank: str,
        account_number: str,
        percentage_charge: float = 0,
    ) -> Dict:
        """Creates a Paystack subaccount that a transaction can route straight to.

        percentage_charge is the percentage the MAIN account keeps — 0 means
        the subaccount (the teacher) receives the full transaction amount,
        settled directly to their own bank/MoMo account on Paystack's normal
        schedule, with no manual payout step on our side at all.
        """
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "business_name": business_name,
            "settlement_bank": settlement_bank,
            "account_number": account_number,
            "percentage_charge": percentage_charge,
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.BASE_URL}/subaccount",
                    headers=headers,
                    json=payload,
                    timeout=10.0,
                )
                if response.status_code in (200, 201):
                    data = response.json().get("data", {})
                    return {
                        "success": True,
                        "subaccount_code": data.get("subaccount_code"),
                        "account_name": data.get("account_name") or data.get("settlement_account", {}).get("account_name"),
                    }
                return {"success": False, "error": response.json().get("message", "Failed to create subaccount")}
        except Exception as e:
            logger.error(f"Paystack create_subaccount error: {str(e)}")
            return {"success": False, "error": str(e)}

    async def charge_mobile_money(
        self,
        amount_kobo: int,
        email: str,
        phone: str,
        provider: str,
        reference: str,
        metadata: Dict = None,
        subaccount: Optional[str] = None,
    ) -> Dict:
        """Initiate a mobile-money charge — pushes an approval PROMPT to the
        parent's phone, so payment works on any handset with no app or browser.

        Args:
            amount_kobo: amount in pesewas (GHS x 100)
            phone: MoMo wallet number, e.g. "0244000000"
            provider: Paystack MoMo code — "mtn", "vod" (Telecel), or "atl" (AirtelTigo)
            reference: our transaction reference

        The charge resolves asynchronously: the parent approves on their phone
        and Paystack fires the same charge.success webhook as card payments.
        """
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "email": email,
            "amount": amount_kobo,
            "currency": "GHS",
            "reference": reference,
            "mobile_money": {"phone": phone, "provider": provider},
            "metadata": metadata or {},
        }
        if subaccount:
            payload["subaccount"] = subaccount

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.BASE_URL}/charge",
                    headers=headers,
                    json=payload,
                    timeout=15.0,
                )
                data = response.json()
                if response.status_code == 200 and data.get("status"):
                    charge = data.get("data", {})
                    logger.info(
                        f"MoMo charge initiated: {reference} -> {phone} ({provider}), "
                        f"status={charge.get('status')}"
                    )
                    return {
                        "success": True,
                        "charge_status": charge.get("status"),  # e.g. pay_offline, send_otp
                        "display_text": charge.get("display_text")
                        or "A payment prompt has been sent to the phone. Approve it to complete payment.",
                        "reference": charge.get("reference", reference),
                    }
                error_msg = data.get("message", "Mobile money charge failed")
                logger.error(f"Paystack MoMo charge failed: {response.text[:300]}")
                return {"success": False, "error": error_msg}
        except Exception as e:
            logger.error(f"Paystack MoMo charge error: {str(e)}")
            return {"success": False, "error": f"Connection error: {str(e)}"}

    async def verify_payment(self, reference: str) -> Dict:
        """
        Verify payment status with Paystack
        
        Args:
            reference: Paystack transaction reference
        
        Returns:
            {
                "success": True,
                "data": {...full transaction data...},
                "status": "success" or "failed"
            }
        """
        headers = {
            "Authorization": f"Bearer {self.secret_key}"
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.BASE_URL}/transaction/verify/{reference}",
                    headers=headers,
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"Payment verified: {reference}")
                    return {
                        "success": True,
                        "data": data["data"],
                        "status": data["data"]["status"]  # "success" or "failed"
                    }
                else:
                    logger.error(f"Paystack verify failed: {response.text}")
                    return {
                        "success": False,
                        "error": "Payment verification failed"
                    }
        
        except Exception as e:
            logger.error(f"Paystack verify error: {str(e)}")
            return {
                "success": False,
                "error": f"Verification error: {str(e)}"
            }
    
    @staticmethod
    def verify_webhook_signature(payload_bytes: bytes, signature: str, secret_key: str) -> bool:
        """
        Verify that webhook came from Paystack
        
        Args:
            payload_bytes: Raw webhook body
            signature: X-Paystack-Signature header
            secret_key: Your Paystack secret key
        
        Returns:
            True if signature is valid
        """
        hash = hmac.new(
            key=secret_key.encode(),
            msg=payload_bytes,
            digestmod=hashlib.sha512
        )
        computed_sig = hash.hexdigest()
        
        return hmac.compare_digest(computed_sig, signature)
    
    async def get_account_balance(self) -> Dict:
        """
        Get current Paystack account balance
        
        Returns:
        {
            "success": True,
            "balance": 50000,  # in kobo
            "currency": "GHS"
        }
        """
        headers = {
            "Authorization": f"Bearer {self.secret_key}"
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.BASE_URL}/balance",
                    headers=headers,
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    # Convert kobo to GHS
                    balance_ghs = data["data"][0]["balance"] / 100 if data["data"] else 0
                    logger.info(f"Balance fetched: GHS {balance_ghs}")
                    return {
                        "success": True,
                        "balance": balance_ghs,
                        "currency": "GHS"
                    }
                else:
                    logger.error(f"Balance fetch failed: {response.text}")
                    return {
                        "success": False,
                        "error": "Failed to fetch balance",
                        "balance": 0
                    }
        
        except Exception as e:
            logger.error(f"Balance error: {str(e)}")
            return {
                "success": False,
                "error": str(e),
                "balance": 0
            }
    
    async def create_transfer_recipient(
        self,
        type_: str,
        account_number: str,
        account_name: str,
        currency: str = "GHS",
        bank_code: str = None
    ) -> Dict:
        """
        Create a transfer recipient (MoMo or bank account)
        
        Args:
            type_: "mobile_money" or "nuban"
            account_number: MoMo number or bank account
            account_name: Name of recipient
            currency: "GHS"
            bank_code: Bank code (e.g., "MTN", "VOD", "ATL" for mobile_money)
        
        Returns:
        {
            "success": True,
            "recipient_code": "RCP_xxx",
            "account_number": "...",
            "account_name": "..."
        }
        """
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "type": type_,
            "account_number": account_number,
            "account_name": account_name,
            "currency": currency
        }
        
        # Add bank_code for mobile_money transfers (required by Paystack for Ghana)
        if type_ == "mobile_money" and bank_code:
            payload["bank_code"] = bank_code
            logger.info(f"Adding bank_code {bank_code} for MoMo transfer to {account_number}")
        
        logger.debug(f"Transfer recipient payload: {payload}")
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.BASE_URL}/transferrecipient",
                    headers=headers,
                    json=payload,
                    timeout=10.0
                )
                
                if response.status_code == 201:
                    data = response.json()
                    logger.debug(f"Paystack response data: {data}")
                    
                    # Extract recipient data safely
                    recipient_data = data.get("data", {})
                    return {
                        "success": True,
                        "recipient_code": recipient_data.get("recipient_code", ""),
                        "account_number": recipient_data.get("account_number", account_number),
                        "account_name": recipient_data.get("account_name", account_name)
                    }
                else:
                    logger.error(f"Recipient creation failed: {response.text}")
                    return {
                        "success": False,
                        "error": response.json().get("message", "Failed to create recipient")
                    }
        
        except Exception as e:
            logger.error(f"Recipient creation error: {str(e)}", exc_info=True)
            return {
                "success": False,
                "error": f"Recipient creation failed: {str(e)}"
            }
    
    async def initiate_transfer(
        self,
        source: str,
        amount: int,
        recipient_code: str,
        reason: str,
        reference: str
    ) -> Dict:
        """
        Initiate a transfer to recipient
        
        Args:
            source: "balance" (transfer from Paystack balance)
            amount: Amount in kobo
            recipient_code: Recipient code from create_transfer_recipient
            reason: Transfer description
            reference: Unique reference for tracking
        
        Returns:
        {
            "success": True,
            "transfer_code": "TRF_xxx",
            "reference": "...",
            "amount": 50000,
            "status": "pending"
        }
        """
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "source": source,
            "amount": amount,
            "recipient": recipient_code,
            "reason": reason,
            "reference": reference
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.BASE_URL}/transfer",
                    headers=headers,
                    json=payload,
                    timeout=10.0
                )
                
                if response.status_code in [200, 201]:
                    data = response.json()
                    logger.debug(f"Paystack transfer response: {data}")
                    
                    # Extract transfer data safely
                    transfer_data = data.get("data", {})
                    return {
                        "success": True,
                        "transfer_code": transfer_data.get("transfer_code", ""),
                        "reference": transfer_data.get("reference", reference),
                        "amount": transfer_data.get("amount", amount) / 100,  # Convert to GHS
                        "status": transfer_data.get("status", "pending"),
                        "recipient": transfer_data.get("recipient", recipient_code)
                    }
                else:
                    logger.error(f"Transfer initiation failed: {response.text}")
                    return {
                        "success": False,
                        "error": response.json().get("message", "Transfer initiation failed")
                    }
        
        except Exception as e:
            logger.error(f"Transfer initiation error: {str(e)}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }
    
    async def verify_transfer(self, transfer_code: str) -> Dict:
        """
        Verify status of a transfer
        
        Args:
            transfer_code: Paystack transfer code
        
        Returns:
        {
            "success": True,
            "transfer_code": "TRF_xxx",
            "status": "success|pending|failed",
            "amount": 50000,
            "reason": "..."
        }
        """
        headers = {
            "Authorization": f"Bearer {self.secret_key}"
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.BASE_URL}/transfer/verify/{transfer_code}",
                    headers=headers,
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    return {
                        "success": True,
                        "transfer_code": data["data"]["transfer_code"],
                        "status": data["data"]["status"],
                        "amount": data["data"]["amount"] / 100,
                        "reason": data["data"]["reason"]
                    }
                else:
                    logger.error(f"Transfer verification failed: {response.text}")
                    return {
                        "success": False,
                        "error": "Transfer verification failed"
                    }
        
        except Exception as e:
            logger.error(f"Transfer verification error: {str(e)}")
            return {
                "success": False,
                "error": str(e)
            }
