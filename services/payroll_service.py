"""Payroll Service for salary calculation and payroll run generation"""
import calendar
import json
import logging
from datetime import datetime, date
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlmodel import select, and_
from decimal import Decimal

from models.payroll import (
    PayrollContract, PayrollRun, PayrollRunCreate, PayrollLineItem,
    PayrollAdjustment, PayrollStatus, PayrollCategory, PaySchedule, PayeBracket
)
from models.staff import Staff, StaffStatus, PayoutVerificationStatus
from models.user import User
from models.finance import (
    JournalEntryCreate, JournalLineItemCreate, ReferenceType
)
from models.finance.gl_audit_log import AuditActionType, AuditEntityType
from services.deduction_rules_service import RulesEvaluationService
from services.sms_service import sms_service
from services.staff_loan_service import StaffLoanService
from services.leave_encashment_service import LeaveEncashmentService
from services.gl_audit_log_service import GLAuditLogService
from services.journal_entry_service import JournalEntryService, JournalEntryError

logger = logging.getLogger(__name__)


class PayrollCalculationError(Exception):
    """Raised when payroll calculation encounters an error"""
    pass


class PayrollService:
    """Service for payroll calculations and run generation"""
    
    def __init__(self, session: AsyncSession):
        self.session = session
        self.audit_service = GLAuditLogService(session)

    async def _log_payroll_audit(
        self, school_id: str, payroll_run_id: str, action: AuditActionType,
        user_id: str, user_name: str = "Unknown", user_role: str = "finance",
        new_values: Optional[dict] = None,
    ) -> None:
        """Best-effort audit log for a payroll-run lifecycle event — never
        raises, matching every other GL-audit call site in this codebase
        (a logging failure must not block the actual payroll action)."""
        try:
            await self.audit_service.log_action(
                school_id=school_id, entity_type=AuditEntityType.PAYROLL_RUN, entity_id=payroll_run_id,
                action=action, user_id=user_id, user_name=user_name, user_role=user_role,
                new_values=new_values or {},
            )
        except Exception as e:
            logger.warning(f"Failed to write GL audit log for payroll run {payroll_run_id}: {e}")

    # ==================== Helper Methods ====================
    
    async def get_active_contract(self, school_id: str, staff_id: str) -> Optional[PayrollContract]:
        """
        Get the active payroll contract for a staff member on the current date.
        
        Args:
            school_id: School identifier
            staff_id: Staff member identifier
            
        Returns:
            PayrollContract if found and active, None otherwise
        """
        try:
            result = await self.session.execute(
                select(PayrollContract).where(
                    PayrollContract.school_id == school_id,
                    PayrollContract.staff_id == staff_id,
                    PayrollContract.is_active == True,
                    PayrollContract.effective_from <= datetime.utcnow(),
                    (PayrollContract.effective_to.is_(None) | 
                     (PayrollContract.effective_to >= datetime.utcnow()))
                ).order_by(PayrollContract.effective_from.desc())
            )
            return result.scalars().first()
        except Exception as e:
            logger.error(f"Error fetching active contract for staff {staff_id}: {str(e)}")
            return None
    
    async def get_staff_by_id(self, staff_id: str, school_id: str) -> Optional[Staff]:
        """Get staff member by ID and school"""
        try:
            result = await self.session.execute(
                select(Staff).where(
                    Staff.id == staff_id,
                    Staff.school_id == school_id
                )
            )
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error fetching staff {staff_id}: {str(e)}")
            return None
    
    # ==================== Calculation Methods ====================
    
    def calculate_allowances(self, contract: PayrollContract) -> Tuple[float, Dict[str, float]]:
        """
        Calculate total allowances from contract.

        Args:
            contract: PayrollContract instance

        Returns:
            Tuple of (total_allowances, breakdown_dict)
        """
        breakdown = {
            "housing": float(contract.allowance_housing),
            "transport": float(contract.allowance_transport),
            "meals": float(contract.allowance_meals),
            "utilities": float(contract.allowance_utilities),
            "other": float(contract.allowance_other),
        }

        # Arbitrary extra allowance line items beyond the 5 fixed categories
        # above — see PayrollContract.extra_allowances. Each item folds into
        # the total by its own name (e.g. "responsibility_allowance": 150.0)
        # so the payslip breakdown shows it individually, not lumped into
        # "other".
        if contract.extra_allowances:
            try:
                extras = json.loads(contract.extra_allowances)
                for item in extras:
                    name = str(item.get("name", "extra")).strip() or "extra"
                    amount = float(item.get("amount", 0.0))
                    breakdown[name] = breakdown.get(name, 0.0) + amount
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                logger.warning(f"Could not parse extra_allowances for contract {contract.id}: {e}")

        total = sum(breakdown.values())
        return total, breakdown
    
    def calculate_gross_amount(self, contract: PayrollContract) -> Tuple[float, Dict[str, float]]:
        """
        Calculate gross amount (basic + allowances).
        
        Args:
            contract: PayrollContract instance
            
        Returns:
            Tuple of (gross_amount, components_dict)
        """
        basic_salary = float(contract.basic_salary)
        total_allowances, allowance_breakdown = self.calculate_allowances(contract)
        
        gross = basic_salary + total_allowances
        
        components = {
            "basic_salary": basic_salary,
            "total_allowances": total_allowances,
            "allowances_breakdown": allowance_breakdown,
            "gross_amount": gross
        }
        
        return gross, components
    
    def calculate_tax(self, gross_amount: float, tax_rate_percent: float) -> float:
        """
        Calculate income tax based on gross amount and tax rate.

        Args:
            gross_amount: Total gross salary
            tax_rate_percent: Tax rate as percentage (e.g., 15.0 for 15%)

        Returns:
            Tax amount
        """
        if tax_rate_percent < 0 or tax_rate_percent > 100:
            raise PayrollCalculationError(f"Invalid tax rate: {tax_rate_percent}")

        return (gross_amount * tax_rate_percent) / 100.0

    @staticmethod
    def default_paye_bracket_seed() -> List[Dict[str, Any]]:
        """Illustrative starting point for Ghana's monthly PAYE bands —
        loosely following the GRA's published 2023-era monthly schedule.
        A school MUST confirm current figures against the Ghana Revenue
        Authority's actual published schedule before relying on this for
        real payroll; this is a seed to edit, not a maintained statutory
        table, the same "starting point, not gospel" honesty already used
        by RulePresetService.get_preset_rules() for deduction-rule presets
        elsewhere in this codebase."""
        return [
            {"lower_bound": 0, "upper_bound": 490, "rate_percent": 0, "sort_order": 1},
            {"lower_bound": 490, "upper_bound": 600, "rate_percent": 5, "sort_order": 2},
            {"lower_bound": 600, "upper_bound": 730, "rate_percent": 10, "sort_order": 3},
            {"lower_bound": 730, "upper_bound": 3896.67, "rate_percent": 17.5, "sort_order": 4},
            {"lower_bound": 3896.67, "upper_bound": 19896.67, "rate_percent": 25, "sort_order": 5},
            {"lower_bound": 19896.67, "upper_bound": None, "rate_percent": 30, "sort_order": 6},
        ]

    def calculate_paye_from_brackets(self, taxable_income: float, brackets: List[PayeBracket]) -> float:
        """Progressive PAYE — each bracket's rate applies only to the slice
        of income that actually falls within it, not the whole amount
        (that's what makes it progressive rather than a single flat rate,
        which is what tax_rate_percent above has always been — factually
        wrong for a real income tax system where marginal rates rise with
        income). Used only for a PayrollContract with
        tax_calculation_mode="bracket"; every contract left on "flat"
        keeps using calculate_tax above exactly as before."""
        if not brackets:
            return 0.0
        ordered = sorted(brackets, key=lambda b: b.lower_bound)
        tax = 0.0
        for b in ordered:
            if taxable_income <= b.lower_bound:
                break
            band_top = b.upper_bound if b.upper_bound is not None else taxable_income
            taxable_in_band = max(0.0, min(taxable_income, band_top) - b.lower_bound)
            tax += taxable_in_band * (b.rate_percent / 100.0)
        return round(tax, 2)
    
    def calculate_pension(self, gross_amount: float, pension_rate_percent: float) -> float:
        """
        Calculate pension contribution.
        
        Args:
            gross_amount: Total gross salary
            pension_rate_percent: Pension rate as percentage
            
        Returns:
            Pension amount
        """
        if pension_rate_percent < 0 or pension_rate_percent > 100:
            raise PayrollCalculationError(f"Invalid pension rate: {pension_rate_percent}")
        
        return (gross_amount * pension_rate_percent) / 100.0
    
    def calculate_nssf(self, basic_salary: float, nssf_rate_percent: float) -> float:
        """
        Calculate the employee-side SSNIT/NSSF contribution.

        Statutorily computed on BASIC salary, not gross — a contract's
        allowances (housing, transport, etc.) don't attract SSNIT
        contributions. Previously this was computed against gross_amount,
        overstating the deduction for any contract carrying allowances.

        Args:
            basic_salary: Contract's basic salary (not gross)
            nssf_rate_percent: NSSF rate as percentage

        Returns:
            NSSF amount
        """
        if nssf_rate_percent < 0 or nssf_rate_percent > 100:
            raise PayrollCalculationError(f"Invalid NSSF rate: {nssf_rate_percent}")

        return (basic_salary * nssf_rate_percent) / 100.0

    def calculate_employer_nssf(self, basic_salary: float, contract: PayrollContract) -> Tuple[float, float]:
        """Employer-side statutory contributions — a real cost to the
        school, never deducted from the employee's own pay. Returns
        (employer_ssnit_tier1_amount, ssnit_tier2_amount), both on basic
        salary. Previously neither was tracked or GL-posted anywhere;
        services/statutory_export_service.py computed an on-the-fly
        estimate purely for a CSV export."""
        tier1 = round(basic_salary * contract.employer_nssf_rate_percent / 100.0, 2)
        tier2 = round(basic_salary * contract.nssf_tier2_rate_percent / 100.0, 2)
        return tier1, tier2

    def calculate_deductions(
        self,
        gross_amount: float,
        contract: PayrollContract,
        paye_brackets: Optional[List[PayeBracket]] = None,
    ) -> Tuple[float, Dict[str, float]]:
        """
        Calculate total deductions (tax, pension, NSSF, other) taken from
        the employee's own pay. Employer-side contributions are computed
        separately (calculate_employer_nssf) since they never reduce
        net_amount.

        Args:
            gross_amount: Total gross salary
            contract: PayrollContract with deduction rates
            paye_brackets: this school's PayeBracket rows, only consulted
                when contract.tax_calculation_mode == "bracket" — every
                contract left on the default "flat" mode ignores this
                entirely and keeps its exact prior behavior.

        Returns:
            Tuple of (total_deductions, breakdown_dict)
        """
        basic_salary = float(contract.basic_salary)
        if contract.tax_calculation_mode == "bracket" and paye_brackets:
            tax_amount = self.calculate_paye_from_brackets(gross_amount, paye_brackets)
        else:
            tax_amount = self.calculate_tax(gross_amount, contract.tax_rate_percent)
        pension_amount = self.calculate_pension(gross_amount, contract.pension_rate_percent)
        nssf_amount = self.calculate_nssf(basic_salary, contract.nssf_rate_percent)
        other_deductions = float(contract.other_deduction)

        extra_deductions_breakdown = {}
        if contract.extra_deductions:
            try:
                extras = json.loads(contract.extra_deductions)
                for item in extras:
                    name = str(item.get("name", "extra")).strip() or "extra"
                    amount = float(item.get("amount", 0.0))
                    extra_deductions_breakdown[name] = extra_deductions_breakdown.get(name, 0.0) + amount
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                logger.warning(f"Could not parse extra_deductions for contract {contract.id}: {e}")
        extra_deductions_total = sum(extra_deductions_breakdown.values())

        total = tax_amount + pension_amount + nssf_amount + other_deductions + extra_deductions_total

        breakdown = {
            "tax": tax_amount,
            "pension": pension_amount,
            "nssf": nssf_amount,
            "other": other_deductions,
        }
        if extra_deductions_breakdown:
            breakdown["extra"] = extra_deductions_breakdown

        return total, breakdown
    
    def calculate_net_amount(self, gross_amount: float, total_deductions: float) -> float:
        """
        Calculate net amount (gross - deductions).
        
        Args:
            gross_amount: Total gross salary
            total_deductions: Total deductions
            
        Returns:
            Net amount
        """
        net = gross_amount - total_deductions
        return max(net, 0.0)  # Ensure net is never negative
    
    def calculate_payroll_for_staff(
        self,
        contract: PayrollContract,
        adjustments: List[Dict[str, Any]] = None,
        paye_brackets: Optional[List[PayeBracket]] = None,
    ) -> Dict[str, Any]:
        """
        Calculate complete payroll for a staff member.

        Args:
            contract: PayrollContract for the staff
            adjustments: Optional list of adjustments (bonus/penalty)
            paye_brackets: this school's PayeBracket rows — see calculate_deductions

        Returns:
            Dictionary with calculated payroll details
        """
        # Calculate components
        basic_salary = float(contract.basic_salary)
        total_allowances, allowance_breakdown = self.calculate_allowances(contract)
        gross_amount, gross_components = self.calculate_gross_amount(contract)
        total_deductions, deduction_breakdown = self.calculate_deductions(gross_amount, contract, paye_brackets)
        net_amount = self.calculate_net_amount(gross_amount, total_deductions)
        employer_nssf_amount, nssf_tier2_amount = self.calculate_employer_nssf(basic_salary, contract)

        # Apply adjustments if any
        total_adjustments = 0.0
        adjustment_details = {}
        if adjustments:
            for adj in adjustments:
                amount = float(adj.get("amount", 0.0))
                adj_type = adj.get("adjustment_type", "other")
                total_adjustments += amount
                adjustment_details[adj_type] = adjustment_details.get(adj_type, 0.0) + amount
        
        # Final net (after adjustments)
        final_net = net_amount + total_adjustments
        
        return {
            "basic_salary": basic_salary,
            "total_allowances": total_allowances,
            "allowance_breakdown": allowance_breakdown,
            "gross_amount": gross_amount,
            "tax_amount": deduction_breakdown["tax"],
            "pension_amount": deduction_breakdown["pension"],
            "nssf_amount": deduction_breakdown["nssf"],
            "other_deductions": deduction_breakdown["other"],
            "total_deductions": total_deductions,
            "net_amount": net_amount,
            "employer_nssf_amount": employer_nssf_amount,
            "nssf_tier2_amount": nssf_tier2_amount,
            "adjustments": adjustment_details,
            "total_adjustments": total_adjustments,
            "final_net": final_net,
            "breakdown": {
                "allowances": allowance_breakdown,
                "deductions": deduction_breakdown,
                "adjustments": adjustment_details
            }
        }
    
    # ==================== Payroll Run Generation ====================
    
    async def get_active_staff_for_school(
        self,
        school_id: str,
        campus_id: Optional[str] = None,
    ) -> List[Staff]:
        """
        Get all active staff members for a school, optionally scoped to one
        campus — previously payroll had zero campus-scoping anywhere (every
        other module using resolve_campus_scope let a campus-scoped admin
        act on just their own campus; payroll always ran for the whole
        school regardless of the caller's campus assignment).

        Args:
            school_id: School identifier
            campus_id: When given, only staff assigned to this campus

        Returns:
            List of active Staff members
        """
        try:
            query = select(Staff).where(
                Staff.school_id == school_id,
                Staff.status == StaffStatus.ACTIVE
            )
            if campus_id:
                query = query.where(Staff.campus_id == campus_id)
            result = await self.session.execute(query.order_by(Staff.first_name))
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error fetching active staff for school {school_id}: {str(e)}")
            return []

    async def get_recently_exited_staff_for_period(self, school_id: str, year: int, month: int) -> Dict[str, str]:
        """staff_id -> last_working_date for any StaffExit whose
        last_working_date falls inside this payroll period. Previously a
        staff member who exited mid-period simply vanished from payroll
        the moment their Staff.status flipped off ACTIVE (see
        routers/hr_admin.py::_finalize_staff_exit) — no final paycheck was
        ever generated for the days they actually worked that month.
        Called by generate_payroll_run to add these staff back into the
        run (get_active_contract works for them regardless of current
        status, since a contract's own effective_to window is independent
        of Staff.status); calculate_payroll_for_staff's caller then
        pro-rates their pay down to last_working_date."""
        from models.hr_admin import StaffExit

        period_prefix = f"{year:04d}-{month:02d}"
        result = await self.session.execute(
            select(StaffExit).where(
                StaffExit.school_id == school_id,
                StaffExit.last_working_date.like(f"{period_prefix}%"),
            )
        )
        return {e.staff_id: e.last_working_date for e in result.scalars().all()}

    def calculate_proration_fraction(
        self,
        year: int,
        month: int,
        date_joined: Optional[str] = None,
        last_working_date: Optional[str] = None,
    ) -> float:
        """Fraction (0.0-1.0] of this calendar-month payroll period actually
        covered by a staff member's employment — 1.0 (no proration) unless
        they joined or exited mid-period. Previously the ONLY proration
        anywhere in payroll was for approved unpaid leave days; a brand-new
        hire starting on the 28th, or someone whose last working day was
        the 5th, was still paid a FULL month's salary either way."""
        days_in_period = calendar.monthrange(year, month)[1]
        period_start = date(year, month, 1)
        period_end = date(year, month, days_in_period)

        effective_start = period_start
        if date_joined:
            try:
                joined = date.fromisoformat(str(date_joined)[:10])
                if period_start <= joined <= period_end:
                    effective_start = joined
            except ValueError:
                pass

        effective_end = period_end
        if last_working_date:
            try:
                exited = date.fromisoformat(str(last_working_date)[:10])
                if period_start <= exited <= period_end:
                    effective_end = exited
            except ValueError:
                pass

        if effective_start == period_start and effective_end == period_end:
            return 1.0

        covered_days = (effective_end - effective_start).days + 1
        if covered_days <= 0:
            return 0.0
        return round(covered_days / days_in_period, 4)
    
    async def get_existing_payroll_run(
        self,
        school_id: str,
        year: int,
        month: int,
        pay_schedule: "PaySchedule" = None,
        campus_id: Optional[str] = None,
    ) -> Optional[PayrollRun]:
        """
        Check if a payroll run already exists for a period + schedule (+
        campus, for a school running separate per-campus payroll). A
        school can have a separate monthly run and weekly/biweekly run
        covering different staff in the same calendar month, so the
        schedule is part of what makes a period unique, not just year/month.

        Args:
            school_id: School identifier
            year: Payroll year
            month: Payroll month
            pay_schedule: Which schedule this run covers (defaults to MONTHLY)
            campus_id: None = school-wide run; set = only that campus's run

        Returns:
            PayrollRun if exists, None otherwise
        """
        try:
            # A REJECTED run doesn't count as "existing" for this check: it
            # was sent back to the admin as unusable, and nothing in this
            # service ever revives it (no path takes it from REJECTED to
            # GENERATED/APPROVED). Excluding it here — matched by the
            # partial uq_payroll_run_school_period index, which no longer
            # constrains REJECTED rows either — lets the school regenerate
            # a fresh run for the same period+schedule instead of being
            # permanently blocked by the rejected one.
            query = select(PayrollRun).where(
                PayrollRun.school_id == school_id,
                PayrollRun.period_year == year,
                PayrollRun.period_month == month,
                PayrollRun.campus_id == campus_id,
                PayrollRun.status != PayrollStatus.REJECTED,
            )
            if pay_schedule is not None:
                query = query.where(PayrollRun.pay_schedule == pay_schedule)
            result = await self.session.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error checking existing payroll run: {str(e)}")
            return None
    
    async def get_staff_attendance_counts(
        self, school_id: str, staff_id: str, year: int, month: int
    ) -> Tuple[int, int]:
        """Real absent/present day counts for a staff member in a payroll
        period, from actual StaffAttendance records — populated by HR/admin
        roster marking, bulk entry, self clock-in/out, and auto-absent
        marking (routers/attendance.py).

        absent_days is strictly ABSENT by construction, so LATE and EXCUSED
        (including days from an approved LeaveRequest) never factor into
        it — excused leave correctly never triggers the "Absence Penalty"
        deduction rule.

        present_days is PRESENT+LATE — a late arrival is still physical
        presence, matching every other "days present" figure in this
        codebase (routers/attendance.py's own self-service attendance_percentage,
        the attendance-risk sweep, report cards, portals). Previously this
        counted PRESENT only, so a custom DeductionRule.expression written
        against present_days (e.g. "present_days < 20") would silently
        disagree with what HR's own attendance view calls "days present"
        for the identical StaffAttendance rows — the same bug already fixed
        across the rest of the app this session.
        """
        from models.attendance import StaffAttendance, AttendanceStatus

        period_prefix = f"{year:04d}-{month:02d}"
        result = await self.session.execute(
            select(StaffAttendance).where(
                StaffAttendance.school_id == school_id,
                StaffAttendance.staff_id == staff_id,
                StaffAttendance.attendance_date.like(f"{period_prefix}%"),
            )
        )
        records = result.scalars().all()
        absent_days = sum(1 for r in records if r.status == AttendanceStatus.ABSENT)
        present_days = sum(1 for r in records if r.status in (AttendanceStatus.PRESENT, AttendanceStatus.LATE))
        return absent_days, present_days

    async def get_unpaid_leave_days(self, school_id: str, staff_id: str, year: int, month: int) -> int:
        """Days in this payroll period covered by an approved LeaveType.UNPAID
        request — these ARE EXCUSED StaffAttendance rows (so they correctly
        never count as an unexplained absence), but "unpaid" has to mean
        something to pay itself: previously nothing anywhere read
        LeaveType.UNPAID when computing a payslip, so a staff member on
        approved unpaid leave was paid in full regardless. See
        generate_payroll_run, which turns this into a pro-rated gross
        reduction the same way it applies any other deduction."""
        from models.attendance import StaffAttendance, AttendanceStatus
        from models.leave_request import LeaveRequest, LeaveType

        period_prefix = f"{year:04d}-{month:02d}"
        result = await self.session.execute(
            select(StaffAttendance)
            .join(LeaveRequest, LeaveRequest.id == StaffAttendance.leave_request_id)
            .where(
                StaffAttendance.school_id == school_id,
                StaffAttendance.staff_id == staff_id,
                StaffAttendance.attendance_date.like(f"{period_prefix}%"),
                StaffAttendance.status == AttendanceStatus.EXCUSED,
                LeaveRequest.leave_type == LeaveType.UNPAID,
            )
        )
        return len(result.scalars().all())

    def get_period_name(self, year: int, month: int) -> str:
        """Generate readable period name from year and month"""
        months = [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December"
        ]
        try:
            month_name = months[month - 1] if 1 <= month <= 12 else f"Month {month}"
            return f"{month_name} {year}"
        except IndexError:
            return f"Period {month}/{year}"
    
    async def generate_payroll_run(
        self,
        school_id: str,
        year: int,
        month: int,
        current_user: User,
        pay_schedule: PaySchedule = PaySchedule.MONTHLY,
        notes: Optional[str] = None,
        campus_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate a payroll run for a school for a given month + schedule.
        Only staff whose active contract matches `pay_schedule` are
        included — a school running monthly payroll for most staff and a
        separate weekly run for casual/support staff generates two runs,
        not one that silently only ever paid the monthly people.

        Args:
            school_id: School identifier
            year: Payroll year
            month: Payroll month (1-12)
            current_user: User generating the payroll
            pay_schedule: Which contracts' schedule this run covers
            notes: Optional notes for the payroll run
            campus_id: None = school-wide run (every campus, the only mode
                that existed before); set = only staff assigned to that
                campus, for a campus-scoped admin running their own
                campus's payroll independently of the rest of the school.

        Returns:
            Dictionary with payroll run details and status
        """
        try:
            # Validate month
            if not 1 <= month <= 12:
                raise PayrollCalculationError(f"Invalid month: {month}. Must be 1-12.")

            # Check if payroll already exists for this period + schedule (+ campus)
            existing = await self.get_existing_payroll_run(school_id, year, month, pay_schedule, campus_id=campus_id)
            if existing:
                return {
                    "success": False,
                    "message": f"{pay_schedule.value.capitalize()} payroll already exists for {self.get_period_name(year, month)}",
                    "payroll_run": None,
                    "errors": [f"Payroll run status: {existing.status}"]
                }

            # Get active staff, plus anyone who exited during this exact
            # period (so their final pro-rated paycheck still gets
            # generated instead of them silently vanishing from payroll
            # the moment their status flips off ACTIVE).
            staff_list = list(await self.get_active_staff_for_school(school_id, campus_id=campus_id))
            exited_staff_map = await self.get_recently_exited_staff_for_period(school_id, year, month)
            existing_ids = {s.id for s in staff_list}
            missing_exited_ids = [sid for sid in exited_staff_map if sid not in existing_ids]
            if missing_exited_ids:
                extra_query = select(Staff).where(Staff.id.in_(missing_exited_ids), Staff.school_id == school_id)
                if campus_id:
                    extra_query = extra_query.where(Staff.campus_id == campus_id)
                extra_result = await self.session.execute(extra_query)
                staff_list.extend(extra_result.scalars().all())

            if not staff_list:
                return {
                    "success": False,
                    "message": "No active staff found for this school",
                    "payroll_run": None,
                    "errors": []
                }

            # Create payroll run
            period_name = self.get_period_name(year, month)
            payroll_run = PayrollRun(
                school_id=school_id,
                campus_id=campus_id,
                pay_schedule=pay_schedule,
                period_year=year,
                period_month=month,
                period_name=period_name,
                status=PayrollStatus.DRAFT,
                generated_by=current_user.id,
                notes=notes
            )
            self.session.add(payroll_run)
            try:
                await self.session.flush()
            except IntegrityError:
                # uq_payroll_run_school_period hit: a concurrent request
                # generated this exact period between our check above and
                # this insert. The check catches the sequential case; this
                # catches the race.
                await self.session.rollback()
                return {
                    "success": False,
                    "message": f"Payroll already exists for {period_name}",
                    "payroll_run": None,
                    "errors": ["Concurrent payroll generation detected"]
                }

            # Generate line items for each staff
            total_gross = 0.0
            total_allowances = 0.0
            total_deductions = 0.0
            total_net = 0.0
            line_items_created = 0
            errors = []
            created_staff_ids: List[str] = []
            
            # Initialize rules service for deduction rule evaluation
            rules_service = RulesEvaluationService(self.session, school_id)

            # Fetched once per run, not per staff — only actually used for
            # a contract with tax_calculation_mode="bracket" (see
            # calculate_deductions); a school with no contract in bracket
            # mode never even queries this in practice, but fetching it
            # unconditionally here is one cheap query either way.
            brackets_result = await self.session.execute(
                select(PayeBracket).where(PayeBracket.school_id == school_id, PayeBracket.year == year)
            )
            paye_brackets = brackets_result.scalars().all()

            for staff in staff_list:
                try:
                    # Get active contract for staff
                    contract = await self.get_active_contract(school_id, staff.id)
                    if not contract:
                        errors.append(f"No active contract for {staff.first_name} {staff.last_name}")
                        continue

                    # Not an error — this staff member is simply paid on a
                    # different cadence and belongs on that schedule's run.
                    if contract.pay_schedule != pay_schedule:
                        continue

                    # Calculate payroll for this staff
                    calculation = self.calculate_payroll_for_staff(contract, paye_brackets=paye_brackets)

                    # Prepare metadata for rule evaluation. years_service is
                    # deliberately NOT set here — RulesEvaluationService's
                    # _build_context() already computes it for real from
                    # staff.date_joined, and this dict gets merged on top of
                    # that via context.update(metadata), so including a
                    # stub value here would silently clobber the real one.
                    absent_days, present_days = await self.get_staff_attendance_counts(
                        school_id, staff.id, year, month
                    )
                    staff_metadata = {
                        "staff_id": staff.id,
                        "basic_salary": float(contract.basic_salary),
                        "absent_days": absent_days,
                        "present_days": present_days,
                    }
                    
                    # Apply deduction rules
                    applied_rules = []
                    rule_deductions = 0.0
                    try:
                        rule_results, total_rule_deductions = await rules_service.evaluate_rules_for_staff(
                            staff_id=staff.id,
                            basic_salary=float(contract.basic_salary),
                            period_year=year,
                            period_month=month,
                            staff_metadata=staff_metadata
                        )
                        
                        for result in rule_results:
                            applied_rules.append({
                                "rule_id": result.rule_id,
                                "rule_name": result.rule_name,
                                "deduction_amount": result.deduction_amount,
                                "category": result.deduction_type
                            })
                        rule_deductions = total_rule_deductions
                    except Exception as e:
                        logger.warning(f"Error evaluating rules for staff {staff.id}: {str(e)}")
                        # Continue without rules if error occurs
                    
                    # Add rule deductions to total. Reapply the same zero-floor
                    # calculate_net_amount() used above — otherwise heavy rule
                    # deductions can push this staff member's net_amount
                    # negative, which then silently understates
                    # payroll_run.total_net (and the GL credit to Salaries
                    # Payable) by that same amount.
                    calculation["total_deductions"] += rule_deductions
                    calculation["breakdown"]["applied_rules"] = applied_rules
                    calculation["breakdown"]["rule_deductions"] = rule_deductions

                    # LeaveType.UNPAID is documented as "unlimited by
                    # definition" precisely because it isn't paid — but
                    # nothing previously read it at payroll time, so a
                    # staff member on approved unpaid leave was paid in
                    # full regardless. Pro-rate a daily-rate reduction over
                    # the actual number of calendar days in this period
                    # (not a fixed 30), same basis a school would use to
                    # explain the deduction on the payslip.
                    unpaid_leave_days = await self.get_unpaid_leave_days(school_id, staff.id, year, month)
                    unpaid_leave_deduction = 0.0
                    if unpaid_leave_days > 0:
                        days_in_period = calendar.monthrange(year, month)[1]
                        daily_rate = calculation["gross_amount"] / days_in_period
                        unpaid_leave_deduction = round(daily_rate * unpaid_leave_days, 2)
                        calculation["total_deductions"] += unpaid_leave_deduction
                    calculation["breakdown"]["unpaid_leave_days"] = unpaid_leave_days
                    calculation["breakdown"]["unpaid_leave_deduction"] = unpaid_leave_deduction

                    # A brand-new hire starting mid-period or a staff member
                    # whose last working day fell inside this period gets
                    # every component (basic, allowances, deductions,
                    # employer contributions) scaled down proportionally to
                    # the days actually covered — otherwise both were
                    # previously paid/charged a full month regardless.
                    proration_fraction = self.calculate_proration_fraction(
                        year, month, staff.date_joined, exited_staff_map.get(staff.id)
                    )
                    if proration_fraction < 1.0:
                        for key in ("basic_salary", "total_allowances", "gross_amount", "tax_amount",
                                    "pension_amount", "nssf_amount", "other_deductions", "total_deductions",
                                    "employer_nssf_amount", "nssf_tier2_amount"):
                            calculation[key] = round(calculation[key] * proration_fraction, 2)
                        calculation["breakdown"]["proration_fraction"] = proration_fraction

                    calculation["net_amount"] = self.calculate_net_amount(
                        calculation["gross_amount"], calculation["total_deductions"]
                    )

                    # Create line item
                    line_item = PayrollLineItem(
                        payroll_run_id=payroll_run.id,
                        school_id=school_id,
                        staff_id=staff.id,
                        basic_salary=calculation["basic_salary"],
                        total_allowances=calculation["total_allowances"],
                        gross_amount=calculation["gross_amount"],
                        tax_amount=calculation["tax_amount"],
                        pension_amount=calculation["pension_amount"],
                        nssf_amount=calculation["nssf_amount"],
                        other_deductions=calculation["other_deductions"],
                        total_deductions=calculation["total_deductions"],
                        net_amount=calculation["net_amount"],
                        employer_nssf_amount=calculation["employer_nssf_amount"],
                        nssf_tier2_amount=calculation["nssf_tier2_amount"],
                        breakdown=json.dumps(calculation["breakdown"])
                    )
                    self.session.add(line_item)
                    
                    # Accumulate totals
                    total_gross += calculation["gross_amount"]
                    total_allowances += calculation["total_allowances"]
                    total_deductions += calculation["total_deductions"]
                    total_net += calculation["net_amount"]
                    line_items_created += 1
                    created_staff_ids.append(staff.id)

                except Exception as e:
                    errors.append(f"Error calculating payroll for {staff.first_name} {staff.last_name}: {str(e)}")
                    logger.error(f"Error in payroll calculation: {str(e)}")

            # Update payroll run totals
            payroll_run.total_gross = total_gross
            payroll_run.total_allowances = total_allowances
            payroll_run.total_deductions = total_deductions
            payroll_run.total_net = total_net
            payroll_run.staff_count = line_items_created
            payroll_run.status = PayrollStatus.GENERATED if line_items_created > 0 else PayrollStatus.DRAFT

            self.session.add(payroll_run)
            await self.session.flush()

            # Auto-apply any due loan repayments and approved-unpaid leave
            # encashments as pre-approved PayrollAdjustment rows, then
            # recompute each affected line item's net_amount and the run's
            # total_net. Deliberately done after the block above (not folded
            # into the per-staff loop) since _recompute_line_item_net
            # re-aggregates payroll_run.total_net from every line item —
            # doing that mid-loop would just get overwritten by the
            # accumulator-based assignment above.
            loan_service = StaffLoanService(self.session)
            leave_service = LeaveEncashmentService(self.session)
            for staff_id in created_staff_ids:
                needs_recompute = False
                try:
                    # Each staff member's loan/leave adjustments run inside
                    # their own SAVEPOINT. Without this, a single failure
                    # here (e.g. a missing table) poisons the whole
                    # transaction: Postgres refuses every subsequent
                    # statement with "current transaction is aborted" for
                    # the rest of this loop, and the final commit() below
                    # then silently no-ops (Postgres treats COMMIT on an
                    # aborted transaction as a ROLLBACK, without raising) —
                    # discarding the entire payroll run while this method
                    # still reports success. A per-staff savepoint rolls
                    # back only that staff's partial work on error, leaving
                    # the outer transaction (and every other staff's line
                    # item) intact.
                    async with self.session.begin_nested():
                        due_loans = await loan_service.get_due_loan_deductions(
                            school_id, staff_id, year, month
                        )
                        for loan in due_loans:
                            await loan_service.apply_loan_repayment(
                                school_id, payroll_run.id, staff_id, loan
                            )
                            needs_recompute = True

                        leave_requests = await leave_service.get_approved_unpaid_for_staff(
                            school_id, staff_id
                        )
                        for leave_request in leave_requests:
                            await leave_service.mark_paid(
                                school_id, payroll_run.id, staff_id, leave_request
                            )
                            needs_recompute = True

                    if needs_recompute:
                        await self._recompute_line_item_net(school_id, payroll_run.id, staff_id)
                except Exception as e:
                    logger.error(
                        f"Error applying loan/leave adjustments for staff {staff_id} "
                        f"on payroll run {payroll_run.id}: {str(e)}"
                    )
                    errors.append(f"Error applying loan/leave adjustments for staff {staff_id}: {str(e)}")

            await self.session.commit()

            # Belt-and-suspenders check: confirm the run actually persisted
            # before reporting success. This runs in a fresh transaction, so
            # it reflects real DB state regardless of what caused any prior
            # silent rollback (the savepoint above covers the known cause,
            # but this guards against reporting a false success either way).
            verify_result = await self.session.execute(
                select(PayrollRun).where(PayrollRun.id == payroll_run.id)
            )
            if verify_result.scalar_one_or_none() is None:
                logger.error(
                    f"Payroll run {payroll_run.id} for {period_name} (school {school_id}) "
                    f"did not persist after commit — transaction was aborted mid-generation."
                )
                return {
                    "success": False,
                    "message": f"Payroll generation for {period_name} failed — no run was saved",
                    "payroll_run": None,
                    "errors": errors or ["Payroll run failed to persist after commit"],
                    "success_count": 0
                }

            await self._log_payroll_audit(
                school_id, payroll_run.id, AuditActionType.PAYROLL_GENERATED,
                user_id=current_user.id, user_name=getattr(current_user, "email", current_user.id),
                new_values={"period_name": period_name, "staff_count": line_items_created, "total_net": total_net},
            )

            return {
                "success": len(errors) == 0,
                "message": f"Payroll generated for {period_name} with {line_items_created} staff",
                "payroll_run": {
                    "id": payroll_run.id,
                    "school_id": payroll_run.school_id,
                    "period_name": payroll_run.period_name,
                    "status": payroll_run.status,
                    "staff_count": payroll_run.staff_count,
                    "total_gross": payroll_run.total_gross,
                    "total_allowances": payroll_run.total_allowances,
                    "total_deductions": payroll_run.total_deductions,
                    "total_net": payroll_run.total_net,
                    "created_at": payroll_run.created_at.isoformat()
                },
                "errors": errors,
                "success_count": line_items_created
            }
            
        except PayrollCalculationError as e:
            logger.error(f"Payroll calculation error: {str(e)}")
            return {
                "success": False,
                "message": f"Payroll generation failed: {str(e)}",
                "payroll_run": None,
                "errors": [str(e)]
            }
        except Exception as e:
            logger.error(f"Unexpected error generating payroll: {str(e)}")
            await self.session.rollback()
            return {
                "success": False,
                "message": f"Payroll generation failed: {str(e)}",
                "payroll_run": None,
                "errors": [str(e)]
            }
    
    # ==================== Payslips ====================

    async def get_payslip_data(
        self,
        school_id: str,
        run_id: str,
        staff_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Assemble the payslip data dict for a staff member's line item on a
        run. Single source of truth shared by the JSON payslip endpoint and
        the PDF payslip endpoint, so both always show the same numbers.

        Returns None if the run or the staff member's line item doesn't exist
        (school_id scoping is the caller's/router's responsibility, matching
        the rest of this service).
        """
        from models.school import School

        run_result = await self.session.execute(
            select(PayrollRun).where(PayrollRun.id == run_id)
        )
        run = run_result.scalar_one_or_none()
        if not run:
            return None

        line_result = await self.session.execute(
            select(PayrollLineItem).where(
                PayrollLineItem.payroll_run_id == run_id,
                PayrollLineItem.staff_id == staff_id,
            )
        )
        line_item = line_result.scalar_one_or_none()
        if not line_item:
            return None

        staff = await self.get_staff_by_id(staff_id, run.school_id)

        school_result = await self.session.execute(
            select(School).where(School.id == run.school_id)
        )
        school = school_result.scalar_one_or_none()

        # Best-effort currency: the currently-active contract's currency —
        # PayrollLineItem doesn't record which contract generated it, but a
        # staff member's currency essentially never changes mid-tenure, so
        # this is accurate in the overwhelming common case. Previously
        # hardcoded to "GHS" regardless of the contract's actual currency
        # field, which is wrong for any foreign-currency contract.
        contract = await self.get_active_contract(run.school_id, staff_id)
        currency = contract.currency if contract else "GHS"

        # The itemized breakdown (per-allowance, per-deduction-rule,
        # unpaid-leave, proration) was already computed and stored in
        # line_item.breakdown at generation time, but previously never
        # read back here — every payslip only ever showed lump totals.
        breakdown = {}
        if line_item.breakdown:
            try:
                breakdown = json.loads(line_item.breakdown)
            except (json.JSONDecodeError, TypeError):
                breakdown = {}

        adjustments_result = await self.session.execute(
            select(PayrollAdjustment).where(
                PayrollAdjustment.payroll_run_id == run_id,
                PayrollAdjustment.staff_id == staff_id,
                PayrollAdjustment.approved_by.is_not(None),
            ).order_by(PayrollAdjustment.approved_at)
        )
        adjustments = [
            {"type": a.adjustment_type, "amount": a.amount, "reason": a.reason}
            for a in adjustments_result.scalars().all()
        ]

        return {
            "payroll_run_id": run.id,
            "period_name": run.period_name,
            "school_name": school.name if school else "School",
            "staff_id": staff_id,
            "staff_id_code": staff.staff_id if staff else "",
            "staff_name": f"{staff.first_name} {staff.last_name}" if staff else "Unknown",
            "position": staff.position if staff else "",
            "basic_salary": line_item.basic_salary,
            "total_allowances": line_item.total_allowances,
            "allowance_breakdown": breakdown.get("allowances", {}),
            "gross_amount": line_item.gross_amount,
            "tax_amount": line_item.tax_amount,
            "pension_amount": line_item.pension_amount,
            "nssf_amount": line_item.nssf_amount,
            "other_deductions": line_item.other_deductions,
            "deduction_breakdown": breakdown.get("deductions", {}),
            "applied_rules": breakdown.get("applied_rules", []),
            "unpaid_leave_days": breakdown.get("unpaid_leave_days", 0),
            "unpaid_leave_deduction": breakdown.get("unpaid_leave_deduction", 0.0),
            "proration_fraction": breakdown.get("proration_fraction"),
            "total_deductions": line_item.total_deductions,
            "total_adjustments": line_item.total_adjustments,
            "adjustments": adjustments,
            "net_amount": line_item.net_amount,
            "currency": currency,
            "generated_at": run.created_at.isoformat(),
            "posted_at": run.posted_at.isoformat() if run.posted_at else None,
            "payment_status": line_item.payment_status,
            "paid_at": line_item.paid_at.isoformat() if line_item.paid_at else None,
        }

    async def get_ytd_summary_for_staff(self, school_id: str, staff_id: str, year: int) -> Dict[str, Any]:
        """Self-service year-to-date earnings/tax summary for one staff
        member — previously the only aggregate like this
        (export_annual_payroll_summary in routers/payroll.py) was an
        admin-only CSV export across all staff; a staff member had no way
        to get their own YTD figures for a personal tax filing or a bank
        loan reference letter without asking an admin for the export."""
        runs_result = await self.session.execute(
            select(PayrollRun).where(
                PayrollRun.school_id == school_id,
                PayrollRun.status == PayrollStatus.POSTED,
            )
        )
        run_ids_in_year = [r.id for r in runs_result.scalars().all() if str(year) in (r.period_name or "")]

        totals = {"gross": 0.0, "tax": 0.0, "pension": 0.0, "nssf": 0.0, "other_deductions": 0.0, "net": 0.0, "pay_periods": 0}
        if run_ids_in_year:
            lines_result = await self.session.execute(
                select(PayrollLineItem).where(
                    PayrollLineItem.payroll_run_id.in_(run_ids_in_year),
                    PayrollLineItem.staff_id == staff_id,
                )
            )
            for li in lines_result.scalars().all():
                totals["gross"] += float(li.gross_amount or 0.0)
                totals["tax"] += float(li.tax_amount or 0.0)
                totals["pension"] += float(li.pension_amount or 0.0)
                totals["nssf"] += float(li.nssf_amount or 0.0)
                totals["other_deductions"] += float(li.other_deductions or 0.0)
                totals["net"] += float(li.net_amount or 0.0)
                totals["pay_periods"] += 1

        contract = await self.get_active_contract(school_id, staff_id)
        return {
            "staff_id": staff_id,
            "year": year,
            "currency": contract.currency if contract else "GHS",
            **{k: round(v, 2) if isinstance(v, float) else v for k, v in totals.items()},
        }

    # ==================== Payroll Adjustments ====================
    # Bonus/penalty/advance-recovery adjustments against a specific staff
    # member's line item on a specific run. Created as pending; only
    # approved adjustments affect net_amount, so an unreviewed bonus can
    # never accidentally hit take-home pay. Only allowed while the run is
    # still DRAFT/GENERATED — once a run is APPROVED its numbers are meant
    # to be final, matching why reject_payroll_run exists as the one way
    # back to an editable state.

    async def create_adjustment(
        self,
        school_id: str,
        payroll_run_id: str,
        staff_id: str,
        adjustment_type: str,
        amount: float,
        reason: str,
        created_by: str,
    ) -> Dict[str, Any]:
        """Create a pending adjustment against a staff member's line item."""
        try:
            run_result = await self.session.execute(
                select(PayrollRun).where(
                    PayrollRun.id == payroll_run_id, PayrollRun.school_id == school_id
                )
            )
            run = run_result.scalar_one_or_none()
            if not run:
                return {"success": False, "error": "Payroll run not found"}

            if run.status not in (PayrollStatus.DRAFT, PayrollStatus.GENERATED):
                return {
                    "success": False,
                    "error": f"Cannot add adjustments to a run in {run.status} status — reject it back to draft first",
                }

            line_result = await self.session.execute(
                select(PayrollLineItem).where(
                    PayrollLineItem.payroll_run_id == payroll_run_id,
                    PayrollLineItem.staff_id == staff_id,
                )
            )
            if not line_result.scalar_one_or_none():
                return {"success": False, "error": "Staff member has no line item on this run"}

            adjustment = PayrollAdjustment(
                payroll_run_id=payroll_run_id,
                school_id=school_id,
                staff_id=staff_id,
                adjustment_type=adjustment_type,
                amount=amount,
                reason=reason,
                created_by=created_by,
            )
            self.session.add(adjustment)
            await self.session.commit()
            await self.session.refresh(adjustment)

            return {
                "success": True,
                "adjustment_id": adjustment.id,
                "message": "Adjustment created, pending approval",
            }

        except Exception as e:
            logger.error(f"Error creating payroll adjustment: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def list_adjustments(
        self,
        school_id: str,
        payroll_run_id: Optional[str] = None,
        staff_id: Optional[str] = None,
    ) -> List[PayrollAdjustment]:
        """List adjustments, optionally filtered by run and/or staff member."""
        query = select(PayrollAdjustment).where(PayrollAdjustment.school_id == school_id)
        if payroll_run_id:
            query = query.where(PayrollAdjustment.payroll_run_id == payroll_run_id)
        if staff_id:
            query = query.where(PayrollAdjustment.staff_id == staff_id)
        query = query.order_by(PayrollAdjustment.created_at.desc())
        result = await self.session.execute(query)
        return result.scalars().all()

    async def _recompute_line_item_net(
        self,
        school_id: str,
        payroll_run_id: str,
        staff_id: str,
    ) -> Optional[PayrollLineItem]:
        """Recompute a line item's total_adjustments/net_amount from scratch
        off ALL approved PayrollAdjustment rows for this (run, staff) pair,
        then roll the run's total_net up from a fresh aggregation of every
        line item on the run.

        Idempotent and self-correcting regardless of call order — shared by
        manual adjustment approval (approve_adjustment) and the automatic
        loan-repayment/leave-encashment adjustments applied during payroll
        generation (generate_payroll_run), so both go through one code path
        instead of two copies that could drift apart.

        Locks the line item FOR UPDATE: concurrent recomputes for the same
        staff member's line item must not race each other.
        """
        line_result = await self.session.execute(
            select(PayrollLineItem).where(
                PayrollLineItem.payroll_run_id == payroll_run_id,
                PayrollLineItem.staff_id == staff_id,
            ).with_for_update()
        )
        line_item = line_result.scalar_one_or_none()
        if not line_item:
            return None

        approved_result = await self.session.execute(
            select(PayrollAdjustment).where(
                PayrollAdjustment.payroll_run_id == payroll_run_id,
                PayrollAdjustment.staff_id == staff_id,
                PayrollAdjustment.approved_by.is_not(None),
            )
        )
        total_adjustments = sum(float(a.amount) for a in approved_result.scalars().all())

        base_net = max(line_item.gross_amount - line_item.total_deductions, 0.0)
        line_item.total_adjustments = round(total_adjustments, 2)
        line_item.net_amount = round(max(base_net + total_adjustments, 0.0), 2)
        line_item.updated_at = datetime.utcnow()
        self.session.add(line_item)
        await self.session.flush()

        run_result = await self.session.execute(
            select(PayrollRun).where(PayrollRun.id == payroll_run_id)
        )
        run = run_result.scalar_one_or_none()
        if run:
            all_lines_result = await self.session.execute(
                select(PayrollLineItem).where(PayrollLineItem.payroll_run_id == payroll_run_id)
            )
            run.total_net = round(sum(li.net_amount for li in all_lines_result.scalars().all()), 2)
            run.updated_at = datetime.utcnow()
            self.session.add(run)
            await self.session.flush()

        return line_item

    async def approve_adjustment(
        self,
        school_id: str,
        adjustment_id: str,
        approved_by: str,
    ) -> Dict[str, Any]:
        """Approve a pending adjustment and fold it into the line item's
        net_amount immediately via _recompute_line_item_net."""
        try:
            adj_result = await self.session.execute(
                select(PayrollAdjustment).where(
                    PayrollAdjustment.id == adjustment_id,
                    PayrollAdjustment.school_id == school_id,
                )
            )
            adjustment = adj_result.scalar_one_or_none()
            if not adjustment:
                return {"success": False, "error": "Adjustment not found"}

            if adjustment.approved_by is not None:
                return {"success": False, "error": "Adjustment already approved"}

            if adjustment.created_by == approved_by:
                return {"success": False, "error": "You cannot approve an adjustment you created yourself — ask another admin to approve it"}

            run_result = await self.session.execute(
                select(PayrollRun).where(PayrollRun.id == adjustment.payroll_run_id)
            )
            run = run_result.scalar_one_or_none()
            if not run or run.status not in (PayrollStatus.DRAFT, PayrollStatus.GENERATED):
                return {
                    "success": False,
                    "error": "Cannot approve an adjustment on a run that is no longer draft/generated",
                }

            adjustment.approved_by = approved_by
            adjustment.approved_at = datetime.utcnow()
            adjustment.updated_at = datetime.utcnow()
            self.session.add(adjustment)
            await self.session.flush()

            line_item = await self._recompute_line_item_net(
                school_id, adjustment.payroll_run_id, adjustment.staff_id
            )
            if not line_item:
                return {"success": False, "error": "Staff member has no line item on this run"}

            await self.session.commit()

            return {
                "success": True,
                "adjustment_id": adjustment.id,
                "line_item_net_amount": line_item.net_amount,
                "message": "Adjustment approved and applied",
            }

        except Exception as e:
            logger.error(f"Error approving payroll adjustment: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    async def delete_adjustment(
        self,
        school_id: str,
        adjustment_id: str,
    ) -> Dict[str, Any]:
        """Retract a pending (not yet approved) adjustment."""
        try:
            adj_result = await self.session.execute(
                select(PayrollAdjustment).where(
                    PayrollAdjustment.id == adjustment_id,
                    PayrollAdjustment.school_id == school_id,
                )
            )
            adjustment = adj_result.scalar_one_or_none()
            if not adjustment:
                return {"success": False, "error": "Adjustment not found"}

            if adjustment.approved_by is not None:
                return {"success": False, "error": "Cannot delete an already-approved adjustment"}

            await self.session.delete(adjustment)
            await self.session.commit()
            return {"success": True, "message": "Adjustment deleted"}

        except Exception as e:
            logger.error(f"Error deleting payroll adjustment: {str(e)}")
            await self.session.rollback()
            return {"success": False, "error": str(e)}

    # ==================== Payroll Run Management ====================

    async def _has_other_eligible_payroll_approver(self, school_id: str, exclude_user_id: str) -> bool:
        """Is there any OTHER active user at this school who could approve
        a payroll run (i.e. holds SCHOOL_ADMIN or HR — the two roles
        scripts/seed_permissions.py grants payroll.run.approve to)? Used
        only to decide whether the self-approval block below can be
        relaxed — see approve_payroll_run's self_approve_confirm handling."""
        from models.user import UserRole as _UserRole
        result = await self.session.execute(
            select(User).where(
                User.school_id == school_id,
                User.id != exclude_user_id,
                User.is_active == True,  # noqa: E712
                User.role.in_([_UserRole.SCHOOL_ADMIN, _UserRole.HR]),
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def approve_payroll_run(
        self,
        school_id: str,
        payroll_run_id: str,
        current_user: User,
        notes: Optional[str] = None,
        self_approve_confirm: bool = False,
    ) -> Dict[str, Any]:
        """
        Approve a payroll run (move from GENERATED to APPROVED status).

        Args:
            school_id: School identifier
            payroll_run_id: Payroll run identifier
            current_user: User approving the payroll
            notes: Optional approval notes
            self_approve_confirm: Explicit override to approve a run this
                same user generated. Only honored when this school genuinely
                has no OTHER SCHOOL_ADMIN/HR user who could approve it
                instead — otherwise a solo administrator could never run
                payroll at all, since self-approval is blocked by default
                (segregation of duties, matching every other maker/checker
                control in this codebase) with no other path around it.

        Returns:
            Status dictionary
        """
        try:
            # Locked FOR UPDATE for the same reason post_payroll_run is: a
            # concurrent approve/reject on the same run must not both pass
            # the status == GENERATED check below and race each other to
            # commit a different final status.
            result = await self.session.execute(
                select(PayrollRun).where(
                    PayrollRun.id == payroll_run_id,
                    PayrollRun.school_id == school_id
                ).with_for_update()
            )
            payroll_run = result.scalar_one_or_none()

            if not payroll_run:
                return {
                    "success": False,
                    "message": "Payroll run not found"
                }

            if payroll_run.status != PayrollStatus.GENERATED:
                return {
                    "success": False,
                    "message": f"Cannot approve payroll in {payroll_run.status} status"
                }

            if payroll_run.generated_by == current_user.id:
                has_other_approver = await self._has_other_eligible_payroll_approver(school_id, current_user.id)
                if has_other_approver or not self_approve_confirm:
                    return {
                        "success": False,
                        "message": (
                            "You cannot approve a payroll run you generated yourself — ask another admin to approve it"
                            if has_other_approver else
                            "You cannot approve a payroll run you generated yourself. No other admin/HR user exists "
                            "at this school to approve it instead — pass self_approve_confirm=true to approve it "
                            "yourself as a last resort."
                        ),
                    }

            payroll_run.status = PayrollStatus.APPROVED
            payroll_run.approved_by = current_user.id
            payroll_run.approved_at = datetime.utcnow()
            if notes:
                payroll_run.notes = notes

            self.session.add(payroll_run)
            await self.session.commit()

            await self._log_payroll_audit(
                school_id, payroll_run.id, AuditActionType.PAYROLL_APPROVED,
                user_id=current_user.id, user_name=getattr(current_user, "email", current_user.id),
                new_values={"self_approved": payroll_run.generated_by == current_user.id},
            )

            return {
                "success": True,
                "message": f"Payroll run approved",
                "payroll_run_id": payroll_run.id,
                "status": payroll_run.status
            }

        except Exception as e:
            logger.error(f"Error approving payroll run: {str(e)}")
            await self.session.rollback()
            return {
                "success": False,
                "message": f"Error approving payroll: {str(e)}"
            }

    async def reject_payroll_run(
        self,
        school_id: str,
        payroll_run_id: str,
        current_user: User,
        notes: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Reject a payroll run. Despite the name, this does NOT send it back
        to an editable DRAFT — REJECTED is a permanent terminal status (no
        code path anywhere transitions a run out of REJECTED). The actual
        recovery path is generating a brand-new run for the same
        period+schedule: get_existing_payroll_run deliberately excludes
        REJECTED runs from its "does one already exist" check, so a fresh
        POST /payroll/runs immediately after rejection works. If you need
        to preserve/inspect what was rejected (e.g. for audit), do so
        before rejecting — the rejected run's data stays queryable, it's
        just never reusable as a live run again.

        Args:
            school_id: School identifier
            payroll_run_id: Payroll run identifier
            current_user: User rejecting the payroll
            notes: Optional rejection reason

        Returns:
            Status dictionary
        """
        try:
            # Locked FOR UPDATE — see approve_payroll_run's identical
            # comment: a concurrent approve/reject on the same run must not
            # both pass the status == GENERATED check below.
            result = await self.session.execute(
                select(PayrollRun).where(
                    PayrollRun.id == payroll_run_id,
                    PayrollRun.school_id == school_id
                ).with_for_update()
            )
            payroll_run = result.scalar_one_or_none()

            if not payroll_run:
                return {
                    "success": False,
                    "message": "Payroll run not found"
                }

            if payroll_run.status != PayrollStatus.GENERATED:
                return {
                    "success": False,
                    "message": f"Cannot reject payroll in {payroll_run.status} status"
                }

            payroll_run.status = PayrollStatus.REJECTED
            if notes:
                payroll_run.notes = notes
            payroll_run.updated_at = datetime.utcnow()

            self.session.add(payroll_run)
            await self.session.commit()

            await self._log_payroll_audit(
                school_id, payroll_run.id, AuditActionType.PAYROLL_REJECTED,
                user_id=current_user.id, user_name=getattr(current_user, "email", current_user.id),
                new_values={"reason": notes},
            )

            return {
                "success": True,
                "message": "Payroll run rejected",
                "payroll_run_id": payroll_run.id,
                "status": payroll_run.status
            }

        except Exception as e:
            logger.error(f"Error rejecting payroll run: {str(e)}")
            await self.session.rollback()
            return {
                "success": False,
                "message": f"Error rejecting payroll: {str(e)}"
            }

    async def post_payroll_run(
        self,
        school_id: str,
        payroll_run_id: str,
        posted_by: Optional[User] = None,
    ) -> Dict[str, Any]:
        """
        Post a payroll run (move from APPROVED to POSTED status, final state).

        Also creates a journal entry to GL for the payroll posting:
        - Dr. 5100 (Salaries and Wages): Gross salary amount
        - Dr. 5110 (Payroll Taxes and Contributions): Employer-side SSNIT Tier-1 + Tier-2
        - Cr. 2100 (Salaries Payable): Net salary to be paid
        - Cr. 2110/2112/2113/2120/2130: Deductions + employer contributions payable

        Args:
            school_id: School identifier
            payroll_run_id: Payroll run identifier
            posted_by: User posting the run (for audit logging only — None
                is accepted for backward compatibility with any internal
                caller that doesn't have a User handy).

        Returns:
            Status dictionary with payroll_run_id and journal_entry_id if successful
        """
        try:
            # Get the payroll run. Locked FOR UPDATE so a concurrent second
            # "Post" click (or request retry) blocks here instead of both
            # racing past the status == APPROVED check below and each
            # creating their own GL journal entry for the same payroll run.
            result = await self.session.execute(
                select(PayrollRun)
                .where(
                    PayrollRun.id == payroll_run_id,
                    PayrollRun.school_id == school_id
                )
                .with_for_update()
            )
            payroll_run = result.scalar_one_or_none()

            if not payroll_run:
                return {
                    "success": False,
                    "message": "Payroll run not found"
                }

            if payroll_run.status != PayrollStatus.APPROVED:
                return {
                    "success": False,
                    "message": f"Cannot post payroll in {payroll_run.status} status"
                }
            
            # Create GL journal entry for payroll posting
            journal_entry_id = None
            try:
                journal_entry_id = await self._create_payroll_journal_entry(
                    school_id=school_id,
                    payroll_run=payroll_run,
                )
                logger.info(f"Created journal entry {journal_entry_id} for payroll run {payroll_run_id}")
            except Exception as e:
                logger.error(f"Error creating journal entry for payroll: {str(e)}")
                # Continue posting even if GL entry fails (data consistency is paramount)
                # But log the error for manual reconciliation
            
            # Update payroll run status
            payroll_run.status = PayrollStatus.POSTED
            payroll_run.posted_at = datetime.utcnow()
            if journal_entry_id:
                payroll_run.journal_entry_id = journal_entry_id

            self.session.add(payroll_run)
            await self.session.commit()

            await self._log_payroll_audit(
                school_id, payroll_run.id, AuditActionType.PAYROLL_POSTED,
                user_id=(posted_by.id if posted_by else "SYSTEM"),
                user_name=(getattr(posted_by, "email", posted_by.id) if posted_by else "SYSTEM"),
                new_values={"journal_entry_id": journal_entry_id, "total_net": payroll_run.total_net},
            )

            response = {
                "success": True,
                "message": "Payroll run posted successfully",
                "payroll_run_id": payroll_run.id,
                "status": payroll_run.status
            }

            if journal_entry_id:
                response["journal_entry_id"] = journal_entry_id

            return response
            
        except Exception as e:
            logger.error(f"Error posting payroll run: {str(e)}")
            await self.session.rollback()
            return {
                "success": False,
                "message": f"Error posting payroll: {str(e)}"
            }

    async def void_payroll_run(
        self,
        school_id: str,
        payroll_run_id: str,
        voided_by: User,
        reason: str,
    ) -> Dict[str, Any]:
        """Void a POSTED run discovered to be wrong before any staff member
        has actually been paid — previously there was no way to fix a
        wrong run at all short of manual, out-of-band GL surgery outside
        this module entirely. Reverses the run's journal entry via
        JournalEntryService.reverse_entry (which itself enforces the
        maker-checker segregation-of-duties check already applied to every
        other reversal in this codebase) and marks the run VOIDED.

        Refused once ANY line item has payment_status == "paid" — money
        has already left the school's account for at least one staff
        member at that point, and undoing that isn't something a GL
        reversal alone can fix (see disburse_payroll_run's own separate
        clearing entry, which would also need reversing, and the real
        Paystack transfer itself, which this can't undo)."""
        try:
            result = await self.session.execute(
                select(PayrollRun).where(
                    PayrollRun.id == payroll_run_id, PayrollRun.school_id == school_id
                ).with_for_update()
            )
            payroll_run = result.scalar_one_or_none()
            if not payroll_run:
                return {"success": False, "message": "Payroll run not found"}

            if payroll_run.status != PayrollStatus.POSTED:
                return {"success": False, "message": f"Cannot void a run in {payroll_run.status} status — only a POSTED run can be voided"}

            paid_result = await self.session.execute(
                select(PayrollLineItem).where(
                    PayrollLineItem.payroll_run_id == payroll_run_id,
                    PayrollLineItem.payment_status == "paid",
                ).limit(1)
            )
            if paid_result.scalar_one_or_none() is not None:
                return {
                    "success": False,
                    "message": "Cannot void — at least one staff member has already been paid for this run. "
                                "Money has actually moved; this can no longer be undone by voiding.",
                }

            reversal_journal_entry_id = None
            if payroll_run.journal_entry_id:
                journal_service = JournalEntryService(self.session)
                try:
                    _, reversal = await journal_service.reverse_entry(
                        school_id=school_id, entry_id=payroll_run.journal_entry_id,
                        reversed_by=voided_by.id, reversal_reason=f"Payroll run voided: {reason}",
                    )
                    reversal_journal_entry_id = reversal.id
                except JournalEntryError as e:
                    return {"success": False, "message": f"Cannot void — {str(e)}"}

            payroll_run.status = PayrollStatus.VOIDED
            payroll_run.voided_at = datetime.utcnow()
            payroll_run.voided_by = voided_by.id
            payroll_run.void_reason = reason
            payroll_run.reversal_journal_entry_id = reversal_journal_entry_id
            payroll_run.updated_at = datetime.utcnow()
            self.session.add(payroll_run)
            await self.session.commit()

            await self._log_payroll_audit(
                school_id, payroll_run_id, AuditActionType.PAYROLL_VOIDED,
                user_id=voided_by.id, user_name=getattr(voided_by, "email", voided_by.id),
                new_values={"reason": reason, "reversal_journal_entry_id": reversal_journal_entry_id},
            )

            return {
                "success": True,
                "message": "Payroll run voided",
                "payroll_run_id": payroll_run_id,
                "reversal_journal_entry_id": reversal_journal_entry_id,
            }
        except Exception as e:
            logger.error(f"Error voiding payroll run: {str(e)}")
            await self.session.rollback()
            return {"success": False, "message": f"Error voiding payroll: {str(e)}"}

    # ==================== Disbursement ====================
    # "Posted" only books the GL liability (Cr. Salaries Payable) — it never
    # moved real money. This actually pays staff via Paystack Transfers,
    # reusing the exact same payout_bank_code/payout_account_number/
    # payout_verification_status fields and admin-verification flow already
    # built for extra-class teacher payouts on the Staff model. Only a
    # POSTED run can be paid (the GL entry must be final first), and each
    # staff member is tried independently — one missing/unverified payout
    # detail fails that person's line item without blocking everyone else's.

    async def disburse_payroll_run(
        self,
        school_id: str,
        payroll_run_id: str,
        disbursed_by: Optional[User] = None,
    ) -> Dict[str, Any]:
        import os
        from services.paystack_service import PaystackService

        result = await self.session.execute(
            select(PayrollRun).where(
                PayrollRun.id == payroll_run_id, PayrollRun.school_id == school_id
            )
        )
        run = result.scalar_one_or_none()
        if not run:
            return {"success": False, "message": "Payroll run not found"}

        if run.status != PayrollStatus.POSTED:
            return {
                "success": False,
                "message": f"Cannot disburse a run in {run.status} status — it must be posted first",
            }

        paystack_secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
        if not paystack_secret_key:
            return {"success": False, "message": "Payment gateway not configured"}
        paystack = PaystackService(paystack_secret_key)

        lines_result = await self.session.execute(
            select(PayrollLineItem).where(
                PayrollLineItem.payroll_run_id == payroll_run_id,
                PayrollLineItem.payment_status.in_(["unpaid", "failed"]),
            )
        )
        line_items = lines_result.scalars().all()

        paid_count = 0
        failed_count = 0
        paid_total = 0.0
        errors: List[str] = []

        for line_item in line_items:
            staff_result = await self.session.execute(
                select(Staff).where(Staff.id == line_item.staff_id)
            )
            staff = staff_result.scalar_one_or_none()
            staff_label = f"{staff.first_name} {staff.last_name}" if staff else line_item.staff_id

            if (
                not staff
                or staff.payout_verification_status != PayoutVerificationStatus.VERIFIED
                or not staff.payout_account_number
                or not staff.payout_bank_code
            ):
                line_item.payment_status = "failed"
                line_item.payment_failure_reason = "Payout details not verified"
                line_item.updated_at = datetime.utcnow()
                self.session.add(line_item)
                failed_count += 1
                errors.append(f"{staff_label}: payout details not verified")
                continue

            if line_item.net_amount <= 0:
                line_item.payment_status = "failed"
                line_item.payment_failure_reason = "Net amount is zero"
                line_item.updated_at = datetime.utcnow()
                self.session.add(line_item)
                failed_count += 1
                errors.append(f"{staff_label}: net amount is zero")
                continue

            recipient_type = "nuban" if staff.payout_account_type == "bank" else "mobile_money"
            recipient_result = await paystack.create_transfer_recipient(
                type_=recipient_type,
                account_number=staff.payout_account_number,
                account_name=staff.payout_account_name or staff_label,
                currency="GHS",
                bank_code=staff.payout_bank_code,
            )
            if not recipient_result.get("success"):
                line_item.payment_status = "failed"
                line_item.payment_failure_reason = recipient_result.get("error", "Could not create transfer recipient")
                line_item.updated_at = datetime.utcnow()
                self.session.add(line_item)
                failed_count += 1
                errors.append(f"{staff_label}: {line_item.payment_failure_reason}")
                continue

            transfer_reference = f"PAYROLL-{line_item.id}"
            transfer_result = await paystack.initiate_transfer(
                source="balance",
                amount=int(round(line_item.net_amount * 100)),
                recipient_code=recipient_result["recipient_code"],
                reason=f"Salary — {run.period_name}",
                reference=transfer_reference,
            )

            if transfer_result.get("success"):
                line_item.payment_status = "paid"
                line_item.paid_at = datetime.utcnow()
                line_item.transfer_reference = transfer_result.get("transfer_code", transfer_reference)
                line_item.payment_failure_reason = None
                paid_count += 1
                paid_total += float(line_item.net_amount)
                await self._notify_staff_paid(staff, run, line_item)
            else:
                line_item.payment_status = "failed"
                line_item.payment_failure_reason = transfer_result.get("error", "Transfer failed")
                failed_count += 1
                errors.append(f"{staff_label}: {line_item.payment_failure_reason}")

            line_item.updated_at = datetime.utcnow()
            self.session.add(line_item)

        await self.session.commit()

        # "Posted" only ever booked the GL liability (Dr Salary Expense /
        # Cr Salaries Payable) — actually paying staff here never cleared
        # that liability back down, so Salaries Payable sat overstated on
        # the books forever with no link between the real payment event
        # and the GL. Clear it now for whatever was ACTUALLY paid out
        # successfully (paid_total may be less than the run's total_net if
        # some staff failed) — best-effort, matching every other GL
        # posting call site in this codebase (a GL failure must not undo a
        # payment that already went through via Paystack).
        clearing_journal_entry_id = None
        if paid_total > 0.01:
            try:
                clearing_journal_entry_id = await self._create_disbursement_journal_entry(
                    school_id=school_id, run=run, amount_paid=paid_total,
                )
                run.disbursement_journal_entry_id = clearing_journal_entry_id
                run.updated_at = datetime.utcnow()
                self.session.add(run)
                await self.session.commit()
            except Exception as e:
                logger.error(f"Error creating disbursement clearing journal entry for payroll run {payroll_run_id}: {str(e)}")

        await self._log_payroll_audit(
            school_id, payroll_run_id, AuditActionType.PAYROLL_DISBURSED,
            user_id=(disbursed_by.id if disbursed_by else "SYSTEM"),
            user_name=(getattr(disbursed_by, "email", disbursed_by.id) if disbursed_by else "SYSTEM"),
            new_values={"paid_count": paid_count, "failed_count": failed_count, "paid_total": paid_total},
        )

        response = {
            "success": True,
            "message": f"Disbursed to {paid_count} staff member(s), {failed_count} failed",
            "paid_count": paid_count,
            "failed_count": failed_count,
            "errors": errors,
        }
        if clearing_journal_entry_id:
            response["clearing_journal_entry_id"] = clearing_journal_entry_id
        return response

    async def _create_disbursement_journal_entry(self, school_id: str, run: PayrollRun, amount_paid: float) -> str:
        """Dr Salaries Payable (2100) / Cr Business Checking (1010) for
        whatever was actually transferred out this disbursement call — a
        run can be disbursed more than once (retrying failed line items),
        so this posts once per call for exactly that call's total, not the
        run's full total_net."""
        from services.journal_entry_service import JournalEntryService
        from services.gl_account_helpers import get_or_create_system_account

        salaries_payable_account = await get_or_create_system_account(self.session, school_id, "2100")
        checking_account = await get_or_create_system_account(self.session, school_id, "1010")

        entry_data = JournalEntryCreate(
            entry_date=datetime.utcnow(),
            reference_type=ReferenceType.PAYROLL_RUN,
            reference_id=run.id,
            description=f"Payroll disbursement for {run.period_name}",
            line_items=[
                JournalLineItemCreate(
                    gl_account_id=salaries_payable_account.id, debit_amount=float(amount_paid), credit_amount=0.0,
                    description=f"Salaries payable cleared — {run.period_name}",
                ),
                JournalLineItemCreate(
                    gl_account_id=checking_account.id, debit_amount=0.0, credit_amount=float(amount_paid),
                    description=f"Salaries paid via transfer — {run.period_name}",
                ),
            ],
            notes=f"Auto-posted from payroll disbursement for run {run.id}",
        )
        journal_service = JournalEntryService(self.session)
        entry = await journal_service.create_entry(school_id=school_id, entry_data=entry_data, created_by="SYSTEM")
        posted = await journal_service.post_entry(
            school_id=school_id, entry_id=entry.id, posted_by="SYSTEM",
            approval_notes="Auto-posted from payroll disbursement",
        )
        return posted.id

    async def _notify_staff_paid(self, staff: Staff, run: PayrollRun, line_item: PayrollLineItem) -> None:
        """Best-effort SMS to a staff member once their salary transfer
        actually succeeds. Never raises — a notification failure shouldn't
        undo or block a payment that already went through."""
        if not staff.phone or not sms_service.validate_phone_number(staff.phone):
            return
        try:
            formatted_phone = sms_service.format_phone_number(staff.phone)
            message = (
                f"Hi {staff.first_name}, your salary of GHS {line_item.net_amount:,.2f} for "
                f"{run.period_name} has been paid to your {staff.payout_account_type or 'registered'} account."
            )
            await sms_service.send_sms([formatted_phone], message)
        except Exception as e:
            logger.warning(f"Failed to send payment SMS to staff {staff.id}: {str(e)}")

    async def _create_payroll_journal_entry(
        self,
        school_id: str,
        payroll_run: PayrollRun,
    ) -> str:
        """
        Create a journal entry for payroll posting to GL.
        
        Posts:
        - Dr. 5100 (Salaries and Wages): total_gross
        - Cr. 2100 (Salaries Payable): total_net
        - Cr. 2110 (NSSF Payable): NSSF portion of total_deductions
        - Cr. 2120 (Pension Payable): Pension portion of total_deductions
        - Cr. 2130 (Income Tax Withheld Payable): Tax portion of total_deductions
        
        Args:
            school_id: School identifier
            payroll_run: PayrollRun instance with totals
            
        Returns:
            Journal entry ID
            
        Raises:
            Exception: If GL account not found or other GL errors
        """
        from services.journal_entry_service import JournalEntryService
        from services.gl_account_helpers import get_or_create_system_account

        # Every account below is guaranteed present via get_or_create_system_account
        # (auto-creates from DEFAULT_CHART_OF_ACCOUNTS if a school was seeded
        # before one of these codes existed — e.g. 2112/2113 for employer
        # SSNIT, added this pass) rather than raising if missing.
        salary_expense_account = await get_or_create_system_account(self.session, school_id, "5100")
        employer_contributions_expense_account = await get_or_create_system_account(self.session, school_id, "5110")
        salaries_payable_account = await get_or_create_system_account(self.session, school_id, "2100")
        nssf_payable_account = await get_or_create_system_account(self.session, school_id, "2110")
        employer_nssf_payable_account = await get_or_create_system_account(self.session, school_id, "2112")
        nssf_tier2_payable_account = await get_or_create_system_account(self.session, school_id, "2113")
        pension_payable_account = await get_or_create_system_account(self.session, school_id, "2120")
        tax_payable_account = await get_or_create_system_account(self.session, school_id, "2130")

        # Staff Loans Receivable — best-effort, not required. Schools seeded
        # before the staff-loan feature shipped may not have this account
        # yet; if it's missing, loan-repayment adjustments fall back to the
        # generic adjustment treatment below instead of blocking posting.
        from services.coa_service import CoaService
        from services.staff_loan_service import STAFF_LOAN_RECEIVABLE_ACCOUNT_CODE
        coa_service = CoaService(self.session)
        loan_receivable_account = await coa_service.get_system_account(
            school_id, "staff_loan_receivable", fallback_code=STAFF_LOAN_RECEIVABLE_ACCOUNT_CODE
        )

        # Get payroll line items to calculate deduction breakdown by type
        result = await self.session.execute(
            select(PayrollLineItem).where(
                PayrollLineItem.payroll_run_id == payroll_run.id,
                PayrollLineItem.school_id == school_id
            )
        )
        line_items = result.scalars().all()

        # Calculate deduction breakdown from line items
        total_nssf = 0.0
        total_pension = 0.0
        total_tax = 0.0
        total_other = 0.0
        total_adjustments = 0.0
        total_employer_nssf = 0.0
        total_nssf_tier2 = 0.0

        for line_item in line_items:
            total_nssf += float(line_item.nssf_amount or 0.0)
            total_pension += float(line_item.pension_amount or 0.0)
            total_tax += float(line_item.tax_amount or 0.0)
            total_other += float(line_item.other_deductions or 0.0)
            total_adjustments += float(line_item.total_adjustments or 0.0)
            total_employer_nssf += float(line_item.employer_nssf_amount or 0.0)
            total_nssf_tier2 += float(line_item.nssf_tier2_amount or 0.0)

        # Verify calculation: all deductions should sum to total_deductions
        calculated_total_deductions = total_nssf + total_pension + total_tax + total_other

        # Loan-repayment adjustments (adjustment_type="deduction_loan", see
        # StaffLoanService.apply_loan_repayment) are excluded from the
        # generic total_adjustments folded into the Salaries Expense debit
        # line below: repaying a staff loan through payroll isn't a new
        # payroll expense, it's the staff member's Staff Loans Receivable
        # balance being paid down. They get their own dedicated credit line
        # further down instead. Everything else (bonuses, penalties, leave
        # encashment) is a genuine change in payroll expense and stays in
        # the generic bucket.
        adj_result = await self.session.execute(
            select(PayrollAdjustment).where(
                PayrollAdjustment.payroll_run_id == payroll_run.id,
                PayrollAdjustment.school_id == school_id,
                PayrollAdjustment.adjustment_type == "deduction_loan",
                PayrollAdjustment.approved_by.is_not(None),
            )
        )
        total_loan_deductions = abs(sum(float(a.amount) for a in adj_result.scalars().all()))
        # Only exclude loan deductions from the expense line if there's
        # actually a dedicated account to credit them to instead — otherwise
        # fall back to the original behavior (folded into the generic
        # adjustment bucket) so the entry still balances.
        if loan_receivable_account and total_loan_deductions > 0.01:
            expense_adjustments = total_adjustments + total_loan_deductions
        else:
            expense_adjustments = total_adjustments
            total_loan_deductions = 0.0

        # Build line items for journal entry. total_net already bakes in
        # approved adjustments (bonuses/penalties/leave encashment) on top
        # of gross minus deductions, so the expense side must include them
        # too or this entry won't balance — a bonus raises what the school
        # owes without raising the raw payroll gross, so it has to show up
        # as additional expense here. Loan deductions are added back in
        # (expense_adjustments) since they were subtracted out of
        # total_adjustments above but never counted as expense to begin with.
        journal_line_items = [
            # Debit: Salary Expense (+ any approved bonus/penalty/leave
            # adjustments, excluding loan repayments)
            JournalLineItemCreate(
                gl_account_id=salary_expense_account.id,
                debit_amount=float(payroll_run.total_gross) + expense_adjustments,
                credit_amount=0.0,
                description=f"Payroll for {payroll_run.period_name}: Salaries"
                + (f" (incl. {expense_adjustments:+.2f} adjustments)" if abs(expense_adjustments) > 0.01 else ""),
            ),
            # Credit: Salaries Payable (net to be paid to staff)
            # Note: This includes all deductions, including other_deductions
            JournalLineItemCreate(
                gl_account_id=salaries_payable_account.id,
                debit_amount=0.0,
                credit_amount=float(payroll_run.total_net),
                description=f"Payroll for {payroll_run.period_name}: Net salary payable",
            ),
        ]
        
        # Add deduction credits for known accounts
        # These represent liabilities to be paid to third parties
        remaining_deductions = calculated_total_deductions  # Sum of all deductions
        
        if total_nssf > 0.01:
            journal_line_items.append(
                JournalLineItemCreate(
                    gl_account_id=nssf_payable_account.id,
                    debit_amount=0.0,
                    credit_amount=total_nssf,
                    description=f"Payroll for {payroll_run.period_name}: NSSF contributions",
                )
            )
            remaining_deductions -= total_nssf
        
        if total_pension > 0.01:
            journal_line_items.append(
                JournalLineItemCreate(
                    gl_account_id=pension_payable_account.id,
                    debit_amount=0.0,
                    credit_amount=total_pension,
                    description=f"Payroll for {payroll_run.period_name}: Pension contributions",
                )
            )
            remaining_deductions -= total_pension
        
        if total_tax > 0.01:
            journal_line_items.append(
                JournalLineItemCreate(
                    gl_account_id=tax_payable_account.id,
                    debit_amount=0.0,
                    credit_amount=total_tax,
                    description=f"Payroll for {payroll_run.period_name}: Income tax withheld",
                )
            )
            remaining_deductions -= total_tax

        if loan_receivable_account and total_loan_deductions > 0.01:
            journal_line_items.append(
                JournalLineItemCreate(
                    gl_account_id=loan_receivable_account.id,
                    debit_amount=0.0,
                    credit_amount=total_loan_deductions,
                    description=f"Payroll for {payroll_run.period_name}: Staff loan repayments",
                )
            )

        # Any remaining deductions (other_deductions) are retained/internal adjustments
        # They reduce the expense posting
        if remaining_deductions > 0.01:
            # Reduce the initial salary expense debit by the unaccounted deductions
            journal_line_items[0].debit_amount = float(journal_line_items[0].debit_amount) - remaining_deductions

        # Employer-side statutory contributions (Tier-1 + Tier-2) — a real
        # cost to the school, never deducted from staff pay, so it needs
        # its OWN debit (5110) balanced by its OWN credit lines (2112/2113)
        # rather than folding into the salary-expense/payable lines above.
        # Previously this was never posted anywhere at all.
        total_employer_contributions = total_employer_nssf + total_nssf_tier2
        if total_employer_contributions > 0.01:
            journal_line_items.append(
                JournalLineItemCreate(
                    gl_account_id=employer_contributions_expense_account.id,
                    debit_amount=total_employer_contributions,
                    credit_amount=0.0,
                    description=f"Payroll for {payroll_run.period_name}: Employer SSNIT Tier-1 + Tier-2 contributions",
                )
            )
            if total_employer_nssf > 0.01:
                journal_line_items.append(
                    JournalLineItemCreate(
                        gl_account_id=employer_nssf_payable_account.id,
                        debit_amount=0.0,
                        credit_amount=total_employer_nssf,
                        description=f"Payroll for {payroll_run.period_name}: Employer SSNIT Tier-1 payable",
                    )
                )
            if total_nssf_tier2 > 0.01:
                journal_line_items.append(
                    JournalLineItemCreate(
                        gl_account_id=nssf_tier2_payable_account.id,
                        debit_amount=0.0,
                        credit_amount=total_nssf_tier2,
                        description=f"Payroll for {payroll_run.period_name}: SSNIT Tier-2 payable",
                    )
                )

        # Create the journal entry
        entry_data = JournalEntryCreate(
            entry_date=payroll_run.posted_at or datetime.utcnow(),
            reference_type=ReferenceType.PAYROLL_RUN,
            reference_id=payroll_run.id,
            description=f"Payroll posting for {payroll_run.period_name}",
            line_items=journal_line_items,
            notes=f"Auto-posted from payroll run {payroll_run.id}",
        )
        
        # Use JournalEntryService to create and post the entry
        journal_service = JournalEntryService(self.session)
        entry = await journal_service.create_entry(
            school_id=school_id,
            entry_data=entry_data,
            created_by="SYSTEM",  # Mark as system-generated
        )
        
        # Post the entry immediately (auto-posting)
        posted_entry = await journal_service.post_entry(
            school_id=school_id,
            entry_id=entry.id,
            posted_by="SYSTEM",
            approval_notes="Auto-posted from payroll run",
        )
        
        return posted_entry.id
