"""Hall Ticket PDF Generation Service"""
from jinja2 import Environment, FileSystemLoader
from xhtml2pdf import pisa
from datetime import datetime
from io import BytesIO
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class HallTicketPDFService:
    """Service for generating exam hall ticket PDFs from a registration data dict
    (see routers/exam_board.py's hall-ticket endpoint for the expected shape)."""

    def __init__(self):
        template_dir = Path(__file__).parent.parent / "templates"
        self.env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=True
        )
        self.default_template_name = "hall_ticket.html"

    @staticmethod
    def format_hall_ticket_data(data: dict) -> dict:
        return {
            "school_name": data.get("school_name", "School"),
            "student_name": data.get("student_name", "Unknown"),
            "student_id_code": data.get("student_id_code", ""),
            "date_of_birth": data.get("date_of_birth", ""),
            "gender": data.get("gender", ""),
            "exam_name": data.get("exam_name", ""),
            "exam_year": data.get("exam_year", ""),
            "index_number": data.get("index_number") or "Not yet issued",
            "subjects_registered": data.get("subjects_registered", ""),
            "exam_center": data.get("exam_center") or "Not yet assigned",
            "seat_number": data.get("seat_number") or "Not yet assigned",
            "generated_date": datetime.utcnow().strftime("%d %B %Y"),
        }

    def render_html(self, data: dict) -> str:
        template = self.env.get_template(self.default_template_name)
        return template.render(**self.format_hall_ticket_data(data))

    def generate_pdf(self, data: dict) -> bytes:
        """Generate hall ticket PDF bytes from a registration data dict."""
        try:
            html_content = self.render_html(data)

            pdf_buffer = BytesIO()
            pisa_status = pisa.CreatePDF(html_content, dest=pdf_buffer, encoding="UTF-8")

            if pisa_status.err:
                raise Exception(f"PDF generation error: {pisa_status.err}")

            pdf_buffer.seek(0)
            pdf_bytes = pdf_buffer.getvalue()

            if not pdf_bytes:
                raise Exception("PDF generation produced empty output")

            return pdf_bytes
        except Exception as e:
            logger.error(f"Failed to generate hall ticket PDF: {str(e)}")
            raise Exception(f"Failed to generate hall ticket PDF: {str(e)}")
