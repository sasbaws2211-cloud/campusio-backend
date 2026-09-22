"""Statutory (Ghana SSNIT) contribution export

Every figure in this export is a REAL number actually calculated and
posted to the GL for the run: the employee-side Tier-1 contribution
(PayrollLineItem.nssf_amount, computed on basic salary — see
PayrollService.calculate_nssf), the employer-side Tier-1 share
(PayrollLineItem.employer_nssf_amount, GL 2112), and the mandatory
Tier-2 occupational pension (PayrollLineItem.nssf_tier2_amount, GL 2113,
remitted to a separate private trustee, not SSNIT itself). Previously the
employer-side figure here was never tracked anywhere in the codebase and
was computed on the fly purely for this export, explicitly labeled as an
estimate rather than a real recorded liability — that gap is closed now
that payroll actually posts these to the GL at run-post time.
"""
import csv
import io
from datetime import datetime
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.payroll import PayrollRun, PayrollLineItem
from models.staff import Staff
from models.school import School


async def generate_ssnit_csv(session: AsyncSession, school_id: str, payroll_run_id: str) -> Optional[str]:
    """Build a Ghana SSNIT contribution schedule CSV for a payroll run.
    Returns None if the run doesn't exist for this school."""
    run_result = await session.execute(
        select(PayrollRun).where(
            PayrollRun.id == payroll_run_id, PayrollRun.school_id == school_id
        )
    )
    run = run_result.scalar_one_or_none()
    if not run:
        return None

    school_result = await session.execute(select(School).where(School.id == school_id))
    school = school_result.scalar_one_or_none()

    lines_result = await session.execute(
        select(PayrollLineItem).where(PayrollLineItem.payroll_run_id == payroll_run_id)
    )
    line_items = lines_result.scalars().all()

    staff_ids = [li.staff_id for li in line_items]
    staff_map = {}
    if staff_ids:
        staff_result = await session.execute(select(Staff).where(Staff.id.in_(staff_ids)))
        for s in staff_result.scalars().all():
            staff_map[s.id] = s

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(["School", school.name if school else ""])
    writer.writerow(["SSNIT Employer Number", (school.ssnit_employer_number if school else "") or "Not set"])
    writer.writerow(["Period", run.period_name])
    writer.writerow(["Generated", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")])
    writer.writerow([])

    writer.writerow([
        "SSNIT Number", "Staff Name", "Staff ID", "Basic Salary",
        "Employee Tier-1", "Employer Tier-1", "Tier-2 (private trustee)",
        "Total SSNIT (Tier-1)", "Total Incl. Tier-2",
    ])

    total_employee = 0.0
    total_employer = 0.0
    total_tier2 = 0.0
    for li in line_items:
        staff = staff_map.get(li.staff_id)
        basic_salary = float(li.basic_salary or 0.0)
        employee_contribution = float(li.nssf_amount or 0.0)
        employer_contribution = float(li.employer_nssf_amount or 0.0)
        tier2_contribution = float(li.nssf_tier2_amount or 0.0)
        tier1_total = round(employee_contribution + employer_contribution, 2)
        grand_total = round(tier1_total + tier2_contribution, 2)

        total_employee += employee_contribution
        total_employer += employer_contribution
        total_tier2 += tier2_contribution

        writer.writerow([
            (staff.ssnit_number if staff else "") or "Not set",
            f"{staff.first_name} {staff.last_name}" if staff else "Unknown",
            staff.staff_id if staff else li.staff_id,
            f"{basic_salary:.2f}",
            f"{employee_contribution:.2f}",
            f"{employer_contribution:.2f}",
            f"{tier2_contribution:.2f}",
            f"{tier1_total:.2f}",
            f"{grand_total:.2f}",
        ])

    writer.writerow([])
    writer.writerow([
        "TOTAL", "", "", "",
        f"{total_employee:.2f}", f"{total_employer:.2f}", f"{total_tier2:.2f}",
        f"{total_employee + total_employer:.2f}", f"{total_employee + total_employer + total_tier2:.2f}",
    ])
    writer.writerow([])
    writer.writerow([
        "Note: every figure above is the actual amount calculated for this run and posted "
        "to the general ledger (GL 2110 employee Tier-1, GL 2112 employer Tier-1, GL 2113 "
        "Tier-2) — none of these are on-the-fly estimates. Tier-2 is a mandatory "
        "occupational pension remitted to a licensed private trustee, separate from SSNIT "
        "itself; confirm this school's actual trustee remittance details before filing."
    ])

    return buffer.getvalue()


async def generate_bank_payment_csv(session: AsyncSession, school_id: str, payroll_run_id: str) -> Optional[dict]:
    """Build a generic bulk-payment CSV for a payroll run — the alternative
    to paying via the live Paystack transfer flow this codebase already has
    (PayrollLineItem.payment_status/paid_at/transfer_reference): a finance
    officer takes this file to any bank's own bulk-upload portal instead.
    Column layout is deliberately generic (account number/name/bank code,
    not one bank's proprietary fixed-width format) since Ghanaian banks and
    GhIPSS-based aggregators commonly accept CSV for bulk salary payments —
    treat this as a starting template to adjust per the receiving bank's
    exact required columns, not a guaranteed drop-in file.

    Returns {"csv": str, "included": int, "skipped_no_payout_details": int}
    or None if the run doesn't exist for this school."""
    run_result = await session.execute(
        select(PayrollRun).where(PayrollRun.id == payroll_run_id, PayrollRun.school_id == school_id)
    )
    run = run_result.scalar_one_or_none()
    if not run:
        return None

    school_result = await session.execute(select(School).where(School.id == school_id))
    school = school_result.scalar_one_or_none()

    lines_result = await session.execute(
        select(PayrollLineItem).where(PayrollLineItem.payroll_run_id == payroll_run_id)
    )
    line_items = lines_result.scalars().all()

    staff_ids = [li.staff_id for li in line_items]
    staff_map = {}
    if staff_ids:
        staff_result = await session.execute(select(Staff).where(Staff.id.in_(staff_ids)))
        for s in staff_result.scalars().all():
            staff_map[s.id] = s

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(["School", school.name if school else ""])
    writer.writerow(["Period", run.period_name])
    writer.writerow(["Generated", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")])
    writer.writerow([])
    writer.writerow(["Staff ID", "Staff Name", "Bank/Mobile Money Code", "Account Number", "Account Name", "Amount (GHS)", "Reference"])

    included = 0
    skipped = 0
    for li in line_items:
        staff = staff_map.get(li.staff_id)
        if not staff or not staff.payout_bank_code or not staff.payout_account_number:
            skipped += 1
            continue
        writer.writerow([
            staff.staff_id, f"{staff.first_name} {staff.last_name}",
            staff.payout_bank_code, staff.payout_account_number,
            staff.payout_account_name or f"{staff.first_name} {staff.last_name}",
            f"{float(li.net_amount or 0.0):.2f}",
            f"{run.period_name.replace(' ', '_')}-{staff.staff_id}",
        ])
        included += 1

    if skipped:
        writer.writerow([])
        writer.writerow([f"Note: {skipped} staff member(s) skipped — no bank/mobile money payout details on file."])

    return {"csv": buffer.getvalue(), "included": included, "skipped_no_payout_details": skipped}


async def generate_paye_csv(session: AsyncSession, school_id: str, payroll_run_id: str) -> Optional[str]:
    """Build a PAYE (income tax) withholding schedule CSV for a payroll
    run — previously the only place PAYE figures existed anywhere was
    inside each PayrollLineItem.tax_amount / the GL 2130 account, with no
    dedicated remittance report a finance officer could hand to the Ghana
    Revenue Authority (or attach to a GRA online-filing submission).
    Returns None if the run doesn't exist for this school."""
    run_result = await session.execute(
        select(PayrollRun).where(PayrollRun.id == payroll_run_id, PayrollRun.school_id == school_id)
    )
    run = run_result.scalar_one_or_none()
    if not run:
        return None

    school_result = await session.execute(select(School).where(School.id == school_id))
    school = school_result.scalar_one_or_none()

    lines_result = await session.execute(
        select(PayrollLineItem).where(PayrollLineItem.payroll_run_id == payroll_run_id)
    )
    line_items = lines_result.scalars().all()

    staff_ids = [li.staff_id for li in line_items]
    staff_map = {}
    if staff_ids:
        staff_result = await session.execute(select(Staff).where(Staff.id.in_(staff_ids)))
        for s in staff_result.scalars().all():
            staff_map[s.id] = s

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(["School", school.name if school else ""])
    writer.writerow(["Period", run.period_name])
    writer.writerow(["Generated", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")])
    writer.writerow([])
    writer.writerow(["Staff ID", "Staff Name", "Gross Pay", "Taxable Income (Gross)", "PAYE Withheld"])

    total_gross = 0.0
    total_paye = 0.0
    for li in line_items:
        staff = staff_map.get(li.staff_id)
        gross = float(li.gross_amount or 0.0)
        paye = float(li.tax_amount or 0.0)
        total_gross += gross
        total_paye += paye
        writer.writerow([
            staff.staff_id if staff else li.staff_id,
            f"{staff.first_name} {staff.last_name}" if staff else "Unknown",
            f"{gross:.2f}", f"{gross:.2f}", f"{paye:.2f}",
        ])

    writer.writerow([])
    writer.writerow(["TOTAL", "", f"{total_gross:.2f}", f"{total_gross:.2f}", f"{total_paye:.2f}"])
    writer.writerow([])
    writer.writerow([
        "Note: PAYE Withheld is the actual amount calculated for this run (flat rate or "
        "bracket-based, per each staff member's PayrollContract.tax_calculation_mode) and "
        "posted to GL 2130 (Income Tax Withheld Payable) — this is the input data for a GRA "
        "remittance filing, not a specific government form layout."
    ])

    return buffer.getvalue()
