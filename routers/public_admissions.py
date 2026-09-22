"""Public Admissions Router

UNAUTHENTICATED — no `get_current_user`/`require_roles` on any endpoint here.
This is the public-facing admissions application form: a prospective
parent/guardian reaches `/apply/{school_code}` from a link the school shares
(website, flyer, social media) with no login of any kind. Every endpoint is
reachable by anyone on the internet, so:

- Only `School.name`/`logo_url`/fee settings are ever returned — never
  internal IDs, payout details, or anything else on the School row.
- Submissions and uploads are IP-rate-limited via Redis (fails open if Redis
  is down, matching auth.py::get_current_user's existing cache-unavailable
  philosophy — infra hiccups should never block a legitimate applicant).
- A hidden `honeypot` field must arrive empty; a bot that fills it gets a
  normal-looking success response with nothing written to the database.
- Document uploads are content-type/size restricted and capped per applicant.
- When a school requires an application fee, payment goes through the same
  Paystack + OnlineTransaction + webhook machinery as every other payment in
  this app (see services/online_payment_service.py), just tagged
  TransactionType.ADMISSION_FEE so it never touches Fee/GL — there's no
  Student yet, just a public Applicant.
"""
import logging
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlmodel import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.school import School
from models.admissions import Applicant, ApplicantDocument, ApplicantDocumentType, ApplicationStatus, PublicApplicantCreate
from models.admissions_enterprise import AdmissionDeposit, AdmissionDepositStatus, AdmissionDepositOnlinePaymentRequest
from models.payment import OnlineTransaction, TransactionType, TransactionStatus
from database import get_session
from auth import get_redis
from services.online_payment_service import OnlinePaymentService
from services.email_service import email_service

router = APIRouter(prefix="/public", tags=["Public"])
logger = logging.getLogger(__name__)

RATE_LIMIT_MAX_PER_HOUR = 5
UPLOAD_RATE_LIMIT_MAX_PER_HOUR = 20
# Higher ceiling than the write endpoints above: these are read-only status
# lookups a guardian's browser legitimately polls every few seconds while
# waiting for a Paystack checkout/webhook to resolve, not a one-shot action.
STATUS_POLL_RATE_LIMIT_MAX_PER_HOUR = 60

UPLOAD_DIR = Path("uploads/admissions")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_DOCUMENT_CONTENT_TYPES = {"application/pdf", "image/jpeg", "image/png"}
MAX_DOCUMENT_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_DOCUMENTS_PER_APPLICANT = 5


def _get_client_ip(request: Request) -> str:
    """The rate-limit key. The direct TCP peer (request.client.host) can't
    be spoofed by the caller and is used by default. X-Forwarded-For is
    attacker-controlled unless the app sits behind a trusted reverse proxy
    that overwrites (never appends to) the header before forwarding — no
    such trust boundary exists in this deployment (no ProxyHeadersMiddleware,
    no trusted-proxy allow-list configured anywhere), so trusting it let
    anyone reset their own rate-limit bucket on every request by sending a
    fresh, arbitrary value, completely defeating _check_rate_limit below.
    A deployment that genuinely does run behind such a proxy can opt back
    into the old behavior by setting TRUST_X_FORWARDED_FOR=true — do this
    only if the proxy is configured to strip/overwrite any client-supplied
    X-Forwarded-For before appending its own."""
    if os.getenv("TRUST_X_FORWARDED_FOR", "").lower() in ("1", "true", "yes"):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _check_rate_limit(ip: str, key_prefix: str = "public_admissions_rl", max_per_hour: int = RATE_LIMIT_MAX_PER_HOUR) -> None:
    redis_client = await get_redis()
    if not redis_client:
        return
    key = f"{key_prefix}:{ip}"
    try:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, 3600)
        if count > max_per_hour:
            raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Rate limit check failed ({e}), allowing request")


async def _get_active_school(session: AsyncSession, school_code: str) -> School:
    result = await session.execute(select(School).where(School.code == school_code))
    school = result.scalar_one_or_none()
    if not school or not school.is_active or school.access_suspended:
        raise HTTPException(status_code=404, detail="School not found")
    return school


@router.get("/schools/{school_code}", response_model=dict)
async def get_public_school_info(
    school_code: str,
    session: AsyncSession = Depends(get_session),
):
    """Confirms which school a public applicant is applying to, and whether
    an application fee must be paid to complete submission. Deliberately
    returns nothing beyond that — no internal IDs, no contact/banking
    details."""
    school = await _get_active_school(session, school_code)
    return {
        "name": school.name,
        "logo_url": school.logo_url,
        "require_application_fee": school.require_application_fee,
        "application_fee_amount": school.application_fee_amount if school.require_application_fee else None,
    }


@router.post("/admissions/{school_code}/apply", response_model=dict)
async def submit_public_application(
    school_code: str,
    request: Request,
    data: PublicApplicantCreate,
    session: AsyncSession = Depends(get_session),
):
    ip = _get_client_ip(request)
    await _check_rate_limit(ip)

    school = await _get_active_school(session, school_code)

    if data.honeypot:
        logger.info(f"Public admissions honeypot triggered from IP {ip} for school {school_code}")
        return {"success": True, "payment_required": False, "message": "Application submitted successfully."}

    # Guard against a literal double-submit (a slow network retry, an
    # impatient double-click on the submit button) creating two Applicant
    # rows for what's actually one submission attempt. Deliberately narrow
    # -- a genuine re-application weeks/months later (a different admission
    # cycle) falls outside this 10-minute window and is NOT blocked; the
    # original audit flagged re-applications as a legitimate, intentional
    # flow that must keep working.
    retry_window_start = datetime.utcnow() - timedelta(minutes=10)
    existing_result = await session.execute(
        select(Applicant).where(
            Applicant.school_id == school.id,
            Applicant.first_name == data.first_name,
            Applicant.last_name == data.last_name,
            Applicant.date_of_birth == data.date_of_birth,
            Applicant.guardian_phone == data.guardian_phone,
            Applicant.created_at >= retry_window_start,
        ).order_by(Applicant.created_at.desc())
    )
    existing_applicant = existing_result.scalars().first()
    if existing_applicant:
        if school.require_application_fee and school.application_fee_amount and school.application_fee_amount > 0:
            txn_result = await session.execute(
                select(OnlineTransaction).where(
                    OnlineTransaction.school_id == school.id,
                    OnlineTransaction.fee_id == existing_applicant.id,
                    OnlineTransaction.transaction_type == TransactionType.ADMISSION_FEE,
                ).order_by(OnlineTransaction.created_at.desc())
            )
            existing_txn = txn_result.scalars().first()
            if existing_txn and existing_txn.status in (TransactionStatus.PENDING, TransactionStatus.PROCESSING) and existing_txn.payment_url:
                return {
                    "success": True,
                    "applicant_id": existing_applicant.id,
                    "payment_required": True,
                    "payment_url": existing_txn.payment_url,
                    "reference": existing_txn.reference,
                    "amount": school.application_fee_amount,
                    "message": "This application was already submitted a moment ago. Continue to payment below.",
                }
        return {
            "success": True,
            "applicant_id": existing_applicant.id,
            "payment_required": False,
            "message": "This application was already submitted a moment ago.",
        }

    applicant = Applicant(
        school_id=school.id,
        first_name=data.first_name,
        last_name=data.last_name,
        other_names=data.other_names,
        date_of_birth=data.date_of_birth,
        gender=data.gender,
        guardian_name=data.guardian_name,
        guardian_relationship=data.guardian_relationship,
        guardian_phone=data.guardian_phone,
        guardian_email=data.guardian_email,
        notes=data.notes,
        application_fee_amount=school.application_fee_amount if school.require_application_fee else None,
    )
    session.add(applicant)
    await session.commit()
    await session.refresh(applicant)

    if school.require_application_fee and school.application_fee_amount and school.application_fee_amount > 0:
        paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
        if not paystack_secret_key:
            logger.error(f"Application fee required for school {school.code} but PAYSTACK_SECRET_KEY not configured")
            raise HTTPException(status_code=503, detail="Online payment is not available right now. Please contact the school directly.")

        payment_service = OnlinePaymentService(paystack_secret_key)
        transaction_id = f"TXN-{uuid.uuid4().hex[:12].upper()}"

        # fee_id/student_id/parent_id are all NOT NULL on OnlineTransaction but
        # don't semantically apply here — there's no Fee/Student/Parent yet,
        # just this Applicant. Reusing applicant.id for all three mirrors the
        # same trick CANTEEN_TOPUP uses (fee_id=student_id) rather than
        # widening a heavily-used financial table's schema for one edge case.
        transaction = OnlineTransaction(
            school_id=school.id,
            fee_id=applicant.id,
            student_id=applicant.id,
            parent_id=applicant.id,
            amount=school.application_fee_amount,
            gateway="paystack",
            reference=transaction_id,
            transaction_type=TransactionType.ADMISSION_FEE,
            status=TransactionStatus.PENDING,
        )
        session.add(transaction)
        await session.flush()

        payer_email = data.guardian_email or f"applicant-{applicant.id}@campusio.online"
        paystack_result = await payment_service.paystack.initialize_payment(
            amount_kobo=int(school.application_fee_amount * 100),
            email=payer_email,
            reference=transaction_id,
            metadata={"applicant_id": applicant.id, "admission_fee": True},
        )

        if not paystack_result.get("success"):
            transaction.status = TransactionStatus.FAILED
            transaction.failed_reason = paystack_result.get("error", "Payment initialization failed")
            session.add(transaction)
            await session.commit()
            raise HTTPException(status_code=502, detail="Could not start payment. Please try again shortly.")

        transaction.payment_url = paystack_result["authorization_url"]
        transaction.access_code = paystack_result["access_code"]
        transaction.reference = paystack_result["reference"]
        transaction.status = TransactionStatus.PROCESSING
        session.add(transaction)
        await session.commit()

        return {
            "success": True,
            "applicant_id": applicant.id,
            "payment_required": True,
            "payment_url": paystack_result["authorization_url"],
            "reference": paystack_result["reference"],
            "amount": school.application_fee_amount,
            "message": "Application saved. Please complete the application fee payment to finish submitting.",
        }

    if data.guardian_email:
        try:
            await email_service.send_admission_confirmation(
                to=data.guardian_email,
                applicant_name=f"{applicant.first_name} {applicant.last_name}",
                school_name=school.name,
                fee_paid=False,
            )
        except Exception as e:
            logger.error(f"Failed to send admission confirmation email: {e}")

    return {
        "success": True,
        "applicant_id": applicant.id,
        "payment_required": False,
        "message": "Application submitted successfully. The school will contact you soon.",
    }


async def _poll_transaction(session: AsyncSession, school: School, reference: str, transaction_type: TransactionType) -> OnlineTransaction:
    """Shared by every public payment-status endpoint (application fee,
    deposit): reads the transaction if the webhook already landed;
    otherwise actively verifies with Paystack so the payer isn't stuck
    waiting on webhook delivery — same fallback pattern as
    routers/payments.py's manual verify."""
    result = await session.execute(
        select(OnlineTransaction).where(
            and_(
                OnlineTransaction.reference == reference,
                OnlineTransaction.school_id == school.id,
                OnlineTransaction.transaction_type == transaction_type,
            )
        )
    )
    transaction = result.scalar_one_or_none()
    if not transaction:
        raise HTTPException(status_code=404, detail="Payment not found")

    if transaction.status in (TransactionStatus.SUCCESS, TransactionStatus.FAILED):
        return transaction

    paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
    if not paystack_secret_key:
        return transaction

    payment_service = OnlinePaymentService(paystack_secret_key)
    verify_result = await payment_service.paystack.verify_payment(reference)
    if verify_result.get("success"):
        await payment_service.process_webhook(session, {"data": verify_result["data"]})
        await session.refresh(transaction)

    return transaction


@router.get("/admissions/{school_code}/payment/status/{reference}", response_model=dict)
async def check_public_admission_payment_status(
    school_code: str,
    reference: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Polled by the apply page after the Paystack checkout window closes."""
    ip = _get_client_ip(request)
    await _check_rate_limit(ip, key_prefix="public_admissions_status_rl", max_per_hour=STATUS_POLL_RATE_LIMIT_MAX_PER_HOUR)

    school = await _get_active_school(session, school_code)
    transaction = await _poll_transaction(session, school, reference, TransactionType.ADMISSION_FEE)
    if transaction.status == TransactionStatus.SUCCESS:
        return {"status": "success", "applicant_id": transaction.student_id}
    if transaction.status == TransactionStatus.FAILED:
        return {"status": "failed", "error": transaction.failed_reason}
    return {"status": "pending"}


@router.get("/admissions/{school_code}/deposits/{deposit_id}", response_model=dict)
async def get_public_deposit_info(
    school_code: str,
    deposit_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Lets a guardian who received a deposit-payment link see what's owed
    before paying — no auth, so only balance/status is returned, never
    anything else about the applicant record."""
    ip = _get_client_ip(request)
    await _check_rate_limit(ip, key_prefix="public_admissions_status_rl", max_per_hour=STATUS_POLL_RATE_LIMIT_MAX_PER_HOUR)

    school = await _get_active_school(session, school_code)
    result = await session.execute(
        select(AdmissionDeposit, Applicant)
        .join(Applicant, Applicant.id == AdmissionDeposit.applicant_id)
        .where(AdmissionDeposit.id == deposit_id, AdmissionDeposit.school_id == school.id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Deposit not found")
    deposit, applicant = row
    return {
        "applicant_name": f"{applicant.first_name} {applicant.last_name}",
        "required_amount": deposit.required_amount,
        "paid_amount": deposit.paid_amount,
        "balance": deposit.required_amount - deposit.paid_amount,
        "status": deposit.status.value,
        "due_date": deposit.due_date,
    }


@router.post("/admissions/{school_code}/deposits/{deposit_id}/pay", response_model=dict)
async def pay_public_deposit(
    school_code: str,
    deposit_id: str,
    request: Request,
    data: AdmissionDepositOnlinePaymentRequest,
    session: AsyncSession = Depends(get_session),
):
    """Starts a Paystack checkout for an admission deposit — the online
    counterpart to the admin-side manual `/deposits/{id}/payments` (cash,
    bank transfer). Applies via the same webhook pipeline as the
    application fee, tagged TransactionType.ADMISSION_DEPOSIT."""
    ip = _get_client_ip(request)
    await _check_rate_limit(ip, key_prefix="public_admissions_deposit_rl")

    school = await _get_active_school(session, school_code)
    # Locked FOR UPDATE so a second concurrent request for the same deposit
    # blocks here until this one commits -- combined with the in-flight-
    # checkout check below, this is what actually closes the race (the lock
    # alone wouldn't: this endpoint never writes to AdmissionDeposit itself,
    # so without that check a serialized second request would still see the
    # same balance and still be allowed to start its own Paystack charge).
    result = await session.execute(
        select(AdmissionDeposit).where(AdmissionDeposit.id == deposit_id, AdmissionDeposit.school_id == school.id).with_for_update()
    )
    deposit = result.scalar_one_or_none()
    if not deposit:
        raise HTTPException(status_code=404, detail="Deposit not found")
    applicant_result = await session.execute(select(Applicant).where(Applicant.id == deposit.applicant_id))
    applicant = applicant_result.scalar_one_or_none()
    if not applicant:
        raise HTTPException(status_code=404, detail="Deposit not found")

    if deposit.status in (AdmissionDepositStatus.PAID, AdmissionDepositStatus.WAIVED):
        raise HTTPException(status_code=409, detail="This deposit has already been settled")

    # Only deposit.status was previously checked, never the applicant's own
    # pipeline status — a guardian could still complete a Paystack charge
    # for a deposit belonging to an applicant who declined an offer or was
    # rejected, since the deposit itself might still be sitting PENDING.
    if applicant.status in (ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN):
        raise HTTPException(status_code=409, detail="This application is no longer active")

    # Two concurrent calls here (a double-click, a retried request) could
    # otherwise both pass the balance check below and both start a real
    # Paystack charge for the same money owed. If a checkout for this
    # deposit is already in flight (started in the last 10 minutes, not yet
    # resolved), hand back that same checkout instead of starting another.
    recent_window_start = datetime.utcnow() - timedelta(minutes=10)
    existing_txn_result = await session.execute(
        select(OnlineTransaction).where(
            OnlineTransaction.school_id == school.id,
            OnlineTransaction.fee_id == deposit.id,
            OnlineTransaction.transaction_type == TransactionType.ADMISSION_DEPOSIT,
            OnlineTransaction.status.in_([TransactionStatus.PENDING, TransactionStatus.PROCESSING]),
            OnlineTransaction.created_at >= recent_window_start,
        ).order_by(OnlineTransaction.created_at.desc())
    )
    existing_txn = existing_txn_result.scalars().first()
    if existing_txn and existing_txn.payment_url:
        return {
            "success": True,
            "payment_url": existing_txn.payment_url,
            "reference": existing_txn.reference,
            "amount": existing_txn.amount,
        }

    balance = deposit.required_amount - deposit.paid_amount
    payment_amount = data.amount if data.amount is not None else balance
    if payment_amount <= 0 or payment_amount > balance:
        raise HTTPException(status_code=422, detail=f"Payment amount must be between 0 and the outstanding balance of GHS {balance:.2f}")

    paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
    if not paystack_secret_key:
        raise HTTPException(status_code=503, detail="Online payment is not available right now. Please contact the school directly.")

    payment_service = OnlinePaymentService(paystack_secret_key)
    transaction_id = f"TXN-{uuid.uuid4().hex[:12].upper()}"

    # fee_id holds AdmissionDeposit.id, student_id/parent_id hold Applicant.id —
    # same reused-column trick as the application fee flow above.
    transaction = OnlineTransaction(
        school_id=school.id,
        fee_id=deposit.id,
        student_id=applicant.id,
        parent_id=applicant.id,
        amount=payment_amount,
        gateway="paystack",
        reference=transaction_id,
        transaction_type=TransactionType.ADMISSION_DEPOSIT,
        status=TransactionStatus.PENDING,
    )
    session.add(transaction)
    await session.flush()

    payer_email = applicant.guardian_email or f"applicant-{applicant.id}@campusio.online"
    paystack_result = await payment_service.paystack.initialize_payment(
        amount_kobo=int(payment_amount * 100),
        email=payer_email,
        reference=transaction_id,
        metadata={"deposit_id": deposit.id, "applicant_id": applicant.id, "admission_deposit": True},
    )

    if not paystack_result.get("success"):
        transaction.status = TransactionStatus.FAILED
        transaction.failed_reason = paystack_result.get("error", "Payment initialization failed")
        session.add(transaction)
        await session.commit()
        raise HTTPException(status_code=502, detail="Could not start payment. Please try again shortly.")

    transaction.payment_url = paystack_result["authorization_url"]
    transaction.access_code = paystack_result["access_code"]
    transaction.reference = paystack_result["reference"]
    transaction.status = TransactionStatus.PROCESSING
    session.add(transaction)
    await session.commit()

    return {
        "success": True,
        "payment_url": paystack_result["authorization_url"],
        "reference": paystack_result["reference"],
        "amount": payment_amount,
    }


@router.get("/admissions/{school_code}/deposits/{deposit_id}/payment/status/{reference}", response_model=dict)
async def check_public_deposit_payment_status(
    school_code: str,
    deposit_id: str,
    reference: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Polled by the deposit-payment page after the Paystack checkout window closes."""
    ip = _get_client_ip(request)
    await _check_rate_limit(ip, key_prefix="public_admissions_status_rl", max_per_hour=STATUS_POLL_RATE_LIMIT_MAX_PER_HOUR)

    school = await _get_active_school(session, school_code)
    transaction = await _poll_transaction(session, school, reference, TransactionType.ADMISSION_DEPOSIT)
    if transaction.status == TransactionStatus.SUCCESS:
        deposit_result = await session.execute(select(AdmissionDeposit).where(AdmissionDeposit.id == deposit_id))
        deposit = deposit_result.scalar_one_or_none()
        return {
            "status": "success",
            "balance": (deposit.required_amount - deposit.paid_amount) if deposit else None,
            "deposit_status": deposit.status.value if deposit else None,
        }
    if transaction.status == TransactionStatus.FAILED:
        return {"status": "failed", "error": transaction.failed_reason}
    return {"status": "pending"}


@router.post("/admissions/{school_code}/apply/{applicant_id}/documents", response_model=dict)
async def upload_applicant_documents(
    school_code: str,
    applicant_id: str,
    request: Request,
    document_types: List[str] = Form(...),
    files: List[UploadFile] = File(...),
    session: AsyncSession = Depends(get_session),
):
    """Attaches supporting documents (birth certificate, previous report
    card, photo, ...) to an application already created via /apply. Kept as
    a separate step so a failed/retried upload never risks the application
    record itself."""
    ip = _get_client_ip(request)
    await _check_rate_limit(ip, key_prefix="public_admissions_upload_rl", max_per_hour=UPLOAD_RATE_LIMIT_MAX_PER_HOUR)

    school = await _get_active_school(session, school_code)

    result = await session.execute(
        select(Applicant).where(and_(Applicant.id == applicant_id, Applicant.school_id == school.id))
    )
    applicant = result.scalar_one_or_none()
    if not applicant:
        raise HTTPException(status_code=404, detail="Applicant not found")
    if applicant.converted_student_id:
        raise HTTPException(status_code=409, detail="This application has already been processed")

    if len(files) != len(document_types):
        raise HTTPException(status_code=400, detail="Each file must have a matching document type")
    if len(files) > MAX_DOCUMENTS_PER_APPLICANT:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_DOCUMENTS_PER_APPLICANT} files per application")

    existing_count_result = await session.execute(
        select(func.count()).select_from(ApplicantDocument).where(ApplicantDocument.applicant_id == applicant_id)
    )
    existing_count = existing_count_result.scalar() or 0
    if existing_count + len(files) > MAX_DOCUMENTS_PER_APPLICANT:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_DOCUMENTS_PER_APPLICANT} files per application")

    valid_types = {t.value for t in ApplicantDocumentType}
    saved = []
    for file, doc_type in zip(files, document_types):
        if file.content_type not in ALLOWED_DOCUMENT_CONTENT_TYPES:
            raise HTTPException(status_code=400, detail=f"Unsupported file type for {file.filename}: {file.content_type}. Allowed: PDF, JPEG, PNG.")
        content = await file.read()
        if len(content) > MAX_DOCUMENT_SIZE_BYTES:
            raise HTTPException(status_code=400, detail=f"{file.filename} exceeds the 5 MB limit.")

        safe_doc_type = doc_type if doc_type in valid_types else ApplicantDocumentType.OTHER.value

        filename = f"{uuid.uuid4()}_{Path(file.filename or 'document').name}"
        target_dir = UPLOAD_DIR / school.id / applicant_id
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / filename).write_bytes(content)

        doc = ApplicantDocument(
            applicant_id=applicant_id,
            school_id=school.id,
            document_type=safe_doc_type,
            file_path=f"/uploads/admissions/{school.id}/{applicant_id}/{filename}",
            original_filename=file.filename or "document",
            content_type=file.content_type,
            file_size=len(content),
        )
        session.add(doc)
        saved.append(doc)

    await session.commit()
    return {"success": True, "uploaded": len(saved)}
