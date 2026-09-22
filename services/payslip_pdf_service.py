"""Payslip PDF Generation Service"""
from jinja2 import Environment, FileSystemLoader, Template
from xhtml2pdf import pisa
from datetime import datetime
from io import BytesIO
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class PayslipPDFService:
    """Service for generating payslip PDFs from payslip data dicts
    (the same shape returned by PayrollService.get_payslip_data)."""

    def __init__(self):
        template_dir = Path(__file__).parent.parent / "templates"
        self.env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=True
        )
        self.default_template_name = "payslip.html"

    @staticmethod
    def format_payslip_data(payslip: dict) -> dict:
        """Format a payslip dict (see PayrollService.get_payslip_data) into
        template-ready values, matching InvoicePDFService.format_invoice_data's
        pattern of pre-formatting numeric fields for display."""
        def money(value) -> str:
            return f"{float(value or 0):.2f}"

        adjustments_total = float(payslip.get("total_adjustments", 0) or 0)

        # The itemized breakdown (per-allowance, unpaid-leave, per-deduction-
        # rule) is computed and stored at generation time but was previously
        # never surfaced past the lump totals below — every payslip showed
        # "Allowances: 350.00" with no way to see it was housing+transport+
        # a responsibility allowance, or "Other Deductions: 40.00" with no
        # way to see which deduction rule fired.
        allowance_items = [
            {"label": name.replace("_", " ").title(), "amount": money(amt)}
            for name, amt in (payslip.get("allowance_breakdown") or {}).items()
            if float(amt or 0) != 0
        ]
        applied_rule_items = [
            {"label": r.get("rule_name", "Rule"), "amount": money(r.get("deduction_amount"))}
            for r in (payslip.get("applied_rules") or [])
        ]
        unpaid_leave_deduction = float(payslip.get("unpaid_leave_deduction") or 0)
        unpaid_leave_days = payslip.get("unpaid_leave_days") or 0
        adjustment_items = [
            {"label": f"{a.get('type', 'Adjustment').replace('_', ' ').title()}" + (f" — {a['reason']}" if a.get("reason") else ""), "amount": money(a.get("amount"))}
            for a in (payslip.get("adjustments") or [])
        ]
        proration_fraction = payslip.get("proration_fraction")

        return {
            "school_name": payslip.get("school_name", "School"),
            "period_name": payslip.get("period_name", ""),
            "staff_name": payslip.get("staff_name", "Unknown"),
            "staff_id_code": payslip.get("staff_id_code", ""),
            "position": payslip.get("position", ""),
            "currency": payslip.get("currency", "GHS"),
            "payment_status": str(payslip.get("payment_status", "unpaid")).capitalize(),

            "basic_salary": money(payslip.get("basic_salary")),
            "total_allowances": money(payslip.get("total_allowances")),
            "allowance_items": allowance_items,
            "gross_amount": money(payslip.get("gross_amount")),

            "tax_amount": money(payslip.get("tax_amount")),
            "pension_amount": money(payslip.get("pension_amount")),
            "nssf_amount": money(payslip.get("nssf_amount")),
            "other_deductions": money(payslip.get("other_deductions")),
            "applied_rule_items": applied_rule_items,
            "unpaid_leave_days": unpaid_leave_days,
            "unpaid_leave_deduction_numeric": unpaid_leave_deduction,
            "unpaid_leave_deduction": money(unpaid_leave_deduction),
            "total_deductions": money(payslip.get("total_deductions")),

            "adjustments_total_numeric": adjustments_total,
            "adjustments_total": money(adjustments_total),
            "adjustment_items": adjustment_items,

            "proration_fraction": proration_fraction,
            "proration_note": (
                f"Partial period — {round(float(proration_fraction) * 100)}% of a full month"
                if proration_fraction is not None and float(proration_fraction) < 1.0 else None
            ),

            "net_amount": money(payslip.get("net_amount")),

            "generated_date": datetime.utcnow().strftime("%d %B %Y"),
        }

    def render_html(self, payslip: dict) -> str:
        template = self.env.get_template(self.default_template_name)
        return template.render(**self.format_payslip_data(payslip))

    def generate_pdf(self, payslip: dict) -> bytes:
        """Generate payslip PDF bytes from a payslip data dict."""
        try:
            html_content = self.render_html(payslip)

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
            logger.error(f"Failed to generate payslip PDF: {str(e)}")
            raise Exception(f"Failed to generate payslip PDF: {str(e)}")
