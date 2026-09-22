"""School ERP System - Main Application"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import logging
import os
import asyncio
import sys
from pathlib import Path 
from middleware import register_middleware
from dotenv import load_dotenv

# Load environment variables
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# On Windows, prefer the selector event loop to avoid noisy
# ProactorBasePipeTransport shutdown exceptions when connections
# are reset by remote peers. This is a safe compatibility tweak
# for typical HTTP server workloads.
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

from database import init_db, close_db
from auth import close_redis
from services.broadcaster import broadcaster

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def register_routers(app: FastAPI):
    """🚀 OPTIMIZATION: Lazy-load routers on startup instead of module import
    
    This reduces startup time from 2-3 seconds to <500ms by deferring
    all router imports until after the app is ready.
    """
    logger.info("Loading routers...")
    
    # Import routers here instead of at module level
    from routers.auth import router as auth_router
    from routers.schools import router as schools_router
    from routers.students import router as students_router
    from routers.staff import router as staff_router
    from routers.classes import router as classes_router
    from routers.attendance import router as attendance_router
    from routers.gate_attendance import router as gate_attendance_router
    from routers.leave_requests import router as leave_requests_router
    from routers.grades import router as grades_router
    from routers.fees import router as fees_router
    from routers.timetable import router as timetable_router
    from routers.communication import router as communication_router
    from routers.dashboard import router as dashboard_router
    from routers.email import router as email_router
    from routers.sms import router as sms_router
    from routers.tickets import router as tickets_router
    from routers.parent import router as parent_router
    from routers.student_portal import router as student_portal_router
    from routers.kiosk import router as kiosk_router
    from routers.biometric_adms import router as biometric_adms_router
    from routers.report_templates import router as report_templates_router
    from routers.transport import router as transport_router
    from routers.hostel import router as hostel_router
    from routers.payroll import router as payroll_router
    from routers.deduction_rules import router as deduction_rules_router
    from routers.finance.coa import router as coa_router
    from routers.finance.journal import router as journal_router
    from routers.finance.expenses import router as expenses_router
    from routers.finance.reports import router as reports_router
    from routers.finance_reports import router as finance_reports_router
    from routers.fiscal_period_router import router as fiscal_period_router
    from routers.gl_audit_log_router import router as gl_audit_log_router
    from routers.retained_earnings_router import router as retained_earnings_router
    from routers.date_separation_router import router as date_separation_router
    from routers.reversal_router import router as reversal_router
    from routers.bank_reconciliation_router import router as bank_recon_router
    from routers.subledger_reconciliation_router import router as subledger_router
    from routers.account_hierarchy_router import router as hierarchy_router
    from routers.exchange_rate_router import router as exchange_rate_router
    from routers.budget_router import router as budget_router
    from routers.recurring_entry_router import router as recurring_entry_router
    from routers.depreciation_router import router as depreciation_router
    from routers.payments import router as payments_router
    from routers.ai_settings import router as ai_settings_router
    from routers.system_audit import router as system_audit_router
    from routers.teacher.grades import router as teacher_grades_router
    from routers.teacher.timetable import router as teacher_timetable_router
    from routers.teacher.assignments import router as teacher_assignments_router
    from routers.teacher.ptm import router as teacher_ptm_router
    from routers.documents import router as documents_router
    from routers.teacher_dashboard import router as teacher_dashboard_router
    from routers.settlements import router as settlements_router
    from routers.billing import router as billing_router
    from routers.security import router as security_router
    from routers.canteen_wallet import router as canteen_wallet_router
    from routers.extra_classes import router as extra_classes_router
    from routers.library import router as library_router
    from routers.library_circulation import router as library_circulation_router
    from routers.library_acquisitions import router as library_acquisitions_router
    from routers.absence_requests import router as absence_requests_router
    from routers.document_requests import router as document_requests_router
    from routers.consent_forms import router as consent_forms_router
    from routers.complaints import router as complaints_router
    from routers.acknowledgements import router as acknowledgements_router
    from routers.fee_reminders import router as fee_reminders_router
    from routers.front_office import router as front_office_router
    from routers.admissions import router as admissions_router
    from routers.admissions_enterprise import router as admissions_enterprise_router
    from routers.health import router as health_router
    from routers.discipline import router as discipline_router
    from routers.inventory import router as inventory_router
    from routers.alumni import router as alumni_router
    from routers.exam_board import router as exam_board_router
    from routers.certificates import router as certificates_router
    from routers.id_cards import router as id_cards_router
    from routers.campuses import router as campuses_router
    from routers.public_admissions import router as public_admissions_router
    from routers.exams import router as exams_router
    from routers.exam_papers import router as exam_papers_router
    from routers.exam_marks import router as exam_marks_router
    from routers.exam_remarks import router as exam_remarks_router
    from routers.exam_malpractice import router as exam_malpractice_router
    from routers.strategic_reports import router as strategic_reports_router
    from routers.attendance_risk import router as attendance_risk_router
    from routers.custom_reports import router as custom_reports_router
    from routers.executive_reports import router as executive_reports_router
    from routers.compliance import router as compliance_router
    from routers.operational_insights import router as operational_insights_router
    from routers.strategic_goals import router as strategic_goals_router
    from routers.risk_register import router as risk_register_router
    from routers.reports_analytics import router as reports_analytics_router
    from routers.accounting_integrations import router as accounting_integrations_router
    from routers.substitute_coverage import router as substitute_coverage_router
    from routers.roles import router as roles_router
    from routers.integrations import router as integrations_router
    from routers.public_api import router as public_api_router
    from routers.procurement import router as procurement_router
    from routers.hr import router as hr_router
    from routers.student_support import router as student_support_router
    from routers.student_interventions import router as student_interventions_router
    from routers.student_support_enterprise import router as student_support_enterprise_router
    from routers.curriculum import router as curriculum_router
    from routers.tracks import router as tracks_router
    from routers.academic_calendar import router as academic_calendar_router
    from routers.analytics import router as analytics_router
    from routers.hr_recruitment import router as hr_recruitment_router
    from routers.hr_development import router as hr_development_router
    from routers.hr_admin import router as hr_admin_router
    from routers.hr_overtime import router as hr_overtime_router
    from routers.facilities import router as facilities_router
    from routers.surveys import router as surveys_router
    from routers.push_notifications import router as push_notifications_router
    from routers.faq import router as faq_router

    # Register all routers
    app.include_router(auth_router, prefix="/api")
    app.include_router(schools_router, prefix="/api")
    app.include_router(students_router, prefix="/api")
    app.include_router(staff_router, prefix="/api")
    app.include_router(classes_router, prefix="/api")
    app.include_router(attendance_router, prefix="/api")
    app.include_router(gate_attendance_router, prefix="/api")
    app.include_router(leave_requests_router, prefix="/api")
    app.include_router(grades_router, prefix="/api")
    app.include_router(fees_router, prefix="/api")
    app.include_router(timetable_router, prefix="/api")
    app.include_router(communication_router, prefix="/api")
    app.include_router(dashboard_router, prefix="/api")
    app.include_router(email_router, prefix="/api")
    app.include_router(sms_router, prefix="/api")
    app.include_router(tickets_router, prefix="/api")
    app.include_router(parent_router, prefix="/api")
    app.include_router(student_portal_router, prefix="/api")
    app.include_router(kiosk_router, prefix="/api")
    app.include_router(biometric_adms_router, prefix="/api")
    app.include_router(report_templates_router, prefix="/api")
    app.include_router(transport_router, prefix="/api")
    app.include_router(hostel_router, prefix="/api")
    app.include_router(payroll_router, prefix="/api")
    app.include_router(deduction_rules_router, prefix="/api")
    app.include_router(coa_router, prefix="/api")
    app.include_router(journal_router, prefix="/api")
    app.include_router(expenses_router, prefix="/api")
    app.include_router(reports_router, prefix="/api")
    app.include_router(finance_reports_router)
    app.include_router(fiscal_period_router, prefix="/api", tags=["Accounting Compliance"])
    app.include_router(gl_audit_log_router, prefix="/api", tags=["Accounting Compliance"])
    app.include_router(retained_earnings_router, prefix="/api", tags=["Accounting Compliance"])
    app.include_router(date_separation_router, prefix="/api", tags=["Accounting Compliance"])
    app.include_router(reversal_router, prefix="/api", tags=["Accounting Compliance"])
    app.include_router(bank_recon_router, prefix="/api", tags=["Accounting Compliance"])
    app.include_router(subledger_router, prefix="/api", tags=["Accounting Compliance"])
    app.include_router(hierarchy_router, prefix="/api", tags=["Accounting Compliance"])
    app.include_router(exchange_rate_router, prefix="/api", tags=["Accounting Compliance"])
    app.include_router(budget_router, prefix="/api", tags=["Accounting Compliance"])
    app.include_router(recurring_entry_router, prefix="/api", tags=["Accounting Compliance"])
    app.include_router(depreciation_router, prefix="/api", tags=["Accounting Compliance"])
    app.include_router(payments_router, prefix="/api")
    app.include_router(ai_settings_router, prefix="/api", tags=["AI Settings"])
    app.include_router(system_audit_router, prefix="/api", tags=["System Audit"])
    app.include_router(settlements_router, prefix="/api")
    app.include_router(teacher_grades_router, prefix="/api")
    app.include_router(teacher_timetable_router, prefix="/api")
    app.include_router(teacher_assignments_router, prefix="/api")
    app.include_router(teacher_ptm_router, prefix="/api")
    app.include_router(documents_router, prefix="/api")
    app.include_router(teacher_dashboard_router, prefix="/api")
    app.include_router(billing_router, prefix="/api")
    app.include_router(security_router, prefix="/api")
    app.include_router(canteen_wallet_router, prefix="/api")
    app.include_router(extra_classes_router, prefix="/api")
    app.include_router(library_router, prefix="/api")
    app.include_router(library_circulation_router, prefix="/api")
    app.include_router(library_acquisitions_router, prefix="/api")
    app.include_router(absence_requests_router, prefix="/api")
    app.include_router(document_requests_router, prefix="/api")
    app.include_router(consent_forms_router, prefix="/api")
    app.include_router(complaints_router, prefix="/api")
    app.include_router(acknowledgements_router, prefix="/api")
    app.include_router(fee_reminders_router, prefix="/api")
    app.include_router(front_office_router, prefix="/api")
    app.include_router(admissions_router, prefix="/api")
    app.include_router(admissions_enterprise_router, prefix="/api")
    app.include_router(health_router, prefix="/api")
    app.include_router(discipline_router, prefix="/api")
    app.include_router(inventory_router, prefix="/api")
    app.include_router(alumni_router, prefix="/api")
    app.include_router(exam_board_router, prefix="/api")
    app.include_router(certificates_router, prefix="/api")
    app.include_router(id_cards_router, prefix="/api")
    app.include_router(campuses_router, prefix="/api")
    app.include_router(public_admissions_router, prefix="/api")
    app.include_router(exams_router, prefix="/api")
    app.include_router(exam_papers_router, prefix="/api")
    app.include_router(exam_marks_router, prefix="/api")
    app.include_router(exam_remarks_router, prefix="/api")
    app.include_router(exam_malpractice_router, prefix="/api")
    app.include_router(strategic_reports_router, prefix="/api")
    app.include_router(attendance_risk_router, prefix="/api")
    app.include_router(custom_reports_router, prefix="/api")
    app.include_router(executive_reports_router, prefix="/api")
    app.include_router(compliance_router, prefix="/api")
    app.include_router(operational_insights_router, prefix="/api")
    app.include_router(strategic_goals_router, prefix="/api")
    app.include_router(risk_register_router, prefix="/api")
    app.include_router(reports_analytics_router, prefix="/api")
    app.include_router(accounting_integrations_router, prefix="/api")
    app.include_router(substitute_coverage_router, prefix="/api")
    app.include_router(roles_router, prefix="/api", tags=["Roles & Permissions"])
    app.include_router(integrations_router, prefix="/api", tags=["Integrations"])
    app.include_router(public_api_router, tags=["Public API"])
    app.include_router(procurement_router, prefix="/api")
    app.include_router(hr_router, prefix="/api")
    app.include_router(student_support_router, prefix="/api")
    app.include_router(student_interventions_router, prefix="/api")
    app.include_router(student_support_enterprise_router, prefix="/api")
    app.include_router(curriculum_router, prefix="/api")
    app.include_router(tracks_router, prefix="/api")
    app.include_router(academic_calendar_router, prefix="/api")
    app.include_router(analytics_router, prefix="/api")
    app.include_router(hr_recruitment_router, prefix="/api")
    app.include_router(hr_development_router, prefix="/api")
    app.include_router(hr_admin_router, prefix="/api")
    app.include_router(hr_overtime_router, prefix="/api")
    app.include_router(facilities_router, prefix="/api")
    app.include_router(surveys_router, prefix="/api")
    app.include_router(push_notifications_router, prefix="/api")
    app.include_router(faq_router, prefix="/api")

    logger.info("✓ All routers loaded successfully")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events"""
    logger.info("🚀 Starting School ERP System...")
    
    # Initialize database
    await init_db()
    
    # Register routers (lazy load)
    await register_routers(app)

    # Daily billing cycle (auto-renewals, reminders, overdue suspension) —
    # previously all manual-trigger-only endpoints, see services/scheduler.py
    from services.scheduler import start_scheduler
    start_scheduler()

    logger.info("✅ Application started successfully")

    yield

    logger.info("Shutting down School ERP System...")
    from services.scheduler import stop_scheduler
    stop_scheduler()
    await close_db()
    await close_redis()
    try:
        await broadcaster.close()
    except Exception:
        logger.exception('Error closing broadcaster')
    logger.info("Application shutdown complete")


# Error monitoring — entirely optional. Inert until SENTRY_DSN is set (see
# .env.example); no account, no cost, no behavior change without it.
_sentry_dsn = os.environ.get("SENTRY_DSN", "").strip()
if _sentry_dsn:
    import sentry_sdk
    sentry_sdk.init(
        dsn=_sentry_dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        traces_sample_rate=float(os.environ.get("SENTRY_SAMPLE_RATE", "0.1")),
    )
    logger.info("Sentry error monitoring enabled")

# Create FastAPI app
app = FastAPI(
    title="School ERP System",
    description="Enterprise-grade School ERP for Ghanaian Basic and JHS Schools",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

register_middleware(app)

# Mount static files
from fastapi.staticfiles import StaticFiles
app.mount("/templates", StaticFiles(directory="templates"), name="templates")

os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


@app.get("/api")
async def root():
    """API root endpoint"""
    return {
        "message": "School ERP System API",
        "version": "1.0.0",
        "status": "running"
    }


@app.get("/api/health")
async def health_check():
    """Real health check for an external uptime monitor to poll — checks
    the two things that actually determine whether the app can serve a
    request, rather than just confirming the process is alive. The
    database is a hard dependency (503 if it's unreachable); Redis is a
    soft one — auth.py's get_current_user already falls back to the
    database on a cache miss, so a Redis outage degrades performance, not
    availability, and shouldn't page anyone."""
    from database import async_session
    from auth import get_redis
    from sqlalchemy import text

    checks = {"database": "unknown", "redis": "unknown"}
    healthy = True

    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = "unreachable"
        healthy = False
        logger.error(f"Health check: database unreachable ({e})")

    try:
        redis_client = await get_redis()
        if redis_client:
            await redis_client.ping()
            checks["redis"] = "ok"
        else:
            checks["redis"] = "unavailable"
    except Exception:
        checks["redis"] = "unavailable"

    status_code = 200 if healthy else 503
    return JSONResponse(status_code=status_code, content={"status": "healthy" if healthy else "unhealthy", "checks": checks})
