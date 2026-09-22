from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlmodel import SQLModel

from config import get_settings

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ensure all models are imported so metadata is populated
from  models.attendance import Attendance ,StaffAttendance
from models.classroom import Class ,ClassLevel,ClassSubject
from models.communication import Announcement,Message,EmailNotification
from models.fee import Fee,FeePayment,FeeStructure
from models.grade import Grade,ReportCard
from models.school import School, AcademicTerm, AcademicYear, CalendarEvent
from models.staff import Staff,TeacherAssignment
from models.student import Student,Parent,StudentParent
from models.timetable import Period,Timetable  
from models.user import User
from models.payroll import PayrollContract, PayrollRun, PayrollLineItem, PayrollAdjustment
from models.finance.chart_of_accounts import GLAccount
from models.finance.journal_entries import JournalEntry, JournalLineItem
from models.finance.expenses import Expense
from models.payment import OnlineTransaction, PaymentVerification
from models.front_office import FrontOfficeVisitor
from models.admissions import Applicant
from models.health import StudentHealthProfile, ClinicVisit, ImmunizationRecord
from models.discipline import IncidentReport, IncidentStudent, IncidentAction
from models.inventory import AssetCategory, Asset, StockItem, StockIssuance
from models.alumni import AlumniRecord, AlumniDonation
from models.exam_board import ExamBoardRegistration, ExamSeatingAssignment, InvigilationDuty
from models.certificates import CertificateIssuance, CertificateTemplate, IDCard
from models.campus import Campus
from models.ptm import PTMSlot, PTMBooking
from models.document import Document
from models.leave_request import LeaveRequest, LeaveBalance
from models.rbac import Permission, Role, RolePermission
from models.integrations import ApiKey, WebhookEndpoint, WebhookDelivery, QuickBooksConnection
from models.procurement import Supplier, PurchaseOrder, PurchaseOrderLine
from models.hr import StaffPerformanceReview
from models.student_support import StudentSupportCase

# set target metadata for 'autogenerate' support
target_metadata = SQLModel.metadata

# Optionally override URL in environment with configured settings
config.set_main_option("sqlalchemy.url", get_settings().database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
