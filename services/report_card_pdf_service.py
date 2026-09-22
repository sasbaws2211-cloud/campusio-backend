"""Report Card PDF Generation Service"""
from jinja2 import Environment, FileSystemLoader
from jinja2.sandbox import SandboxedEnvironment
from xhtml2pdf import pisa
from datetime import datetime
from io import BytesIO
from typing import Optional, Tuple
import os
import re
from pathlib import Path

from utils.grade_scale import get_letter_grade as _shared_get_letter_grade
from services.grading_service import match_scale as _match_grading_scale
from services.grading_service import build_subject_weights as _match_grading_weights

# Termly reports follow the GES School Based Assessment format: CLASS SCORE
# (SBA: classwork/homework/quiz/mid-term/project) + EXAM SCORE (end-of-term),
# TOTAL out of 100 graded on the 1-9 GES scale. 50/50 is the default split —
# any caller that resolves a school-configured GradingScheme's ca_weight/
# exam_weight (services/grading_service.py::match_weights) and passes it as
# `weights` overrides that per subject; a caller that doesn't pass `weights`
# gets exactly the previous hardcoded 50/50 behavior, unchanged.
GES_EXAM_ASSESSMENT_TYPES = {"end_of_term"}
DEFAULT_CA_EXAM_WEIGHTS = (50.0, 50.0)


def compute_subject_ges_totals(grades, weights: Optional[dict] = None) -> dict:
    """Bucket raw Grade rows into per-subject GES class-score + exam-score totals.

    This is the single source for a student's per-subject/overall percentage, shared by
    the report-card generation endpoint (which persists ReportCard.total_score/average_score)
    and the PDF/preview renderer — previously each recomputed it differently (a plain sum of
    all assessment scores vs. this split), so the number shown in the report-card list
    and the number on the downloaded PDF could disagree.

    `weights`, if given, is {subject_id: (ca_weight, exam_weight)} from
    services/grading_service.py::match_weights — a subject missing from the
    dict (or weights=None entirely) falls back to DEFAULT_CA_EXAM_WEIGHTS
    (50/50), so every caller that doesn't opt in is unaffected.

    Returns: {subject_id: {"class_score": float, "exam_score": float, "total_score": float}}
    """
    buckets: dict = {}
    for grade in grades:
        subject_id = grade.subject_id
        if subject_id not in buckets:
            buckets[subject_id] = {"sba_score": 0.0, "sba_max": 0.0, "exam_score": 0.0, "exam_max": 0.0}
        atype = grade.assessment_type.value if hasattr(grade.assessment_type, 'value') else str(grade.assessment_type)
        bucket = buckets[subject_id]
        # weight lets a grade count more/less than 1x toward the bucket total —
        # e.g. a mid-term weighted 2x counts twice as much as a single classwork.
        weight = grade.weight if grade.weight else 1.0
        if atype in GES_EXAM_ASSESSMENT_TYPES:
            bucket["exam_score"] += grade.score * weight
            bucket["exam_max"] += grade.max_score * weight
        else:
            bucket["sba_score"] += grade.score * weight
            bucket["sba_max"] += grade.max_score * weight

    totals = {}
    for subject_id, data in buckets.items():
        ca_weight, exam_weight = (weights or {}).get(subject_id, DEFAULT_CA_EXAM_WEIGHTS)
        class_score_scaled = round((data["sba_score"] / data["sba_max"]) * ca_weight, 1) if data["sba_max"] > 0 else 0.0
        exam_score_scaled = round((data["exam_score"] / data["exam_max"]) * exam_weight, 1) if data["exam_max"] > 0 else 0.0
        totals[subject_id] = {
            "class_score": class_score_scaled,
            "exam_score": exam_score_scaled,
            "total_score": round(class_score_scaled + exam_score_scaled, 1),
            # False when either half has no grades recorded at all for this
            # subject/term — the missing half still contributes 0 above (a
            # school's configured split has no other reasonable number to
            # show on the printed report card), so an incomplete subject's
            # total_score is capped near the graded half's weight rather than
            # reflecting only the half that was actually graded. Callers that
            # persist/display this number (routers/grades.py::generate_report_card)
            # surface this flag separately so a human notices "exam not yet
            # entered" instead of reading a low total as the student's real
            # performance.
            "data_complete": data["sba_max"] > 0 and data["exam_max"] > 0,
        }
    return totals


def compute_overall_ges_score(grades, weights: Optional[dict] = None) -> Tuple[float, float]:
    """Returns (total_score, average_score) for a report card: the sum and the average of
    each subject's GES total (out of 100) — the same number the PDF shows as overall_average.
    See compute_subject_ges_totals for what `weights` does."""
    subject_totals = compute_subject_ges_totals(grades, weights=weights)
    values = [s["total_score"] for s in subject_totals.values()]
    total_score = round(sum(values), 1)
    average_score = round(sum(values) / len(values), 1) if values else 0.0
    return total_score, average_score


class ReportCardPDFService:
    """Service for generating report card PDFs"""
    
    def __init__(self):
        # Setup Jinja2 environment for file-based templates
        template_dir = Path(__file__).parent.parent / "templates"
        self.env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=True
        )
        self.default_template_name = "report_card.html"
        # Separate sandboxed environment for school-admin-authored
        # template_html (models.report_template.ReportTemplate.html_content)
        # -- untrusted template SOURCE, unlike the file-based template above
        # which is developer-controlled. Compiling untrusted source with a
        # plain Template()/Environment.from_string() is a classic Jinja2
        # SSTI-to-RCE vector (the standard
        # ''.__class__.__mro__[1].__subclasses__() gadget chain reaches
        # arbitrary Python objects regardless of autoescape, which only
        # escapes variable OUTPUT). Jinja2's own SandboxedEnvironment blocks
        # unsafe dunder-attribute access at render time -- same fix already
        # applied to services/certificate_pdf_service.py for the identical
        # school-admin-authored-template shape; this sibling file was never
        # patched until now.
        self._sandboxed_env = SandboxedEnvironment(autoescape=True)

    def render_html(self, report_data: dict, template_html: str = None) -> str:
        """
        Render report card as HTML (for preview in modal)

        Args:
            report_data: Dictionary containing all report card information
            template_html: Custom HTML template (Jinja2) string. If None, uses default file

        Returns:
            Rendered HTML string
        """
        try:
            if template_html:
                # Log the available keys for debugging
                import logging
                logger = logging.getLogger(__name__)
                logger.info(f"Available template variables: {list(report_data.keys())}")

                template = self._sandboxed_env.from_string(template_html)
                html_content = template.render(**report_data)
            else:
                template = self.env.get_template(self.default_template_name)
                html_content = template.render(**report_data)
            
            return html_content
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Template rendering failed: {str(e)}")
            logger.error(f"Available data keys: {list(report_data.keys()) if report_data else 'Empty data'}")
            raise Exception(f"Failed to render report card HTML: {str(e)}")
    
    def generate_pdf(self, report_data: dict, template_html: str = None) -> bytes:
        """
        Generate PDF from report card data
        
        Args:
            report_data: Dictionary containing all report card information
            template_html: Custom HTML template (Jinja2) string. If None, uses default file
            
        Returns:
            PDF bytes
        """
        try:
            # Render HTML first
            html_content = self.render_html(report_data, template_html=template_html)
            
            # Ensure proper XHTML structure for xhtml2pdf
            html_content = self._ensure_xhtml_compliance(html_content)
            
            # Generate PDF using xhtml2pdf
            pdf_buffer = BytesIO()
            pisa_status = pisa.CreatePDF(
                html_content,
                dest=pdf_buffer,
                encoding='UTF-8'
            )
            
            if pisa_status.err:
                raise Exception(f"PDF generation error: {pisa_status.err}")
            
            pdf_buffer.seek(0)
            pdf_bytes = pdf_buffer.getvalue()
            
            if not pdf_bytes:
                raise Exception("PDF generation produced empty output")
            
            return pdf_bytes
        except Exception as e:
            raise Exception(f"Failed to generate report card PDF: {str(e)}")
    
    @staticmethod
    def _ensure_xhtml_compliance(html_content: str) -> str:
        """
        Ensure HTML is XHTML-compliant for xhtml2pdf
        xhtml2pdf requires proper XHTML structure
        """
        # Add DOCTYPE if missing
        if '<!DOCTYPE' not in html_content and '<html' in html_content:
            html_content = '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">\n' + html_content
        
        # Ensure <html> tag has proper attributes
        if '<html>' in html_content:
            html_content = html_content.replace('<html>', '<html xmlns="http://www.w3.org/1999/xhtml">')
        
        # Fix unclosed img, br, hr tags for XHTML
        import re
        html_content = re.sub(r'<img([^>]*)>', r'<img\1 />', html_content)
        html_content = re.sub(r'<br>', r'<br />', html_content)
        html_content = re.sub(r'<hr>', r'<hr />', html_content)
        
        # Wrap content in proper HTML structure if missing
        if '<html' not in html_content.lower():
            html_content = f'<html><head><meta charset="UTF-8"/></head><body>{html_content}</body></html>'
        
        return html_content
    
    @staticmethod
    def format_grade_data(report_card, grades, subjects_map, student, academic_term_name: str = None, class_average: float = None, mastery_records: list = None, grading_schemes: list = None, class_level: str = None) -> dict:
        """
        Format report card and grades into template data

        Args:
            report_card: ReportCard model instance
            grades: List of Grade model instances
            subjects_map: Dictionary of subject_id -> Subject model
            student: Student model instance or dictionary with student data
            academic_term_name: Name of the academic term
            mastery_records: Optional list of standards-based mastery records
                (dicts, e.g. from StandardMasteryRecord.model_dump() with
                standard_code/standard_title enrichment) — parallel/optional
                alongside the percentage-based subjects list above, never a
                replacement for it. Defaults to an empty list when omitted,
                so every existing caller stays backward-compatible.
            grading_schemes: Optional list from services.grading_service.get_school_schemes()
                — the school's configured grading scales (models.grade.GradingScheme).
                Each subject's letter grade is matched against the most specific
                scheme for (class_level, that subject_id); the overall/average
                grade uses the best match for class_level alone. Omit (or pass
                an empty list) to fall back to the built-in GES scale everywhere,
                same as before this parameter existed.
            class_level: The student's class level (models.classroom.ClassLevel
                value), used together with grading_schemes above.

        Returns:
            Dictionary ready for template rendering
        """
        from datetime import datetime
        
        # Helper function to get values from object or dict
        def get_value(obj, key, default):
            if isinstance(obj, dict):
                return obj.get(key, default)
            else:
                return getattr(obj, key, default)
        
        # ── GES SBA split (shared with the report-card generation endpoint,
        # so the total/average shown here matches what's persisted there) ────
        schemes = grading_schemes or []
        subject_weights = _match_grading_weights(schemes, class_level, {g.subject_id for g in grades})
        subject_totals_by_id = compute_subject_ges_totals(grades, weights=subject_weights)
        subject_name_by_id = {}
        subject_code_by_id = {}
        for grade in grades:
            subject = subjects_map.get(grade.subject_id)
            subject_name_by_id[grade.subject_id] = subject.name if subject else "Unknown"
            subject_code_by_id[grade.subject_id] = subject.code if subject and hasattr(subject, 'code') else "N/A"

        subjects_list = []
        subject_totals = []
        for subject_id, totals in sorted(subject_totals_by_id.items(), key=lambda kv: subject_name_by_id.get(kv[0], "Unknown")):
            total_100 = totals["total_score"]
            subject_scale = _match_grading_scale(schemes, class_level, subject_id)
            grade_info = ReportCardPDFService._get_letter_grade(total_100, scale=subject_scale)
            subject_totals.append(total_100)

            subjects_list.append({
                "subject_name": subject_name_by_id.get(subject_id, "Unknown"),
                "subject_code": subject_code_by_id.get(subject_id, "N/A"),
                "class_score": totals["class_score"],  # out of 50
                "exam_score": totals["exam_score"],    # out of 50
                "total_score": total_100,              # out of 100
                "grade": grade_info["grade"],          # GES 1-9
                "remarks": grade_info["description"],
            })

        overall_percentage = round(sum(subject_totals) / len(subject_totals), 1) if subject_totals else 0
        overall_scale = _match_grading_scale(schemes, class_level, subject_id=None)
        overall_grade_info = ReportCardPDFService._get_letter_grade(overall_percentage, scale=overall_scale)

        # Attendance: prefer explicit day counts (GES format shows "x out of y")
        days_present = get_value(report_card, 'days_present', None)
        days_total = get_value(report_card, 'days_total', None)
        if days_present is not None and days_total:
            attendance_display = f"{days_present} out of {days_total}"
        else:
            pct = get_value(report_card, 'attendance_percentage', None)
            attendance_display = f"{pct}%" if pct is not None else "Not Recorded"

        return {
            "school_name": get_value(student, 'school_name', 'School Name Not Available'),
            "student_name": get_value(student, 'first_name', 'Name not Available'),
            "student_id": get_value(student, 'id', 'Id not Available'),
            "class_name": get_value(student, 'class_name', 'Not Assigned'),
            "academic_term": academic_term_name or "Term 1, 2026",
            "generated_date": datetime.utcnow().strftime("%d %B %Y"),
            "attendance_display": attendance_display,
            "attendance_percentage": get_value(report_card, 'attendance_percentage', 'Not Available'),
            "class_size": get_value(report_card, 'class_size', 'Not Available'),
            "position": get_value(report_card, 'position', 'Not Available'),
            "class_average": class_average if class_average is not None else 'Not Available',
            "overall_average": overall_percentage,
            "overall_grade": overall_grade_info["grade"],
            "overall_description": overall_grade_info["description"],
            "subjects": subjects_list,
            "class_teacher_remarks": get_value(report_card, 'class_teacher_remarks', None) or "",
            "head_teacher_remarks": get_value(report_card, 'head_teacher_remarks', None) or "",
            # GES SBA footer blocks
            "attitude": get_value(report_card, 'attitude', None) or "",
            "conduct": get_value(report_card, 'conduct', None) or "",
            "interest": get_value(report_card, 'interest', None) or "",
            "vacation_date": get_value(report_card, 'vacation_date', None) or "",
            "reopening_date": get_value(report_card, 'reopening_date', None) or "",
            "promoted_to": get_value(report_card, 'promoted_to', None) or "",
            "mastery_records": mastery_records or [],
        }
    
    @staticmethod
    def _get_letter_grade(percentage: float, scale: list = None) -> dict:
        """Convert percentage to grade band (single shared source — see utils/grade_scale.py).
        scale defaults to the built-in GES scale; pass a school-configured one
        via services/grading_service.py's match_scale()."""
        return _shared_get_letter_grade(percentage, scale=scale)
