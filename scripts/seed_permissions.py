"""Seed the fine-grained RBAC permission catalog + one system Role per
UserRole enum value (see models/rbac.py, services/permission_service.py).

Idempotent — safe to re-run after adding new codes to FINANCE_PERMISSIONS
or SYSTEM_ROLE_GRANTS below (existing rows are matched by unique `code` /
`name` and left alone; only missing rows are inserted).

Seeds the Finance pilot module (routers/finance/*, budget_router.py,
depreciation_router.py, bank_reconciliation_router.py) plus the HR cluster
(hr_admin.py, hr_recruitment.py, hr_development.py, hr_overtime.py,
leave_requests.py, payroll.py, and the staff-attendance half of
attendance.py) and the Academics cluster (grades.py, classes.py,
curriculum.py, tracks.py, academic_calendar.py, exams.py, exam_papers.py,
exam_marks.py, exam_remarks.py, exam_malpractice.py, exam_board.py, and the
student-attendance half of attendance.py). Grants below were derived
directly from each router's existing require_roles(...) calls, so running
this script and migrating a route to require_permission() is
behavior-preserving. Extend the relevant *_PERMISSIONS dict / SYSTEM_ROLE_GRANTS
(or add a new module block) as more routers migrate.

Note: HR/Academics codes for attendance intentionally avoid the
`attendance.record.*` / `staff_attendance.record.*` names already used by
PUBLIC_API_PERMISSIONS below (a separate, API-key-scoped catalog, never
granted to a system Role) — reusing those exact strings would attach
system-Role grants to rows meant only for external API keys, since
Permission.code is unique and this script upserts by code.
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlmodel import select

from database import async_session
from models.rbac import Permission, Role, RolePermission
from models.user import UserRole

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# code -> (module, description)
FINANCE_PERMISSIONS: dict[str, tuple[str, str]] = {
    "finance.coa.create": ("finance", "Create chart-of-accounts (GL) accounts"),
    "finance.coa.update": ("finance", "Edit chart-of-accounts (GL) accounts"),
    "finance.coa.delete": ("finance", "Deactivate chart-of-accounts (GL) accounts"),
    "finance.coa.manage": ("finance", "Recalculate/validate GL account balances"),
    "finance.vendor.manage": ("finance", "Create and edit vendor/supplier master records"),
    "finance.expense.create": ("finance", "Record new expenses"),
    "finance.expense.update": ("finance", "Edit, attach receipts to, and submit expenses"),
    "finance.expense.approve": ("finance", "Approve or reject submitted expenses"),
    "finance.expense.post": ("finance", "Post approved expenses to the GL and record payment"),
    "finance.journal.create": ("finance", "Create journal entries"),
    "finance.journal.update": ("finance", "Edit draft journal entries"),
    "finance.journal.delete": ("finance", "Delete draft journal entries"),
    "finance.journal.post": ("finance", "Post journal entries to the general ledger"),
    "finance.journal.reverse": ("finance", "Reverse posted journal entries"),
    "finance.budget.manage": ("finance", "Create, edit, delete, and submit budgets for approval"),
    "finance.budget.approve": ("finance", "Approve or reject submitted budgets"),
    "finance.depreciation.manage": ("finance", "Manage asset depreciation schedules and posting"),
    "finance.bank_reconciliation.manage": ("finance", "Import statements and reconcile bank accounts"),
}

# Codes for routers/public_api.py's API-key-scoped endpoints. These are not
# granted to any system Role — a school picks them explicitly per API key
# (see routers/integrations.py's create_api_key), independent of staff RBAC.
PUBLIC_API_PERMISSIONS: dict[str, tuple[str, str]] = {
    "students.record.view": ("public_api", "View student records (public API)"),
    "payments.status.view": ("public_api", "View payment/transaction status (public API)"),
    "fees.status.view": ("public_api", "View student fee/invoice records (public API)"),
    "grades.record.view": ("public_api", "View student grade records (public API)"),
    "attendance.record.view": ("public_api", "View student attendance records (public API)"),
    "staff.record.view": ("public_api", "View staff directory, excluding payroll data (public API)"),
    "classes.record.view": ("public_api", "View classes (public API)"),
    "announcements.record.view": ("public_api", "View published announcements (public API)"),
    "attendance.record.manage": ("public_api", "Record student attendance (public API write)"),
    "staff_attendance.record.manage": ("public_api", "Record staff attendance (public API write) — biometric devices, etc."),
    "assignments.record.manage": ("public_api", "Push assignments and submissions (public API write) — LMS integrations"),
    "grades.record.manage": ("public_api", "Record student grades (public API write)"),
    "students.record.manage": ("public_api", "Create student records (public API write)"),
    "staff.record.manage": ("public_api", "Create staff records (public API write)"),
    "fees.status.manage": ("public_api", "Create fee/invoice records (public API write)"),
    "classes.record.manage": ("public_api", "Create classes (public API write)"),
    "announcements.record.manage": ("public_api", "Publish announcements (public API write)"),
    "timetable.record.view": ("public_api", "View class timetables (public API)"),
    "timetable.record.manage": ("public_api", "Create timetable entries (public API write)"),
    "discipline.record.view": ("public_api", "View student discipline incidents (public API)"),
    "discipline.record.manage": ("public_api", "Report discipline incidents (public API write)"),
    "library.catalog.view": ("public_api", "View library catalog (public API)"),
    "library.loan.manage": ("public_api", "Check out library book copies (public API write)"),
    "tickets.record.view": ("public_api", "View support tickets (public API)"),
    "tickets.record.manage": ("public_api", "Open support tickets (public API write)"),
    "transport.record.view": ("public_api", "View bus routes (public API)"),
    "transport.record.manage": ("public_api", "Enroll students on bus routes (public API write)"),
    "hostel.record.view": ("public_api", "View hostel rooms (public API)"),
    "hostel.record.manage": ("public_api", "Allocate hostel rooms to students (public API write)"),
    "admissions.record.view": ("public_api", "View admissions applicants (public API)"),
    "admissions.record.manage": ("public_api", "Create admissions applicants (public API write)"),
    "alumni.record.view": ("public_api", "View alumni records (public API)"),
    "alumni.record.manage": ("public_api", "Create alumni records (public API write)"),
    "curriculum.record.view": ("public_api", "View curriculum topics and lesson notes (public API)"),
    "curriculum.record.manage": ("public_api", "Push lesson notes and update topic coverage (public API write)"),
    "tracks.record.view": ("public_api", "View subject/elective tracks (public API)"),
    "tracks.record.manage": ("public_api", "Enroll a student in a track (public API write)"),
    "academic_calendar.record.view": ("public_api", "View academic years and calendar events (public API)"),
    "academic_calendar.record.manage": ("public_api", "Push calendar events (public API write)"),
    "exams.record.view": ("public_api", "View internal exam sessions and seating (public API)"),
    "exams.record.manage": ("public_api", "Push an internal exam schedule (public API write)"),
    "exam_papers.record.view": ("public_api", "View exam papers (public API)"),
    "exam_papers.record.manage": ("public_api", "Push question-bank items (public API write)"),
    "exam_marks.record.view": ("public_api", "View a student's exam component marks (public API)"),
    "exam_marks.record.manage": ("public_api", "Push exam component marks (public API write)"),
    "exam_remarks.record.view": ("public_api", "View exam remark/recheck requests (public API)"),
    "exam_malpractice.record.view": ("public_api", "View exam malpractice cases (public API)"),
    "exam_malpractice.record.manage": ("public_api", "Report an exam malpractice case (public API write)"),
    "exam_board.record.view": ("public_api", "View external exam board registrations and results (public API)"),
    "exam_board.record.manage": ("public_api", "Push external exam board results and index numbers (public API write)"),
}

# code -> (module, description) -- routers/hr_admin.py, hr_recruitment.py,
# hr_development.py, hr_overtime.py, leave_requests.py
HR_PERMISSIONS: dict[str, tuple[str, str]] = {
    "hr.benefit.create": ("hr", "Grant a staff benefit"),
    "hr.disciplinary_action.create": ("hr", "Record a disciplinary action against a staff member"),
    "hr.grievance.manage": ("hr", "View, assign, and resolve staff-submitted grievances"),
    "hr.exit.manage": ("hr", "Create/update staff exit records and interviews"),
    "hr.benefit_plan.manage": ("hr", "Create and edit benefit plans"),
    "hr.department.manage": ("hr", "Create and edit departments"),
    "hr.succession_plan.manage": ("hr", "Create and edit succession plans"),
    "hr.workforce_plan.manage": ("hr", "Create and sync workforce plans"),
    "hr.vacancy.manage": ("hr", "Create and edit job vacancies"),
    "hr.applicant.manage": ("hr", "Create applicants and update their pipeline status"),
    "hr.interview.manage": ("hr", "Schedule and record interviews"),
    "hr.offer.manage": ("hr", "Create offers and download offer letters"),
    "hr.applicant.hire": ("hr", "Convert a hired applicant into a staff record"),
    "hr.onboarding.manage": ("hr", "Create and update onboarding tasks"),
    "hr.training.manage": ("hr", "Record training and its impact"),
    "hr.certification.create": ("hr", "Record a staff certification"),
    "hr.overtime.approve": ("hr", "Approve or reject overtime claims"),
    "hr.overtime.post": ("hr", "Push an approved overtime claim into payroll"),
    "hr.leave_request.view": ("hr", "View and filter all leave requests"),
    "hr.leave_request.approve": ("hr", "Give final approval to a leave request"),
    "hr.leave_request.reject": ("hr", "Reject a leave request"),
    "hr.leave_request.revoke": ("hr", "Revoke a previously approved leave request"),
    "hr.leave_balance.view": ("hr", "View staff leave balances"),
    "hr.leave_balance.manage": ("hr", "Seed and adjust staff leave balances"),
}

# code -> (module, description) -- routers/payroll.py
PAYROLL_PERMISSIONS: dict[str, tuple[str, str]] = {
    "payroll.contract.manage": ("payroll", "Create, import, renew, and edit payroll contracts"),
    "payroll.contract.view": ("payroll", "View contracts nearing expiry"),
    "payroll.run.generate": ("payroll", "Generate a payroll run"),
    "payroll.run.approve": ("payroll", "Approve or reject a payroll run"),
    "payroll.run.post": ("payroll", "Post and disburse a payroll run"),
    "payroll.adjustment.create": ("payroll", "Create a payroll adjustment"),
    "payroll.adjustment.approve": ("payroll", "Approve a payroll adjustment"),
    "payroll.adjustment.delete": ("payroll", "Delete a pending payroll adjustment"),
    "payroll.loan.create": ("payroll", "Request a staff loan/advance"),
    "payroll.loan.approve": ("payroll", "Approve and disburse a staff loan"),
    "payroll.loan.reject": ("payroll", "Reject a staff loan request"),
    "payroll.leave_encashment.create": ("payroll", "Request a leave encashment payout"),
    "payroll.leave_encashment.approve": ("payroll", "Approve a leave encashment request"),
    "payroll.leave_encashment.reject": ("payroll", "Reject a leave encashment request"),
    "payroll.export.manage": ("payroll", "Export SSNIT, bank payment, and annual payroll summaries"),
}

# code -> (module, description) -- routers/attendance.py staff-attendance side
STAFF_ATTENDANCE_PERMISSIONS: dict[str, tuple[str, str]] = {
    "staff_attendance.roster.create": ("staff_attendance", "Mark staff attendance for a day"),
    "staff_attendance.roster.view": ("staff_attendance", "View and export the staff attendance roster"),
    "staff_attendance.roster.update": ("staff_attendance", "Correct staff attendance records"),
    "staff_attendance.pin.manage": ("staff_attendance", "Set a staff member's biometric PIN"),
    "staff_attendance.shift.manage": ("staff_attendance", "Create, edit, and deactivate work shifts"),
    "staff_attendance.shift.view": ("staff_attendance", "View work shifts"),
    "staff_attendance.shift.assign": ("staff_attendance", "Assign shifts to staff members"),
    "staff_attendance.settings.manage": ("staff_attendance", "Configure staff attendance settings"),
}

# code -> (module, description) -- routers/grades.py, classes.py, curriculum.py,
# tracks.py, academic_calendar.py
ACADEMICS_PERMISSIONS: dict[str, tuple[str, str]] = {
    "academics.mastery_record.create": ("academics", "Record a mastery-level assessment"),
    "academics.grade.create": ("academics", "Record grades for students"),
    "academics.grade.update": ("academics", "Edit a recorded grade"),
    "academics.grade.delete": ("academics", "Delete a recorded grade"),
    "academics.grade_scale.manage": ("academics", "Create legacy grade scale bands"),
    "academics.subject.manage": ("academics", "Create subjects and seed default subjects"),
    "academics.grading_scheme.manage": ("academics", "Create, edit, and delete grading schemes"),
    "academics.report_card.generate": ("academics", "Generate and download report cards"),
    "academics.report_card.approve": ("academics", "Approve report cards for release"),
    "academics.report_card.recall": ("academics", "Recall an approved report card"),
    "academics.class.manage": ("academics", "Create and edit classes"),
    "academics.class_subject.manage": ("academics", "Assign and remove subjects from a class"),
    "academics.waitlist.manage": ("academics", "Offer waitlist seats and cancel waitlist entries"),
    "academics.lesson_note.manage": ("academics", "Create and edit lesson notes"),
    "academics.topic.manage": ("academics", "Create and edit curriculum topics"),
    "academics.lesson_plan.manage": ("academics", "Create and edit lesson plans"),
    "academics.standard.manage": ("academics", "Create, edit, and delete curriculum standards"),
    "academics.topic_standard.manage": ("academics", "Link and unlink standards from topics"),
    "academics.curriculum.view": ("academics", "View teacher curriculum-coverage activity"),
    "academics.track.manage": ("academics", "Create and edit subject tracks"),
    "academics.track_subject.manage": ("academics", "Assign and remove subjects from a track"),
    "academics.track_student.manage": ("academics", "Enroll and remove students from a track"),
    "academics.calendar_year.manage": ("academics", "Create and edit academic years"),
    "academics.calendar_event.manage": ("academics", "Create, edit, and delete calendar events"),
    "academics.calendar_feed_token.manage": ("academics", "Manage the school's calendar feed token"),
    "academics.calendar_rollover.manage": ("academics", "Preview and apply an academic-year rollover"),
}

# code -> (module, description) -- routers/attendance.py student-attendance side
ATTENDANCE_PERMISSIONS: dict[str, tuple[str, str]] = {
    "attendance.mark.create": ("attendance", "Mark student attendance"),
    "attendance.class_roster.view": ("attendance", "View a class's attendance roster with contact info"),
    "attendance.mark.update": ("attendance", "Correct a student attendance record"),
}

# code -> (module, description) -- routers/exams.py, exam_papers.py,
# exam_marks.py, exam_remarks.py, exam_malpractice.py
EXAMS_PERMISSIONS: dict[str, tuple[str, str]] = {
    "exams.session.manage": ("exams", "Create, edit, and delete exam sessions"),
    "exams.session.publish": ("exams", "Publish or unpublish exam results"),
    "exams.schedule.manage": ("exams", "Create, edit, and delete exam schedules"),
    "exams.seating.manage": ("exams", "Generate and edit exam seating"),
    "exams.invigilator.manage": ("exams", "Assign and remove invigilators"),
    "exams.paper_question.manage": ("exams", "Create, edit, and delete question-bank items"),
    "exams.paper_question.view": ("exams", "View the question bank"),
    "exams.paper.manage": ("exams", "Create, edit, delete, and compose exam papers"),
    "exams.paper.view": ("exams", "View exam papers"),
    "exams.paper.submit": ("exams", "Submit an exam paper for moderation"),
    "exams.paper.moderate": ("exams", "Approve or reject a submitted exam paper"),
    "exams.component.manage": ("exams", "Create, edit, and delete exam components"),
    "exams.marks.manage": ("exams", "Record exam component marks"),
    "exams.marks.view": ("exams", "View exam component marks"),
    "exams.remark.request": ("exams", "Request or cancel an exam remark/recheck"),
    "exams.remark.review": ("exams", "Review and resolve a remark request"),
    "exams.malpractice.create": ("exams", "Report an exam malpractice case"),
    "exams.malpractice.view": ("exams", "View exam malpractice cases"),
    "exams.malpractice.resolve": ("exams", "Investigate and resolve a malpractice case"),
}

# code -> (module, description) -- routers/exam_board.py
EXAM_BOARD_PERMISSIONS: dict[str, tuple[str, str]] = {
    "exam_board.registration.view": ("exam_board", "View board exam registrations"),
    "exam_board.registration.manage": ("exam_board", "Create, edit, and delete board exam registrations"),
    "exam_board.registration.pay": ("exam_board", "Record a board fee payment and download hall tickets"),
    "exam_board.result.view": ("exam_board", "View board exam results"),
    "exam_board.result.manage": ("exam_board", "Record and bulk-import board exam results"),
    "exam_board.seating.view": ("exam_board", "View board exam seating assignments"),
    "exam_board.seating.manage": ("exam_board", "Assign and edit board exam seating"),
    "exam_board.invigilation.view": ("exam_board", "View board exam invigilation duties"),
    "exam_board.invigilation.manage": ("exam_board", "Create, edit, and delete board exam invigilation duties"),
}

# UserRole -> permission codes granted to that role's system Role.
# Mirrors the current require_roles(...) combinations exactly:
# SUPER_ADMIN and SCHOOL_ADMIN appear in every finance require_roles() call
# in the pilot scope; HR appears only where noted per-code below.
_ADMIN_CODES = list(FINANCE_PERMISSIONS.keys())
_HR_CODES = [
    "finance.coa.create",
    "finance.vendor.manage",
    "finance.expense.create",
    "finance.expense.update",
    "finance.journal.create",
    "finance.journal.update",
    "finance.journal.delete",
    "finance.journal.post",
    "finance.journal.reverse",
    "finance.budget.manage",
    "finance.depreciation.manage",
    "finance.bank_reconciliation.manage",
]

# --- HR cluster: routers/hr_admin.py, hr_recruitment.py, hr_development.py,
# hr_overtime.py, leave_requests.py, payroll.py, attendance.py (staff side) ---
# Codes every HR-role staff member gets. Deliberately excludes the
# admin-only maker/checker codes below it (hire_applicant, leave approve/
# revoke, payroll run.post/adjustment.approve/loan.approve/leave_encashment.approve)
# -- these asymmetries mirror the original require_roles(...) tuples exactly.
_HR_STAFF_CODES = [
    "hr.benefit.create", "hr.disciplinary_action.create", "hr.grievance.manage", "hr.exit.manage",
    "hr.benefit_plan.manage", "hr.department.manage", "hr.succession_plan.manage",
    "hr.workforce_plan.manage", "hr.vacancy.manage", "hr.applicant.manage",
    "hr.interview.manage", "hr.offer.manage",
    "hr.onboarding.manage", "hr.training.manage", "hr.certification.create",
    "hr.overtime.approve", "hr.overtime.post",
    "hr.leave_request.view", "hr.leave_request.reject",
    "hr.leave_balance.view", "hr.leave_balance.manage",
    "payroll.contract.manage", "payroll.contract.view", "payroll.run.generate",
    "payroll.run.approve",  # (SCHOOL_ADMIN, HR) only in the source -- HR gets it, SUPER_ADMIN does not
    "payroll.adjustment.create", "payroll.adjustment.delete",
    "payroll.loan.create", "payroll.loan.reject",
    "payroll.leave_encashment.create", "payroll.leave_encashment.reject",
    "payroll.export.manage",
    "staff_attendance.roster.create", "staff_attendance.roster.view", "staff_attendance.roster.update",
    "staff_attendance.pin.manage", "staff_attendance.shift.manage", "staff_attendance.shift.view",
    "staff_attendance.shift.assign", "staff_attendance.settings.manage",
]
# SCHOOL_ADMIN: everything HR gets, plus the admin-only maker/checker codes.
_HR_ADMIN_CODES_SCHOOL_ADMIN = _HR_STAFF_CODES + [
    "hr.applicant.hire",
    "hr.leave_request.approve", "hr.leave_request.revoke",
    "payroll.run.post", "payroll.adjustment.approve", "payroll.loan.approve",
    "payroll.leave_encashment.approve",
]
# SUPER_ADMIN: same as SCHOOL_ADMIN except payroll.run.approve -- a genuine
# (SCHOOL_ADMIN, HR)-only combo (payroll.py:678,704, confirmed against
# source, docstring says so explicitly). See also exams.remark.request
# below for the one Academics-side exclusion (PARENT/STUDENT only).
_HR_ADMIN_CODES_SUPER_ADMIN = [c for c in _HR_ADMIN_CODES_SCHOOL_ADMIN if c != "payroll.run.approve"]

# --- Academics cluster: routers/grades.py, classes.py, curriculum.py,
# tracks.py, academic_calendar.py, exams.py, exam_papers.py, exam_marks.py,
# exam_remarks.py, exam_malpractice.py, exam_board.py, attendance.py (student side) ---
_ACADEMICS_ADMIN_CODES = [
    c for c in (
        list(ACADEMICS_PERMISSIONS.keys())
        + list(ATTENDANCE_PERMISSIONS.keys())
        + list(EXAMS_PERMISSIONS.keys())
        + list(EXAM_BOARD_PERMISSIONS.keys())
    )
    if c != "exams.remark.request"
    # exams.remark.request is gated by require_roles(PARENT, STUDENT) only
    # (routers/exam_remarks.py) -- SUPER_ADMIN/SCHOOL_ADMIN were never in
    # that tuple, so granting it here would be a real, new capability,
    # not a behavior-preserving conversion. The one exception to "SUPER_ADMIN
    # and SCHOOL_ADMIN get every Academics code."
]
_ACADEMICS_TEACHER_CODES = [
    "academics.mastery_record.create", "academics.grade.create", "academics.grade.update",
    "academics.grade.delete", "academics.report_card.generate",
    "academics.lesson_note.manage", "academics.topic.manage", "academics.lesson_plan.manage",
    "academics.topic_standard.manage",
    # NOT academics.standard.manage -- CurriculumStandard rows are school-wide
    # framework/reference data (GES strand codes etc.), not per-class teacher
    # content like topics/lesson notes/plans. A teacher can still link an
    # existing standard to their own topic (topic_standard.manage, TeacherAssignment-
    # scoped) and can still view standards (list_standards has no permission
    # gate at all) -- they just can't create/edit/delete the standards
    # themselves, which would affect every other teacher's topic-standard
    # links school-wide (TopicStandardLink cascades on standard delete).
    "exams.paper_question.manage", "exams.paper_question.view", "exams.paper.manage",
    "exams.paper.view", "exams.paper.submit",
    "exams.component.manage", "exams.marks.manage", "exams.marks.view",
    "exams.remark.review", "exams.malpractice.create", "exams.malpractice.view",
    "attendance.mark.create", "attendance.class_roster.view", "attendance.mark.update",
]  # NOT exams.session/schedule/seating/invigilator.* (no TEACHER in exams.py's WRITE_ROLES),
   # NOT any exam_board.* code, NOT academics.class/track/calendar_*.*/grade_scale/subject/grading_scheme
_ACADEMICS_REGISTRAR_CODES = [
    "exams.remark.review", "exams.malpractice.create", "exams.malpractice.view",
]  # NOT exams.malpractice.resolve -- RESOLVE_ROLES is narrower than STAFF_ROLES
_ACADEMICS_REQUESTER_CODES = ["exams.remark.request"]  # PARENT and STUDENT

SYSTEM_ROLE_GRANTS: dict[UserRole, list[str]] = {
    UserRole.SUPER_ADMIN: _ADMIN_CODES + _HR_ADMIN_CODES_SUPER_ADMIN + _ACADEMICS_ADMIN_CODES,
    UserRole.SCHOOL_ADMIN: _ADMIN_CODES + _HR_ADMIN_CODES_SCHOOL_ADMIN + _ACADEMICS_ADMIN_CODES,
    UserRole.HR: _HR_CODES + _HR_STAFF_CODES,
    UserRole.TEACHER: _ACADEMICS_TEACHER_CODES,
    UserRole.STUDENT: _ACADEMICS_REQUESTER_CODES,
    UserRole.PARENT: _ACADEMICS_REQUESTER_CODES,
    # Every other role currently has zero require_roles() access in any
    # migrated module — seeded with an empty grant list so the system Role
    # row still exists (permission_service resolves an unseeded role to an
    # empty set anyway, but an explicit empty-grant row is easier for a
    # school admin to find and layer school-wide custom grants onto later).
    UserRole.SECURITY_OFFICER: [],
    UserRole.DRIVER: [],
    UserRole.NURSE: [],
    UserRole.REGISTRAR: _ACADEMICS_REGISTRAR_CODES,
    UserRole.STOREKEEPER: [],
    UserRole.CANTEEN_STAFF: [],
    # Previously missing from this mapping entirely (not even an empty
    # entry) despite existing on the UserRole enum -- added now for the
    # same reason every other zero-access role gets an explicit empty list.
    UserRole.COUNSELOR: [],
    UserRole.SEN_COORDINATOR: [],
    UserRole.SAFEGUARDING_LEAD: [],
}


async def seed_permissions():
    async with async_session() as session:
        code_to_id: dict[str, str] = {}
        all_permissions = {
            **FINANCE_PERMISSIONS, **PUBLIC_API_PERMISSIONS,
            **HR_PERMISSIONS, **PAYROLL_PERMISSIONS, **STAFF_ATTENDANCE_PERMISSIONS,
            **ACADEMICS_PERMISSIONS, **ATTENDANCE_PERMISSIONS,
            **EXAMS_PERMISSIONS, **EXAM_BOARD_PERMISSIONS,
        }
        for code, (module, description) in all_permissions.items():
            existing = (
                await session.execute(select(Permission).where(Permission.code == code))
            ).scalar_one_or_none()
            if existing:
                code_to_id[code] = existing.id
                continue
            perm = Permission(code=code, module=module, description=description)
            session.add(perm)
            await session.flush()
            code_to_id[code] = perm.id
            logger.info(f"Created permission: {code}")

        for user_role, granted_codes in SYSTEM_ROLE_GRANTS.items():
            role = (
                await session.execute(
                    select(Role).where(
                        Role.is_system == True,  # noqa: E712
                        Role.school_id.is_(None),
                        Role.name == user_role.value,
                    )
                )
            ).scalar_one_or_none()
            if role is None:
                role = Role(school_id=None, name=user_role.value, is_system=True)
                session.add(role)
                await session.flush()
                logger.info(f"Created system role: {user_role.value}")

            existing_grants = set(
                (
                    await session.execute(
                        select(RolePermission.permission_id).where(RolePermission.role_id == role.id)
                    )
                ).scalars().all()
            )
            for code in granted_codes:
                permission_id = code_to_id[code]
                if permission_id in existing_grants:
                    continue
                session.add(RolePermission(role_id=role.id, permission_id=permission_id))
                logger.info(f"Granted {code} -> {user_role.value}")

        await session.commit()
    logger.info("Permission seeding complete.")


if __name__ == "__main__":
    asyncio.run(seed_permissions())
