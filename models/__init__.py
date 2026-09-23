"""Models package. Imports below register tables before database initialization."""
from models.facilities import FacilityAsset, MaintenanceRequest, MaintenanceSchedule
from models.survey import Survey, SurveyResponse
from models.procurement import PurchaseRequisition, PurchaseRequisitionLine, GoodsReceivedNote, GoodsReceivedLine, StockReturn, StockTransfer, SupplierInvoice
from models.hr_recruitment import Vacancy, StaffApplicant, Interview, OfferLetter
from models.hr_development import StaffOnboardingTask, StaffTraining, StaffCertification
from models.hr_admin import StaffBenefit, StaffDisciplinaryAction, StaffExit, WorkforcePlan
from models.curriculum import (
    CurriculumTopic, LessonPlan, TeacherLessonNote,
    CurriculumStandard, CurriculumStandardCreate, CurriculumStandardUpdate,
    TopicStandardLink, TopicStandardLinkCreate,
)
from models.tracks import (
    Track, TrackCreate, TrackUpdate,
    TrackSubject, TrackSubjectCreate,
    StudentTrack, StudentTrackCreate,
)
from models.student_support import InterventionPlan, InterventionProgress
from models.student_support_enterprise import SENProfile, IndividualEducationPlan, CounsellingSession, BehaviourSupportPlan, SupportEscalation, ParentConsent
from models.admissions_enterprise import (
    ApplicantInterview, ApplicantOffer, AdmissionDeposit, ApplicantStageEvent, EntranceExamResult,
    ApplicationReview, ApplicationReviewCreate, ApplicationReviewCriterion,
)
import models.finance  # registers all finance tables with SQLModel metadata
from models.security import (
    StudentSecurityProfile, StudentSecurityProfileCreate, StudentSecurityProfileUpdate,
    DailyQRToken, SecurityScanLog, ArrivalEvent, ParentNote, ParentNoteCreate,
    LiveBusLocation, LiveBusLocationCreate,
    CollectorTrackingSession, CollectorLiveLocation, CollectorLocationCreate,
    ArrivalStatus, QRTokenType, ScanResult, ArrivalEventType,
    AuthorizedPickupPerson, StudentLocationLog,
)
from models.user import User, UserCreate, UserLogin, UserResponse, UserRole
from models.rbac import Permission, Role, RolePermission
from models.login_attempt import LoginAttempt
from models.school import (
    School, SchoolCreate, SchoolType, AcademicTerm, AcademicTermCreate, TermType,
    AcademicYear, AcademicYearCreate, AcademicYearUpdate, AcademicYearStatus,
    CalendarEvent, CalendarEventCreate, CalendarEventUpdate,
)
from models.student import (
    Student, StudentCreate, StudentStatus, Gender, Parent, ParentCreate, StudentParent, StudentEnrollment,
    StudentSibling, StudentSiblingCreate, EmergencyContact, EmergencyContactCreate, EmergencyContactUpdate,
    TransferRequest, TransferRequestCreate, TransferRequestUpdate, CustodyType, ParentCustodyUpdate,
)
from models.staff import Staff, StaffCreate, StaffType, StaffStatus, TeacherAssignment
from models.classroom import Class, ClassCreate, ClassLevel, Subject, SubjectCreate, SubjectCategory, ClassSubject, ClassWaitlistEntry
from models.attendance import Attendance, AttendanceCreate, AttendanceBulkCreate, AttendanceStatus, StaffAttendance
from models.gate_attendance import GateAttendance, GateAttendanceSettings
from models.shift import Shift, ShiftCreate, ShiftUpdate, ShiftAssign, ShiftBulkAssign
from models.grade import Grade, GradeCreate, AssessmentType, GradeScale, GradingScheme, ReportCard, ReportCardRecall, StandardMasteryRecord, StandardMasteryRecordCreate
from models.fee import Fee, FeeCreate, FeeStructure, FeeStructureCreate, FeePayment, FeePaymentCreate, FeeType, PaymentStatus, PaymentMethod
from models.payroll import (
    PayrollContract, PayrollContractCreate, PayrollContractUpdate, PaySchedule,
    PayrollRun, PayrollRunCreate, PayrollStatus,
    PayrollLineItem, PayrollAdjustment, PayrollAdjustmentCreate,
    PayrollCategory, PayslipResponse
)
from models.staff_loan import StaffLoan, StaffLoanCreate, StaffLoanRepayment, LoanStatus, LoanWriteOffRequest
from models.timetable import Timetable, TimetableCreate, Period, PeriodCreate, DayOfWeek, PeriodType
from models.communication import Announcement, AnnouncementCreate, AnnouncementType, AnnouncementAudience, Message, MessageCreate, EmailNotification
from models.report_template import ReportTemplate, ReportTemplateCreate, ReportTemplateUpdate, ReportTemplateResponse
from models.assignment import (
    Assignment, AssignmentCreate, AssignmentUpdate, AssignmentResponse, AssignmentType, AssignmentStatus,
    Submission, SubmissionCreate, SubmissionGrade, SubmissionResponse, SubmissionStatus,
    TeacherResource, TeacherResourceCreate, ResourceType,
    LearningMaterial, LearningMaterialCreate,
    StudentProgressNote, StudentProgressNoteCreate, StudentProgressNoteResponse, ProgressNoteType,
    AssignmentStats, ClassPerformanceMetrics, SubmissionSummary,
    CourseModule, CourseModuleCreate, CourseModuleUpdate,
    CourseModuleItem, CourseModuleItemCreate,
    StudentModuleProgress,
)
from models.library import (
    LibraryCategory, LibraryCategoryCreate, LibraryCategoryUpdate, LibraryItem, LibraryItemUpdate,
    LibraryMaterialType, LibraryContentType, LibraryInteractionType,
    LibraryTag, LibraryItemTag, LibraryItemClass, LibraryItemFavorite,
    LibraryItemRating, LibraryItemRatingCreate, LibraryItemInteraction,
)
from models.library_circulation import (
    LibraryBookCopy, LibraryBookCopyCreate, LibraryBookCopyUpdate,
    LibraryLoan, LibraryLoanCreate, LibraryLoanReportLost,
    LibraryFine, LibraryFineWaive,
    LibraryReservation, LibraryReservationCreate,
    CopyCondition, CopyStatus, LoanStatus, FineStatus, ReservationStatus,
)
from models.transport import (
    Vehicle, VehicleCreate, VehicleUpdate, VehicleStatus, VehicleType,
    Route, RouteCreate, RouteUpdate, RouteStatus,
    StudentTransport, StudentTransportCreate, StudentTransportUpdate,
    TransportAttendance, TransportAttendanceCreate, TransportAttendanceBulk, AttendanceStatus as TransportAttendanceStatus,
    TransportFee, TransportFeeCreate, TransportFeeUpdate, TransportFeeType,
    VehicleMaintenance, VehicleMaintenanceCreate,
    DriverStaff, DriverStaffCreate, DriverStaffUpdate
)
from models.hostel import (
    Hostel, HostelCreate, HostelUpdate, HostelStatus,
    Room, RoomCreate, RoomUpdate, RoomType, RoomStatus,
    StudentHostel, StudentHostelCreate, StudentHostelUpdate, StudentHostelStatus,
    RoomAllocation, RoomAllocationCreate,
    HostelAttendance, HostelAttendanceCreate, CheckInStatus,
    HostelFee, HostelFeeCreate, HostelFeeUpdate, HostelFeeType,
    HostelMaintenance, HostelMaintenanceCreate,
    RoomInventoryItem, RoomInventoryItemCreate, RoomInventoryItemUpdate,
    HostelVisitor, HostelVisitorCreate,
    HostelComplaint, HostelComplaintCreate, HostelComplaintUpdate
)
from models.billing import (
    PlatformSubscription, SubscriptionInvoice, SubscriptionStatus,
    PlatformSubscriptionResponse, SubscriptionInvoiceResponse,
    GenerateSubscriptionRequest, ProcessSubscriptionPaymentRequest,
    SubscriptionMetrics,
    # Phase 2 Models
    BillingConfiguration, BillingConfigurationResponse,
    DiscountRule, PaymentReminder, LateFeeCharge,
    BillingReport
)
from models.integrations import ApiKey, WebhookEndpoint, WebhookDelivery
from models.settlement import Withdrawal, WithdrawalStatus, WithdrawalRead
from models.ticket import (
    Ticket, TicketCreate, TicketUpdate, TicketResponse, TicketDetailResponse,
    TicketCategory, TicketPriority, TicketStatus,
    TicketComment, TicketCommentCreate, TicketCommentResponse,
    TicketAttachment, TicketNotification, TicketCloseRequest
)
from models.otp import (
    OTP, OTPBase, OTPSettings, OTPVerificationRequest, OTPVerificationResponse,
    OTPAdminSettings
)
from models.payment import (
    OnlineTransaction, OnlineTransactionRead, PaymentVerification,
    TransactionStatus, PaymentGateway, TransactionType
)
from models.canteen_wallet import CanteenItem, CanteenWalletAccount, CanteenWalletLedgerEntry
from models.extra_class import (
    ExtraClass, ExtraClassCreate, ExtraClassUpdate, ExtraClassEnrollment, ExtraClassSession,
    ExtraClassAssignment, ExtraClassSubmission, ExtraClassGrade, ExtraClassBillingCycle,
    ExtraClassPayment, TeacherPayoutRequest, ExtraClassReminderLog, ExtraClassStatus,
    EnrollmentStatus, BillingInterval, BillingCycleStatus, PayoutStatus
    # SubmissionStatus intentionally not re-imported here — models.extra_class now
    # shares models.assignment.SubmissionStatus (already imported above) instead of
    # defining its own, so importing it again here would just rebind the same name.
)
from models.ai_settings import AIProvider, SchoolAISettings, AISettingsResponse, UpdateAISettingsRequest
from models.audit import SystemAuditLog, SystemAuditLogResponse
from models.document import Document, DocumentOwnerType, DocumentCategory
from models.ptm import PTMSlot, PTMBooking, PTMSlotStatus, PTMBookingStatus, PTMSlotCreate, PTMBookingCreate, PTMCancelRequest
from models.parent_requests import (
    AbsenceRequest, AbsenceRequestCreate, AbsenceRequestReview, AbsenceRequestType,
    RequestStatus, DocumentRequest, DocumentRequestCreate, DocumentRequestReview,
    DocumentRequestFulfill, DocumentRequestStatus, DocumentType,
)
from models.front_office import (
    FrontOfficeVisitor, FrontOfficeVisitorCreate, FrontOfficeVisitorUpdate, VisitorStatus,
    VisitorApprovalStatus, RejectVisitorRequest,
    GatePass, GatePassCreate, GatePassUpdate,
    Appointment, AppointmentCreate, AppointmentUpdate,
    CourierItem, CourierItemCreate, CourierItemUpdate,
)
from models.admissions import (
    Applicant, ApplicantCreate, ApplicantUpdate, ApplicantConvertRequest, ApplicationStatus,
    PublicApplicantCreate,
)
from models.health import (
    StudentHealthProfile, StudentHealthProfileCreate, StudentHealthProfileUpdate,
    ClinicVisit, ClinicVisitCreate, ClinicVisitUpdate, VisitApprovalStatus, RejectVisitRequest,
    ImmunizationRecord, ImmunizationRecordCreate, ImmunizationRecordUpdate,
    MedicationAdministration, MedicationAdministrationCreate, MedicationAdministrationUpdate,
    HealthIncident, HealthIncidentCreate, HealthIncidentUpdate,
    HealthScreeningCampaign, HealthScreeningCampaignCreate, HealthScreeningCampaignUpdate,
    HealthScreeningResult, HealthScreeningResultCreate, HealthScreeningResultBulkCreate,
)
from models.discipline import (
    IncidentReport, IncidentReportCreate, IncidentReportUpdate, IncidentSeverity,
    IncidentStudent, IncidentAction, IncidentActionCreate, DisciplineActionType,
)
from models.inventory import (
    AssetCategory, AssetCategoryCreate, AssetCategoryUpdate,
    Asset, AssetCreate, AssetUpdate, AssetCondition, AssetStatus,
    StockItem, StockItemCreate, StockItemUpdate,
    StockIssuance, StockIssuanceCreate, IssuanceApprovalStatus, RejectIssuanceRequest,
)
from models.alumni import (
    AlumniRecord, AlumniRecordCreate, AlumniRecordUpdate, AlumniOutreachRequest,
    AlumniDonation, AlumniDonationCreate, DonationApprovalStatus, RejectDonationRequest,
)
from models.exam_board import (
    ExamBoardRegistration, ExamBoardRegistrationCreate, ExamBoardRegistrationUpdate, ExamRegistrationStatus,
    ExamSeatingAssignment, ExamSeatingAssignmentCreate, ExamSeatingAssignmentUpdate,
    InvigilationDuty, InvigilationDutyCreate, InvigilationDutyUpdate,
)
from models.exam_malpractice import MalpracticeCase, MalpracticeCaseCreate, MalpracticeCaseUpdate, MalpracticeStatus, MalpracticeSanction
from models.exam_papers import (
    QuestionBankItem, QuestionBankItemCreate, QuestionBankItemUpdate,
    ExamPaper, ExamPaperCreate, ExamPaperUpdate, ExamPaperStatus,
    ExamPaperModerationDecision, ExamPaperQuestion, ExamPaperQuestionAdd,
)
from models.exam import (
    ExamSession, ExamSessionCreate, ExamSessionUpdate, ExamSessionStatus,
    ExamSessionReleaseDate, ExamSchedule, ExamScheduleCreate, ExamScheduleUpdate,
    ExamSeatAssignment, ExamSeatAssignmentUpdate, ExamInvigilator, ExamInvigilatorCreate,
)
from models.exam_marks import (
    ExamComponent, ExamComponentCreate, ExamComponentUpdate,
    ExamComponentMark, ExamComponentMarkUpsert, BulkExamComponentMarksUpsert,
)
from models.exam_remarks import ExamRemarkRequest, ExamRemarkRequestCreate, ExamRemarkRequestReview, RemarkRequestStatus
from models.certificates import (
    CertificateIssuance, CertificateIssuanceCreate, CertificateType,
    CertificateTemplate, CertificateTemplateCreate, CertificateTemplateUpdate,
    IDCard, IDCardCreate, IDCardUpdate, PersonType, IDCardStatus,
)
from models.campus import Campus, CampusCreate, CampusUpdate
from models.procurement import (
    Supplier, SupplierCreate, SupplierUpdate, SupplierStatus,
    PurchaseOrder, PurchaseOrderLine, PurchaseOrderCreate, PurchaseOrderLineCreate,
    PurchaseOrderStatus,
)
from models.hr import StaffPerformanceReview, StaffPerformanceReviewCreate, StaffPerformanceReviewUpdate, ReviewStatus
from models.student_support import StudentSupportCase, StudentSupportCaseCreate, StudentSupportCaseUpdate, SupportCaseStatus, SupportCaseSeverity
from models.analytics import AnalyticsSnapshot, ClassPerformanceSummary, RiskLevel
# leave_request.py was never registered here at all (a pre-existing gap,
# same bug class as models/shift.py earlier — a table that only exists via
# its Alembic migration, not via create_all()); fixed alongside the new
# HR-workflow-depth models below.
from models.leave_request import LeaveRequest, LeaveBalance, LeaveType, LeaveRequestStatus
from models.leave_encashment import LeaveEncashmentRequest, LeaveEncashmentCreate, LeaveEncashmentStatus
from models.overtime import OvertimeRecord
from models.staff_performance_plus import StaffGoal, PerformanceFeedbackRequest, PerformanceFeedback, PerformanceImprovementPlan
from models.strategic_goals import StrategicGoal, StrategicGoalCreate, StrategicGoalUpdate, StrategicGoalProgressUpdate
from models.succession_planning import SuccessionPlan
from models.department import Department
from models.hr_admin import BenefitPlan
from models.push_notification import PushSubscription, NotificationPreference
from models.message_attachment import MessageAttachment
from models.group_conversation import Conversation, ConversationParticipant
from models.faq import FaqArticle
# Customizable executive dashboards (per-user saved widget layout)
from models.dashboard_layout import DashboardLayout, DashboardLayoutUpdate

__all__ = [
    "User", "UserCreate", "UserLogin", "UserResponse", "UserRole",
    "LoginAttempt",
    "School", "SchoolCreate", "SchoolType", "AcademicTerm", "AcademicTermCreate", "TermType",
    "AcademicYear", "AcademicYearCreate", "AcademicYearUpdate", "AcademicYearStatus",
    "CalendarEvent", "CalendarEventCreate", "CalendarEventUpdate",
    "Supplier", "SupplierCreate", "SupplierUpdate", "SupplierStatus",
    "PurchaseOrder", "PurchaseOrderLine", "PurchaseOrderCreate", "PurchaseOrderLineCreate", "PurchaseOrderStatus",
    "StaffPerformanceReview", "StaffPerformanceReviewCreate", "StaffPerformanceReviewUpdate", "ReviewStatus",
    "StudentSupportCase", "StudentSupportCaseCreate", "StudentSupportCaseUpdate", "SupportCaseStatus", "SupportCaseSeverity",
    "AnalyticsSnapshot", "ClassPerformanceSummary", "RiskLevel",
    "Student", "StudentCreate", "StudentStatus", "Gender", "Parent", "ParentCreate", "StudentParent",
    # Student records / admissions gaps additions
    "StudentSibling", "StudentSiblingCreate", "EmergencyContact", "EmergencyContactCreate", "EmergencyContactUpdate",
    "TransferRequest", "TransferRequestCreate", "TransferRequestUpdate", "CustodyType", "ParentCustodyUpdate",
    "ApplicationReview", "ApplicationReviewCreate", "ApplicationReviewCriterion",
    "Staff", "StaffCreate", "StaffType", "StaffStatus", "TeacherAssignment",
    "Class", "ClassCreate", "ClassLevel", "Subject", "SubjectCreate", "SubjectCategory", "ClassSubject", "ClassWaitlistEntry",
    "Attendance", "AttendanceCreate", "AttendanceBulkCreate", "AttendanceStatus", "StaffAttendance",
    "Shift", "ShiftCreate", "ShiftUpdate", "ShiftAssign", "ShiftBulkAssign",
    "Grade", "GradeCreate", "AssessmentType", "GradeScale", "ReportCard",
    "StandardMasteryRecord", "StandardMasteryRecordCreate",
    "Fee", "FeeCreate", "FeeStructure", "FeeStructureCreate", "FeePayment", "FeePaymentCreate", "FeeType", "PaymentStatus", "PaymentMethod",
    "PayrollContract", "PayrollContractCreate", "PayrollContractUpdate", "PaySchedule",
    "PayrollRun", "PayrollRunCreate", "PayrollStatus",
    "PayrollLineItem", "PayrollAdjustment", "PayrollAdjustmentCreate",
    "PayrollCategory", "PayslipResponse",
    "Timetable", "TimetableCreate", "Period", "PeriodCreate", "DayOfWeek", "PeriodType",
    "Announcement", "AnnouncementCreate", "AnnouncementType", "AnnouncementAudience", "Message", "MessageCreate", "EmailNotification",
    "ReportTemplate", "ReportTemplateCreate", "ReportTemplateUpdate", "ReportTemplateResponse",
    # Teacher Portal - Assignment/Submission Models
    "Assignment", "AssignmentCreate", "AssignmentUpdate", "AssignmentResponse", "AssignmentType", "AssignmentStatus",
    "Submission", "SubmissionCreate", "SubmissionGrade", "SubmissionResponse", "SubmissionStatus",
    "TeacherResource", "TeacherResourceCreate", "ResourceType",
    "LearningMaterial", "LearningMaterialCreate",
    "CourseModule", "CourseModuleCreate", "CourseModuleUpdate",
    "CourseModuleItem", "CourseModuleItemCreate",
    "StudentModuleProgress",
    "LibraryCategory", "LibraryCategoryCreate", "LibraryCategoryUpdate", "LibraryItem", "LibraryItemUpdate",
    "LibraryMaterialType", "LibraryContentType", "LibraryInteractionType",
    "LibraryTag", "LibraryItemTag", "LibraryItemClass", "LibraryItemFavorite",
    "LibraryItemRating", "LibraryItemRatingCreate", "LibraryItemInteraction",
    "LibraryBookCopy", "LibraryBookCopyCreate", "LibraryBookCopyUpdate",
    "LibraryLoan", "LibraryLoanCreate", "LibraryLoanReportLost",
    "LibraryFine", "LibraryFineWaive",
    "LibraryReservation", "LibraryReservationCreate",
    "CopyCondition", "CopyStatus", "LoanStatus", "FineStatus", "ReservationStatus",
    "StudentProgressNote", "StudentProgressNoteCreate", "StudentProgressNoteResponse", "ProgressNoteType",
    "AssignmentStats", "ClassPerformanceMetrics", "SubmissionSummary",
    # Transport Module
    "Vehicle", "VehicleCreate", "VehicleUpdate", "VehicleStatus", "VehicleType",
    "Route", "RouteCreate", "RouteUpdate", "RouteStatus",
    "StudentTransport", "StudentTransportCreate", "StudentTransportUpdate",
    "TransportAttendance", "TransportAttendanceCreate", "TransportAttendanceBulk", "TransportAttendanceStatus",
    "TransportFee", "TransportFeeCreate", "TransportFeeUpdate", "TransportFeeType",
    "VehicleMaintenance", "VehicleMaintenanceCreate",
    "DriverStaff", "DriverStaffCreate", "DriverStaffUpdate",
    # Hostel Module
    "Hostel", "HostelCreate", "HostelUpdate", "HostelStatus",
    "Room", "RoomCreate", "RoomUpdate", "RoomType", "RoomStatus",
    "StudentHostel", "StudentHostelCreate", "StudentHostelUpdate", "StudentHostelStatus",
    "RoomAllocation", "RoomAllocationCreate",
    "HostelAttendance", "HostelAttendanceCreate", "CheckInStatus",
    "HostelFee", "HostelFeeCreate", "HostelFeeUpdate", "HostelFeeType",
    "HostelMaintenance", "HostelMaintenanceCreate",
    "RoomInventoryItem", "RoomInventoryItemCreate", "RoomInventoryItemUpdate",
    "HostelVisitor", "HostelVisitorCreate",
    "HostelComplaint", "HostelComplaintCreate", "HostelComplaintUpdate",
    # Platform Billing Module
    "PlatformSubscription", "SubscriptionInvoice", "SubscriptionStatus",
    "PlatformSubscriptionResponse", "SubscriptionInvoiceResponse",
    "GenerateSubscriptionRequest", "ProcessSubscriptionPaymentRequest",
    "SubscriptionMetrics",
    # Platform Billing Phase 2
    "BillingConfiguration", "BillingConfigurationResponse",
    "DiscountRule", "PaymentReminder", "LateFeeCharge",
    "BillingReport",
    # Settlement Module
    "Withdrawal", "WithdrawalStatus", "WithdrawalRead",
    # Ticket Module
    "Ticket", "TicketCreate", "TicketUpdate", "TicketResponse", "TicketDetailResponse",
    "TicketCategory", "TicketPriority", "TicketStatus",
    "TicketComment", "TicketCommentCreate", "TicketCommentResponse",
    "TicketAttachment", "TicketNotification", "TicketCloseRequest",
    # OTP Module
    "OTP", "OTPBase", "OTPSettings", "OTPVerificationRequest", "OTPVerificationResponse",
    "OTPAdminSettings",
    # Online Payment Module
    "OnlineTransaction", "OnlineTransactionRead", "PaymentVerification",
    "TransactionStatus", "PaymentGateway", "TransactionType",
    # Canteen Wallet Module
    "CanteenItem", "CanteenWalletAccount", "CanteenWalletLedgerEntry",
    # Extra Classes Module
    "ExtraClass", "ExtraClassCreate", "ExtraClassUpdate", "ExtraClassEnrollment", "ExtraClassSession",
    "ExtraClassAssignment", "ExtraClassSubmission", "ExtraClassGrade", "ExtraClassBillingCycle",
    "ExtraClassPayment", "TeacherPayoutRequest", "ExtraClassReminderLog", "ExtraClassStatus",
    "EnrollmentStatus", "BillingInterval", "BillingCycleStatus", "PayoutStatus",
    # (SubmissionStatus already exported above, in the Assignment/Submission section)
    # Security Module
    "StudentSecurityProfile", "StudentSecurityProfileCreate", "StudentSecurityProfileUpdate",
    "DailyQRToken", "SecurityScanLog", "ArrivalEvent", "ParentNote", "ParentNoteCreate",
    "LiveBusLocation", "LiveBusLocationCreate",
    "CollectorTrackingSession", "CollectorLiveLocation", "CollectorLocationCreate",
    "ArrivalStatus", "QRTokenType", "ScanResult", "ArrivalEventType",
    # Front Office Module
    "FrontOfficeVisitor", "FrontOfficeVisitorCreate", "FrontOfficeVisitorUpdate", "VisitorStatus",
    "VisitorApprovalStatus", "RejectVisitorRequest",
    "GatePass", "GatePassCreate", "GatePassUpdate",
    "Appointment", "AppointmentCreate", "AppointmentUpdate",
    "CourierItem", "CourierItemCreate", "CourierItemUpdate",
    # Admissions Module
    "Applicant", "ApplicantCreate", "ApplicantUpdate", "ApplicantConvertRequest", "ApplicationStatus",
    "PublicApplicantCreate",
    # Health Module
    "StudentHealthProfile", "StudentHealthProfileCreate", "StudentHealthProfileUpdate",
    "ClinicVisit", "ClinicVisitCreate", "ClinicVisitUpdate", "VisitApprovalStatus", "RejectVisitRequest",
    "ImmunizationRecord", "ImmunizationRecordCreate", "ImmunizationRecordUpdate",
    "MedicationAdministration", "MedicationAdministrationCreate", "MedicationAdministrationUpdate",
    "HealthIncident", "HealthIncidentCreate", "HealthIncidentUpdate",
    "HealthScreeningCampaign", "HealthScreeningCampaignCreate", "HealthScreeningCampaignUpdate",
    "HealthScreeningResult", "HealthScreeningResultCreate", "HealthScreeningResultBulkCreate",
    # Discipline Module
    "IncidentReport", "IncidentReportCreate", "IncidentReportUpdate", "IncidentSeverity",
    "IncidentStudent", "IncidentAction", "IncidentActionCreate", "DisciplineActionType",
    # Inventory Module
    "AssetCategory", "AssetCategoryCreate", "AssetCategoryUpdate",
    "Asset", "AssetCreate", "AssetUpdate", "AssetCondition", "AssetStatus",
    "StockItem", "StockItemCreate", "StockItemUpdate",
    "StockIssuance", "StockIssuanceCreate", "IssuanceApprovalStatus", "RejectIssuanceRequest",
    # Alumni Module
    "AlumniRecord", "AlumniRecordCreate", "AlumniRecordUpdate", "AlumniOutreachRequest",
    "AlumniDonation", "AlumniDonationCreate", "DonationApprovalStatus", "RejectDonationRequest",
    # Exam Board Module
    "ExamBoardRegistration", "ExamBoardRegistrationCreate", "ExamBoardRegistrationUpdate", "ExamRegistrationStatus",
    "ExamSeatingAssignment", "ExamSeatingAssignmentCreate", "ExamSeatingAssignmentUpdate",
    "InvigilationDuty", "InvigilationDutyCreate", "InvigilationDutyUpdate",
    # Certificates / ID Cards Module
    "CertificateIssuance", "CertificateIssuanceCreate", "CertificateType",
    "CertificateTemplate", "CertificateTemplateCreate", "CertificateTemplateUpdate",
    "IDCard", "IDCardCreate", "IDCardUpdate", "PersonType", "IDCardStatus",
    # Campus Module
    "Campus", "CampusCreate", "CampusUpdate",
    # Leave requests (registration fix — was never listed here)
    "LeaveRequest", "LeaveBalance", "LeaveType", "LeaveRequestStatus",
    # HR workflow-depth additions
    "OvertimeRecord",
    "StaffGoal", "PerformanceFeedbackRequest", "PerformanceFeedback", "PerformanceImprovementPlan",
    "SuccessionPlan", "Department", "BenefitPlan",
    # Communication & Platform additions
    "PushSubscription", "NotificationPreference", "MessageAttachment",
    "Conversation", "ConversationParticipant", "FaqArticle",
    # Curriculum standards alignment
    "CurriculumStandard", "CurriculumStandardCreate", "CurriculumStandardUpdate",
    "TopicStandardLink", "TopicStandardLinkCreate",
    # Elective / track management
    "Track", "TrackCreate", "TrackUpdate",
    "TrackSubject", "TrackSubjectCreate",
    "StudentTrack", "StudentTrackCreate",
    # Customizable executive dashboards
    "DashboardLayout", "DashboardLayoutUpdate",
]
