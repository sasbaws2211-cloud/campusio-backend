"""Board Reporting Package PDF generation — bundles the existing
attendance/fee-collection/academic-performance/enrollment report builders
(services/analytics_reports_service.py) plus strategic-goals progress into
ONE Jinja2-rendered HTML document. One document with multiple sections, not
multiple PDFs merged (no PDF-merge library exists in this codebase).

Mirrors services/report_card_pdf_service.py's class shape exactly:
Environment(loader=FileSystemLoader(templates_dir), autoescape=True) in
__init__, a render_html() method doing template.render(**data), and a
generate_pdf() method calling xhtml2pdf's pisa.CreatePDF(html, dest=BytesIO()).
"""
from jinja2 import Environment, FileSystemLoader
from xhtml2pdf import pisa
from io import BytesIO
from pathlib import Path


class BoardPackPDFService:
    """Service for generating the board reporting package PDF."""

    def __init__(self):
        template_dir = Path(__file__).parent.parent / "templates"
        self.env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=True
        )
        self.template_name = "board_pack.html"

    def render_html(self, pack_data: dict) -> str:
        """Render the board pack as HTML (one section per report)."""
        try:
            template = self.env.get_template(self.template_name)
            return template.render(**pack_data)
        except Exception as e:
            raise Exception(f"Failed to render board pack HTML: {str(e)}")

    def generate_pdf(self, pack_data: dict) -> bytes:
        """Generate the board pack PDF from the assembled section data."""
        try:
            html_content = self.render_html(pack_data)

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
            raise Exception(f"Failed to generate board pack PDF: {str(e)}")
