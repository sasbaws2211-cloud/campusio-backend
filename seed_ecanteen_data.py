"""Seed e-canteen data for the School ERP System.

Creates:
- canteen items
- a student canteen wallet account
- ledger entries for top-up and purchase

This script is idempotent and safe to rerun.
"""
import asyncio
import sys
from pathlib import Path
from datetime import datetime

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

from sqlmodel import select
from database import async_session, init_db
from models.user import User
from models.school import School
from models.student import Student
from models.canteen_wallet import CanteenItem, CanteenWalletAccount, CanteenWalletLedgerEntry

ITEM_DEFINITIONS = [
    {
        "name": "School Lunch Box",
        "item_type": "food",
        "price": 12.50,
        "info": "A balanced lunch box with rice, vegetables and protein.",
        "code": "FOOD-LUNCH-01",
    },
    {
        "name": "Fruit Juice",
        "item_type": "drink",
        "price": 4.00,
        "info": "Fresh fruit juice served in a sealed bottle.",
        "code": "DRNK-JUIC-01",
    },
    {
        "name": "Bread and Egg Sandwich",
        "item_type": "food",
        "price": 6.75,
        "info": "Whole wheat bread sandwich with egg and veggies.",
        "code": "FOOD-SAND-01",
    },
    {
        "name": "Exercise Notebook",
        "item_type": "stationery",
        "price": 8.00,
        "info": "A4 notebook for class work and assignments.",
        "code": "STNY-NOTE-01",
    },
    {
        "name": "Snack Pack",
        "item_type": "food",
        "price": 3.50,
        "info": "Assorted snack pack for school break time.",
        "code": "FOOD-SNCK-01",
    },
]


async def get_school_from_admin(session):
    result = await session.exec(select(User).where(User.email == "admin@school.edu.gh"))
    admin = result.first()
    if not admin:
        return None, None

    result = await session.exec(select(School).where(School.id == admin.school_id))
    school = result.first()
    return school, admin


async def get_student_and_parent_user_ids(session):
    student_user = None
    parent_user = None

    result = await session.exec(select(User).where(User.email == "student@school.edu.gh"))
    student_user = result.first()

    result = await session.exec(select(User).where(User.email == "parent@school.edu.gh"))
    parent_user = result.first()

    if not student_user:
        return None, None, None

    result = await session.exec(select(Student).where(Student.user_id == student_user.id))
    student = result.first()
    return student, student_user, parent_user


async def create_canteen_items(session, school):
    created = []
    for item_def in ITEM_DEFINITIONS:
        result = await session.exec(
            select(CanteenItem).where(
                (CanteenItem.school_id == school.id) &
                (CanteenItem.code == item_def["code"])
            )
        )
        item = result.first()
        if item:
            continue

        item = CanteenItem(
            school_id=school.id,
            name=item_def["name"],
            item_type=item_def["item_type"],
            price=item_def["price"],
            info=item_def["info"],
            code=item_def["code"],
            is_active=True,
        )
        session.add(item)
        await session.flush()
        created.append(item)

    return created


async def create_wallet_account(session, school, student, parent_user):
    if not student:
        return None

    result = await session.exec(
        select(CanteenWalletAccount).where(
            (CanteenWalletAccount.school_id == school.id) &
            (CanteenWalletAccount.student_id == student.id)
        )
    )
    account = result.first()
    if not account:
        account = CanteenWalletAccount(
            school_id=school.id,
            student_id=student.id,
            parent_id=parent_user.id if parent_user else None,
            balance=30.00,
            pending_parent_settlement=0.0,
        )
        session.add(account)
        await session.flush()
    else:
        if account.balance < 30.00:
            account.balance = 30.00
            session.add(account)
    return account


async def create_wallet_ledger_entries(session, account):
    if not account:
        return []

    created = []

    entries = [
        {
            "event_type": "parent_topup",
            "amount": 30.00,
            "reference": "TOPUP-ECANTEEN-01",
            "description": "Initial wallet top-up for e-canteen purchases.",
        },
        {
            "event_type": "student_purchase",
            "amount": -6.75,
            "reference": "PURCHASE-SANDWICH-01",
            "description": "Purchase of a bread and egg sandwich.",
        },
        {
            "event_type": "student_purchase",
            "amount": -4.00,
            "reference": "PURCHASE-JUICE-01",
            "description": "Purchase of fruit juice.",
        },
    ]

    for entry_data in entries:
        result = await session.exec(
            select(CanteenWalletLedgerEntry).where(
                (CanteenWalletLedgerEntry.wallet_id == account.id) &
                (CanteenWalletLedgerEntry.reference == entry_data["reference"])
            )
        )
        ledger_entry = result.first()
        if ledger_entry:
            continue

        ledger_entry = CanteenWalletLedgerEntry(
            wallet_id=account.id,
            event_type=entry_data["event_type"],
            amount=entry_data["amount"],
            reference=entry_data["reference"],
            description=entry_data["description"],
        )
        session.add(ledger_entry)
        await session.flush()
        created.append(ledger_entry)

    return created


async def seed_ecanteen_data():
    await init_db()

    async with async_session() as session:
        school, admin = await get_school_from_admin(session)
        if not school or not admin:
            print("No admin user or school found. Run seed_data.py first.")
            return

        student, student_user, parent_user = await get_student_and_parent_user_ids(session)
        if not student or not student_user:
            print("No student user found. Run seed_data.py first.")
            return

        print(f"Seeding e-canteen data for school: {school.name}")
        print(f"Student: {student_user.email}")

        created_items = await create_canteen_items(session, school)
        account = await create_wallet_account(session, school, student, parent_user)
        created_ledger_entries = await create_wallet_ledger_entries(session, account)

        await session.commit()

        print("\n✓ E-canteen seed completed")
        print(f"Canteen items created: {len(created_items)}")
        print(f"Wallet account created/updated: {account.id if account else 'none'}")
        print(f"Ledger entries created: {len(created_ledger_entries)}")


if __name__ == "__main__":
    asyncio.run(seed_ecanteen_data())
