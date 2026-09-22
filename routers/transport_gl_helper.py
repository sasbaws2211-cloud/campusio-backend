"""
GL Posting Helper for Transport Fees - Appended to transport.py

This function should be added to the END of routers/transport.py file
"""

# ============================================================================
# GL AUTO-POSTING HELPER FOR TRANSPORT FEES
# ============================================================================

async def _create_transport_journal_entry(
    session: AsyncSession,
    school_id: str,
    fee_id: str,
    student_id: str,
    route_id: str,
    payment_amount: float,
    payment_method: str
) -> str:
    """
    Create GL journal entry for transport fee payment.
    
    Posts:
    - Dr. GL account based on payment method (1001=Cash, 1010=Bank, 1015=Mobile)
    - Cr. 4155 (Transport Fee Revenue)
    
    Args:
        session: AsyncSession
        school_id: School identifier
        fee_id: Transport fee ID for reference
        student_id: Student ID
        route_id: Route ID
        payment_amount: Amount paid
        payment_method: Payment method (cash, bank_transfer, mobile_money, cheque)
        
    Returns:
        Journal entry ID
        
    Raises:
        Exception: If GL accounts not found or GL posting fails
    """
    from services.journal_entry_service import JournalEntryService
    from models.finance import JournalEntryCreate, JournalLineItemCreate, ReferenceType
    from models.finance.chart_of_accounts import GLAccount
    
    # Map payment method to GL bank account
    GL_BANK_ACCOUNTS = {
        "cash": "1001",              # Cash in Hand
        "bank_transfer": "1010",     # Business Checking Account
        "bank": "1010",              # Alias
        "mobile_money": "1015",      # Mobile Money Account
        "mobile": "1015",            # Alias
        "cheque": "1010",            # Bank Account
    }
    
    GL_TRANSPORT_REVENUE = "4155"   # Transport Fee Revenue
    
    # Get bank account code based on payment method
    bank_account_code = GL_BANK_ACCOUNTS.get(payment_method.lower(), "1001")
    
    try:
        # Get bank GL account
        bank_result = await session.execute(
            select(GLAccount).where(
                and_(
                    GLAccount.school_id == school_id,
                    GLAccount.account_code == bank_account_code,
                    GLAccount.is_active == True
                )
            )
        )
        bank_account = bank_result.scalar_one_or_none()
        
        if not bank_account:
            raise Exception(f"GL Account {bank_account_code} ({payment_method}) not found or inactive")
        
        # Get revenue GL account
        revenue_result = await session.execute(
            select(GLAccount).where(
                and_(
                    GLAccount.school_id == school_id,
                    GLAccount.account_code == GL_TRANSPORT_REVENUE,
                    GLAccount.is_active == True
                )
            )
        )
        revenue_account = revenue_result.scalar_one_or_none()
        
        if not revenue_account:
            raise Exception(f"GL Account {GL_TRANSPORT_REVENUE} (Transport Revenue) not found or inactive")
        
        # Get student for description
        student_result = await session.execute(
            select(Student).where(Student.id == student_id)
        )
        student = student_result.scalar_one_or_none()
        student_name = f"{student.first_name} {student.last_name}" if student else "Unknown"
        
        # Get route for description
        route_result = await session.execute(
            select(Route).where(Route.id == route_id)
        )
        route = route_result.scalar_one_or_none()
        route_name = route.route_name if route else "Unknown Route"
        
        # Create journal line items
        line_items = [
            # Debit: Bank account (payment received)
            JournalLineItemCreate(
                gl_account_id=bank_account.id,
                debit_amount=float(payment_amount),
                credit_amount=0.0,
                description=f"Transport fee payment from {student_name} ({route_name})"
            ),
            # Credit: Revenue account (fee income)
            JournalLineItemCreate(
                gl_account_id=revenue_account.id,
                debit_amount=0.0,
                credit_amount=float(payment_amount),
                description=f"Transport fee income from {student_name} ({route_name})"
            )
        ]
        
        # Create journal entry
        entry_data = JournalEntryCreate(
            entry_date=datetime.utcnow().isoformat().split('T')[0],
            reference_type=ReferenceType.TRANSPORT_FEES,
            reference_id=fee_id,
            description=f"Transport fee payment from {student_name} ({route_name})",
            line_items=line_items,
            notes=f"Auto-posted from transport fee {fee_id} - Payment method: {payment_method}"
        )
        
        # Use JournalEntryService to create and post entry
        journal_service = JournalEntryService(session)
        entry = await journal_service.create_entry(
            school_id=school_id,
            entry_data=entry_data,
            created_by="SYSTEM"
        )
        
        # Post the entry immediately
        posted_entry = await journal_service.post_entry(
            school_id=school_id,
            entry_id=entry.id,
            posted_by="SYSTEM",
            approval_notes="Auto-posted from transport fee payment"
        )

        return posted_entry.id
        
    except Exception as e:
        logger.error(f"Error creating transport fee journal entry: {str(e)}")
        raise
