"""Gapless, sequential receipt numbering — see models.fee.ReceiptSequence.

Previously every fee-payment receipt number was random
("RCP-20260909-A1B2C3D4"), which reads as sequential but proves nothing: it
can't demonstrate that no receipt was skipped, voided-and-hidden, or issued
out of order — the basic property an auditor expects from a numbered
receipt register. This replaces that with a real per-school, per-year
counter, backed by a DB-level unique constraint on (school_id, year) — see
the migration that adds models.fee.ReceiptSequence.
"""
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.fee import ReceiptSequence

logger = logging.getLogger(__name__)


async def get_next_receipt_number(session: AsyncSession, school_id: str, as_of: Optional[datetime] = None) -> str:
    """Atomically claims the next number in this school's receipt register
    for the given year (defaults to now) and returns "RCP-{year}-{number:06d}".

    Uses SELECT ... FOR UPDATE to serialize concurrent claims against the
    same (school_id, year) row. The one race this can't lock away is two
    requests both finding no row yet for a brand-new year and both trying to
    insert the first one — handled with a SAVEPOINT (begin_nested) around
    just that insert, so the loser's IntegrityError only unwinds its own
    failed insert rather than the caller's whole in-progress transaction
    (the payment/fee rows it has already added to this same session).
    """
    year = (as_of or datetime.utcnow()).year

    result = await session.execute(
        select(ReceiptSequence)
        .where(ReceiptSequence.school_id == school_id, ReceiptSequence.year == year)
        .with_for_update()
    )
    seq = result.scalar_one_or_none()
    if seq is not None:
        seq.last_number += 1
        session.add(seq)
        await session.flush()
        return f"RCP-{year}-{seq.last_number:06d}"

    try:
        async with session.begin_nested():
            seq = ReceiptSequence(school_id=school_id, year=year, last_number=1)
            session.add(seq)
            await session.flush()
        return f"RCP-{year}-{seq.last_number:06d}"
    except IntegrityError:
        logger.info(f"Receipt sequence row for school {school_id}/{year} created concurrently, retrying")
        result = await session.execute(
            select(ReceiptSequence)
            .where(ReceiptSequence.school_id == school_id, ReceiptSequence.year == year)
            .with_for_update()
        )
        seq = result.scalar_one()
        seq.last_number += 1
        session.add(seq)
        await session.flush()
        return f"RCP-{year}-{seq.last_number:06d}"
