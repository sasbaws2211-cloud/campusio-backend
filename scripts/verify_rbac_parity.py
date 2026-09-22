"""Parity check: for every new HR/Academics permission code, the set of
system Roles that hold it in the DB must exactly match the role tuple the
code was derived from in routers/*.py (transcribed by hand below as the
ground truth, cross-checked against source during conversion). Run after
seeding, and again after any change to SYSTEM_ROLE_GRANTS, to catch drift."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from database import async_session
from models.rbac import Permission, Role, RolePermission

SA, SCA, HR, TEACHER, PARENT, STUDENT, REGISTRAR = (
    "super_admin", "school_admin", "hr", "teacher", "parent", "student", "registrar",
)

EXPECTED = {
    # hr_admin.py
    "hr.benefit.create": {SA, SCA, HR}, "hr.disciplinary_action.create": {SA, SCA, HR},
    "hr.exit.manage": {SA, SCA, HR}, "hr.benefit_plan.manage": {SA, SCA, HR},
    "hr.department.manage": {SA, SCA, HR}, "hr.succession_plan.manage": {SA, SCA, HR},
    "hr.workforce_plan.manage": {SA, SCA, HR},
    # hr_recruitment.py
    "hr.vacancy.manage": {SA, SCA, HR}, "hr.applicant.manage": {SA, SCA, HR},
    "hr.interview.manage": {SA, SCA, HR}, "hr.offer.manage": {SA, SCA, HR},
    "hr.applicant.hire": {SA, SCA},
    # hr_development.py
    "hr.onboarding.manage": {SA, SCA, HR}, "hr.training.manage": {SA, SCA, HR},
    "hr.certification.create": {SA, SCA, HR},
    # hr_overtime.py
    "hr.overtime.approve": {SA, SCA, HR}, "hr.overtime.post": {SA, SCA, HR},
    # leave_requests.py
    "hr.leave_request.view": {SA, SCA, HR}, "hr.leave_request.approve": {SA, SCA},
    "hr.leave_request.reject": {SA, SCA, HR}, "hr.leave_request.revoke": {SA, SCA},
    "hr.leave_balance.view": {SA, SCA, HR}, "hr.leave_balance.manage": {SA, SCA, HR},
    # payroll.py -- payroll.run.approve is the one real SUPER_ADMIN exclusion
    "payroll.contract.manage": {SA, SCA, HR}, "payroll.contract.view": {SA, SCA, HR},
    "payroll.run.generate": {SA, SCA, HR}, "payroll.run.approve": {SCA, HR},
    "payroll.run.post": {SA, SCA}, "payroll.adjustment.create": {SA, SCA, HR},
    "payroll.adjustment.approve": {SA, SCA}, "payroll.adjustment.delete": {SA, SCA, HR},
    "payroll.loan.create": {SA, SCA, HR}, "payroll.loan.approve": {SA, SCA},
    "payroll.loan.reject": {SA, SCA, HR}, "payroll.leave_encashment.create": {SA, SCA, HR},
    "payroll.leave_encashment.approve": {SA, SCA}, "payroll.leave_encashment.reject": {SA, SCA, HR},
    "payroll.export.manage": {SA, SCA, HR},
    # attendance.py staff side
    "staff_attendance.roster.create": {SA, SCA, HR}, "staff_attendance.roster.view": {SA, SCA, HR},
    "staff_attendance.roster.update": {SA, SCA, HR}, "staff_attendance.pin.manage": {SA, SCA, HR},
    "staff_attendance.shift.manage": {SA, SCA, HR}, "staff_attendance.shift.view": {SA, SCA, HR},
    "staff_attendance.shift.assign": {SA, SCA, HR}, "staff_attendance.settings.manage": {SA, SCA, HR},
    # attendance.py student side
    "attendance.mark.create": {SA, SCA, TEACHER}, "attendance.class_roster.view": {SA, SCA, TEACHER},
    "attendance.mark.update": {SA, SCA, TEACHER},
    # grades.py
    "academics.mastery_record.create": {SA, SCA, TEACHER}, "academics.grade.create": {SA, SCA, TEACHER},
    "academics.grade.update": {SA, SCA, TEACHER}, "academics.grade.delete": {SA, SCA, TEACHER},
    "academics.grade_scale.manage": {SA, SCA}, "academics.subject.manage": {SA, SCA},
    "academics.grading_scheme.manage": {SA, SCA}, "academics.report_card.generate": {SA, SCA, TEACHER},
    "academics.report_card.approve": {SA, SCA}, "academics.report_card.recall": {SA, SCA},
    # classes.py
    "academics.class.manage": {SA, SCA}, "academics.class_subject.manage": {SA, SCA},
    "academics.waitlist.manage": {SA, SCA},
    # curriculum.py
    "academics.lesson_note.manage": {SA, SCA, TEACHER}, "academics.topic.manage": {SA, SCA, TEACHER},
    "academics.lesson_plan.manage": {SA, SCA, TEACHER}, "academics.standard.manage": {SA, SCA, TEACHER},
    "academics.topic_standard.manage": {SA, SCA, TEACHER}, "academics.curriculum.view": {SA, SCA},
    # tracks.py
    "academics.track.manage": {SA, SCA}, "academics.track_subject.manage": {SA, SCA},
    "academics.track_student.manage": {SA, SCA},
    # academic_calendar.py
    "academics.calendar_year.manage": {SA, SCA}, "academics.calendar_event.manage": {SA, SCA},
    "academics.calendar_feed_token.manage": {SA, SCA}, "academics.calendar_rollover.manage": {SA, SCA},
    # exams.py -- no TEACHER anywhere in this file
    "exams.session.manage": {SA, SCA}, "exams.session.publish": {SA, SCA},
    "exams.schedule.manage": {SA, SCA}, "exams.seating.manage": {SA, SCA},
    "exams.invigilator.manage": {SA, SCA},
    # exam_papers.py
    "exams.paper_question.manage": {SA, SCA, TEACHER}, "exams.paper_question.view": {SA, SCA, TEACHER},
    "exams.paper.manage": {SA, SCA, TEACHER}, "exams.paper.view": {SA, SCA, TEACHER},
    "exams.paper.submit": {SA, SCA, TEACHER}, "exams.paper.moderate": {SA, SCA},
    # exam_marks.py
    "exams.component.manage": {SA, SCA, TEACHER}, "exams.marks.manage": {SA, SCA, TEACHER},
    "exams.marks.view": {SA, SCA, TEACHER},
    # exam_remarks.py
    "exams.remark.request": {PARENT, STUDENT}, "exams.remark.review": {SA, SCA, TEACHER, REGISTRAR},
    # exam_malpractice.py
    "exams.malpractice.create": {SA, SCA, TEACHER, REGISTRAR}, "exams.malpractice.view": {SA, SCA, TEACHER, REGISTRAR},
    "exams.malpractice.resolve": {SA, SCA},
    # exam_board.py -- fully admin-only, no exceptions
    "exam_board.registration.view": {SA, SCA}, "exam_board.registration.manage": {SA, SCA},
    "exam_board.registration.pay": {SA, SCA}, "exam_board.result.view": {SA, SCA},
    "exam_board.result.manage": {SA, SCA}, "exam_board.seating.view": {SA, SCA},
    "exam_board.seating.manage": {SA, SCA}, "exam_board.invigilation.view": {SA, SCA},
    "exam_board.invigilation.manage": {SA, SCA},
}


async def main():
    mismatches = []
    async with async_session() as session:
        for code, expected_roles in EXPECTED.items():
            perm = (await session.execute(select(Permission).where(Permission.code == code))).scalar_one_or_none()
            if not perm:
                mismatches.append((code, "MISSING PERMISSION ROW", expected_roles, set()))
                continue
            grants = (await session.execute(
                select(RolePermission).where(RolePermission.permission_id == perm.id)
            )).scalars().all()
            role_ids = [g.role_id for g in grants]
            actual_roles = set()
            if role_ids:
                roles = (await session.execute(
                    select(Role).where(Role.id.in_(role_ids), Role.is_system == True, Role.school_id.is_(None))
                )).scalars().all()
                actual_roles = {r.name for r in roles}
            if actual_roles != expected_roles:
                mismatches.append((code, "MISMATCH", expected_roles, actual_roles))

    print(f"Checked {len(EXPECTED)} permission codes.")
    if mismatches:
        print(f"\n{len(mismatches)} MISMATCHES:")
        for code, kind, expected, actual in mismatches:
            print(f"  {code} [{kind}]: expected={sorted(expected)} actual={sorted(actual)}")
    else:
        print("ALL GRANTS MATCH EXPECTED ROLE SETS -- parity confirmed.")


if __name__ == "__main__":
    asyncio.run(main())
