"""Certificate / ID Card PDF Generation Service"""
from jinja2 import Environment, FileSystemLoader
from jinja2.sandbox import SandboxedEnvironment
from xhtml2pdf import pisa
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional
import base64
import logging
import mimetypes
import qrcode

logger = logging.getLogger(__name__)


class CertificatePDFService:
    """Service for generating certificate and ID card PDFs. Unlike the
    single-template PDF services (payslip, hall ticket), this one serves
    several templates — the caller passes template_name per call."""

    def __init__(self):
        template_dir = Path(__file__).parent.parent / "templates"
        self.env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=True
        )
        # Separate sandboxed environment for school-admin-authored custom_html
        # (models.certificates.CertificateTemplate.custom_html) — untrusted
        # template SOURCE, unlike the file-based templates above which are
        # developer-controlled. Compiling untrusted source with a plain
        # Environment.from_string() is a classic Jinja2 SSTI-to-RCE vector —
        # autoescape only escapes variable OUTPUT, it does nothing to stop
        # template syntax/attribute-access from executing (e.g. the standard
        # ''.__class__.__mro__[1].__subclasses__() gadget chain). Jinja2's
        # own SandboxedEnvironment blocks unsafe dunder-attribute access at
        # render time — the documented mitigation for exactly this case.
        self._sandboxed_env = SandboxedEnvironment(autoescape=True)

    @staticmethod
    def generate_qr_data_uri(payload: str) -> str:
        """Encode payload as a QR PNG, base64'd as a data: URI xhtml2pdf can
        render directly as an <img src=...> with no extra static-file plumbing."""
        img = qrcode.make(payload)
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    @staticmethod
    def resolve_local_image_data_uri(url_path: Optional[str]) -> Optional[str]:
        """Turns a locally-stored '/uploads/...' web path (e.g.
        Student.photo_url, AuthorizedPickupPerson.photo_url) into a base64
        data: URI xhtml2pdf can render directly — pisa has no way to fetch
        the app's own /uploads static mount over HTTP mid-render. Returns
        None (rather than raising) for an unset or missing photo, so a
        cardholder without a photo on file just gets no <img> on the card."""
        if not url_path or not url_path.startswith("/uploads/"):
            return None
        uploads_root = Path("uploads").resolve()
        local_path = (uploads_root / url_path[len("/uploads/"):]).resolve()
        # startswith("/uploads/") above is a check on the raw STRING, which
        # a value like "/uploads/../../.env" also passes — resolve both
        # paths and confirm the resolved target actually stays inside the
        # uploads root before ever reading it. Without this, a traversal
        # payload in a free-text photo_url (settable via the ordinary
        # student/staff profile-update endpoints, not just the dedicated
        # upload one) reads an arbitrary server-local file and embeds its
        # contents, base64'd, straight into a generated PDF.
        if not local_path.is_relative_to(uploads_root):
            return None
        if not local_path.is_file():
            return None
        mime_type, _ = mimetypes.guess_type(local_path.name)
        try:
            encoded = base64.b64encode(local_path.read_bytes()).decode("ascii")
        except OSError:
            return None
        return f"data:{mime_type or 'image/jpeg'};base64,{encoded}"

    def render_html(self, template_name: str, data: dict, custom_html: str = None) -> str:
        """Render either a school-customized template's raw Jinja2 source
        (custom_html, from models.certificates.CertificateTemplate) or the
        fixed file at templates/<template_name> when no customization exists."""
        formatted = {**data, "generated_date": datetime.utcnow().strftime("%d %B %Y")}
        if custom_html:
            template = self._sandboxed_env.from_string(custom_html)
        else:
            template = self.env.get_template(template_name)
        return template.render(**formatted)

    def generate_pdf(self, template_name: str, data: dict, custom_html: str = None) -> bytes:
        """Generate PDF bytes by rendering the given template with data."""
        try:
            html_content = self.render_html(template_name, data, custom_html=custom_html)

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
            logger.error(f"Failed to generate PDF from template {template_name}: {str(e)}")
            raise Exception(f"Failed to generate PDF: {str(e)}")
