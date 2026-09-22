"""Compiles a student's APPROVED report cards across every term into a
single academic transcript PDF. Reuses CertificatePDFService's generic
template rendering (services/certificate_pdf_service.py) rather than a
dedicated PDF class — transcript.html is just one more template, same as
report_card.html and the certificate_*.html files.

Per-subject rows are recomputed live from models.grade.Grade the same way
routers/grades.py::download_report_card_pdf already does for a single
term's PDF — there is no stored per-subject breakdown table, by design
(see routers/grades.py's own comments)."""
from typing import Optional

from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.classroom import Class, Subject
from models.grade import Grade, ReportCard, ReportCardStatus
from models.school import AcademicTerm, School
from models.student import Student
from services.certificate_pdf_service import CertificatePDFService
from services.report_card_pdf_service import compute_subject_ges_totals
from services import grading_service
from utils.grade_scale import get_letter_grade


async def build_transcript_pdf(session: AsyncSession, student: Student) -> Optional[bytes]:
    report_cards_result = await session.execute(
        select(ReportCard).where(
            ReportCard.student_id == student.id,
            ReportCard.status == ReportCardStatus.APPROVED,
        ).order_by(ReportCard.generated_at)
    )
    report_cards = report_cards_result.scalars().all()
    if not report_cards:
        return None

    school = (await session.execute(select(School).where(School.id == student.school_id))).scalar_one_or_none()
    schemes = await grading_service.get_school_schemes(session, student.school_id)

    terms = []
    for report_card in report_cards:
        academic_term = (await session.execute(select(AcademicTerm).where(AcademicTerm.id == report_card.academic_term_id))).scalar_one_or_none()
        term_name = f"{academic_term.academic_year} — {academic_term.term.value.capitalize()} Term" if academic_term else "Unknown Term"

        class_obj = (await session.execute(select(Class).where(Class.id == report_card.class_id))).scalar_one_or_none()
        class_name = class_obj.name if class_obj else "—"

        grades = (await session.execute(
            select(Grade).where(Grade.student_id == student.id, Grade.academic_term_id == report_card.academic_term_id)
        )).scalars().all()

        class_level = class_obj.level if class_obj else None

        # Weighted per-subject totals via compute_subject_ges_totals — the
        # same function the report-card PDF uses, with the same
        # school-configured CA:exam split (falls back to 50/50), so these
        # transcript rows agree with what that PDF showed for the same
        # subject/term.
        subject_weights = grading_service.build_subject_weights(schemes, class_level, {g.subject_id for g in grades})
        subject_ges_totals = compute_subject_ges_totals(grades, weights=subject_weights)

        subject_ids = list(subject_ges_totals.keys())
        subjects_map = {}
        if subject_ids:
            subjects_result = await session.execute(select(Subject).where(Subject.id.in_(subject_ids)))
            subjects_map = {s.id: s for s in subjects_result.scalars().all()}

        subject_rows = []
        for subject_id, totals in subject_ges_totals.items():
            percentage = totals["total_score"]
            subject_scale = grading_service.match_scale(schemes, class_level, subject_id)
            letter = get_letter_grade(percentage, scale=subject_scale)
            subject_rows.append({
                "subject_name": subjects_map[subject_id].name if subject_id in subjects_map else "Unknown",
                "percentage": round(percentage, 1),
                "grade": letter["grade"],
                "description": letter["description"],
            })
        subject_rows.sort(key=lambda r: r["subject_name"])

        overall_scale = grading_service.match_scale(schemes, class_level)
        overall_letter = get_letter_grade(report_card.average_score, scale=overall_scale)
        terms.append({
            "term_name": term_name,
            "class_name": class_name,
            "total_score": report_card.total_score,
            "average_score": report_card.average_score,
            "position": report_card.position,
            "class_size": report_card.class_size,
            "overall_grade": overall_letter["grade"],
            "promotion_decision": report_card.promotion_decision,
            "subjects": subject_rows,
        })

    data = {
        "school_name": school.name if school else "School",
        "student_name": f"{student.first_name} {student.last_name}",
        "student_id_code": student.student_id,
        "terms": terms,
    }
    return CertificatePDFService().generate_pdf("transcript.html", data)
