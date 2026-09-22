"""CSV Import Service for bulk student and staff uploads"""
import traceback
import pandas as pd
import io
from typing import List, Dict, Any, Optional
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from datetime import datetime
import uuid

from models.student import Student, StudentCreate, Gender, StudentStatus, StudentEnrollment
from models.staff import Staff, StaffCreate, StaffType, StaffStatus
from models.classroom import Class, ClassLevel
from models.school import AcademicTerm
from models.user import User


class CSVImportService:
    def __init__(self, session: AsyncSession, school_id: str, current_user: User):
        self.session = session
        self.school_id = school_id
        self.current_user = current_user
        self.errors = []
        self.success_count = 0

    from sqlalchemy import func

    from sqlalchemy import func

    async def _get_class_id_by_name(self, class_name: str):
     if not class_name:
        return None

   

    # ✅ Match by enum VALUE (level)
     try:
        enum_value = class_name.lower()
        level = ClassLevel(enum_value)
        result = await self.session.execute(
            select(Class).where(
                Class.school_id == self.school_id,
                Class.level == level
            )
        )
        cls = result.scalars().first()
        if cls:
            return cls.id
     except ValueError:
        pass

     return None

    def _validate_date(self, date_str: str) -> Optional[str]:
        """Validate and format date string"""
        if not date_str:
            return None
        try:
            # Try different date formats
            for fmt in ['%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y']:
                try:
                    dt = datetime.strptime(date_str.strip(), fmt)
                    return dt.strftime('%Y-%m-%d')
                except ValueError:
                    continue
            raise ValueError(f"Invalid date format: {date_str}")
        except Exception as e:
            raise ValueError(f"Invalid date: {date_str} - {str(e)}")

    def _validate_gender(self, gender_str: str) -> Gender:
        """Validate and convert gender"""
        if not gender_str:
            raise ValueError("Gender is required")

        gender_map = {
            'male': Gender.MALE,
            'female': Gender.FEMALE,
            'm': Gender.MALE,
            'f': Gender.FEMALE,
            'boy': Gender.MALE,
            'girl': Gender.FEMALE,
        }

        gender_lower = gender_str.lower().strip()
        if gender_lower in gender_map:
            return gender_map[gender_lower]

        raise ValueError(f"Invalid gender: {gender_str}. Must be 'male' or 'female'")

    async def import_students(self, csv_content: bytes) -> Dict[str, Any]:
        """Import students from CSV"""
        self.errors = []
        self.success_count = 0

        try:
            # Read CSV
            df = pd.read_csv(io.BytesIO(csv_content))

            # Validate required columns (must be present in CSV)
            required_columns = ['student_id', 'first_name', 'last_name', 'date_of_birth', 'gender', 'admission_date']
            missing_columns = [col for col in required_columns if col not in df.columns]
            if missing_columns:
                return {
                    'success': False,
                    'message': f'Missing required columns: {", ".join(missing_columns)}',
                    'errors': self.errors,
                    'success_count': 0
                }

            # Optional columns that may exist
            optional_columns = ['other_names', 'class', 'address', 'nationality', 'religion', 'blood_group', 'medical_conditions', 'photo_url']

            # Resolve once up front so each row doesn't re-query for it;
            # skipped (no enrollment rows written) if the school has no
            # current term configured yet, same tolerance as the students router.
            term_result = await self.session.execute(
                select(AcademicTerm).where(AcademicTerm.school_id == self.school_id, AcademicTerm.is_current == True)  # noqa: E712
            )
            current_term = term_result.scalar_one_or_none()
            current_term_id = current_term.id if current_term else None

            # Process each row
            for index, row in df.iterrows():
                try:
                    # Check for duplicate student_id
                    result = await self.session.execute(
                        select(Student).where(
                            Student.school_id == self.school_id,
                            Student.student_id == str(row['student_id']).strip()
                        )
                    )
                    if result.scalars().first():
                        self.errors.append(f"Row {index + 2}: Student ID '{row['student_id']}' already exists")
                        continue

                    # Get class ID
                    class_id = None
                    if 'class' in row and pd.notna(row['class']):
                        class_id = await self._get_class_id_by_name(str(row['class']))
                        if not class_id:
                            self.errors.append(f"Row {index + 2}: Class '{row['class']}' not found")
                            continue
                        # check_class_capacity is the same capacity guard every
                        # other student-enrollment path uses (admission
                        # conversion, rollover, direct create) — without it a
                        # CSV import can silently over-fill a class past its
                        # configured capacity.
                        from routers.students import check_class_capacity  # deferred: avoids import cycle (students.py imports this module)
                        try:
                            await check_class_capacity(self.session, self.school_id, class_id)
                        except HTTPException as exc:
                            self.errors.append(f"Row {index + 2}: {exc.detail}")
                            continue

                    # Create student data
                    student_data = StudentCreate(
                        student_id=str(row['student_id']).strip(),
                        first_name=str(row['first_name']).strip(),
                        last_name=str(row['last_name']).strip(),
                        other_names=str(row['other_names']).strip() if 'other_names' in row and pd.notna(row['other_names']) else None,
                        date_of_birth=self._validate_date(str(row['date_of_birth'])),
                        gender=self._validate_gender(str(row['gender'])),
                        admission_date=self._validate_date(str(row['admission_date'])),
                        class_id=class_id,
                        address=str(row['address']).strip() if 'address' in row and pd.notna(row['address']) else None,
                        nationality=str(row['nationality']).strip() if 'nationality' in row and pd.notna(row['nationality']) else 'Ghanaian',
                        religion=str(row['religion']).strip() if 'religion' in row and pd.notna(row['religion']) else None,
                        blood_group=str(row['blood_group']).strip() if 'blood_group' in row and pd.notna(row['blood_group']) else None,
                        medical_conditions=str(row['medical_conditions']).strip() if 'medical_conditions' in row and pd.notna(row['medical_conditions']) else None,
                        photo_url=str(row['photo_url']).strip() if 'photo_url' in row and pd.notna(row['photo_url']) else None,
                    )

                    # Create student
                    student = Student(school_id=self.school_id, **student_data.model_dump())
                    self.session.add(student)

                    if class_id and current_term_id:
                        self.session.add(StudentEnrollment(
                            school_id=self.school_id,
                            student_id=student.id,
                            class_id=class_id,
                            academic_term_id=current_term_id,
                        ))

                    self.success_count += 1

                except Exception as e:
                    self.errors.append(f"Row {index + 2}: {str(e)}")

            await self.session.commit()

            message = f'Imported {self.success_count} students successfully'
            if self.errors:
                message = f"{message} with {len(self.errors)} errors"

            return {
                'success': len(self.errors) == 0,
                'message': message,
                'errors': self.errors,
                'success_count': self.success_count
            }

        except Exception as e:
            return {
                'success': False,
                'message': f'Import failed: {str(e)}',
                'errors': self.errors,
                'success_count': 0
            }

    async def import_staff(self, csv_content: bytes) -> Dict[str, Any]:
        """Import staff from CSV"""
        self.errors = []
        self.success_count = 0

        try:
            # Read CSV
            df = pd.read_csv(io.BytesIO(csv_content))

            # Validate required columns
            required_columns = ['staff_id', 'first_name', 'last_name', 'email', 'phone', 'date_of_birth', 'gender', 'staff_type', 'position', 'date_joined']
            missing_columns = [col for col in required_columns if col not in df.columns]
            if missing_columns:
                return {
                    'success': False,
                    'message': f'Missing required columns: {", ".join(missing_columns)}',
                    'errors': self.errors,
                    'success_count': 0
                }

            # Process each row
            for index, row in df.iterrows():
                try:
                    # Check for duplicate staff_id
                    result = await self.session.execute(
                        select(Staff).where(
                            Staff.school_id == self.school_id,
                            Staff.staff_id == str(row['staff_id']).strip()
                        )
                    )
                    if result.scalars().first():
                        self.errors.append(f"Row {index + 2}: Staff ID '{row['staff_id']}' already exists")
                        continue

                    # Check for duplicate email
                    result = await self.session.execute(
                        select(Staff).where(
                            Staff.school_id == self.school_id,
                            Staff.email == str(row['email']).strip().lower()
                        )
                    )
                    if result.scalars().first():
                        self.errors.append(f"Row {index + 2}: Email '{row['email']}' already exists")
                        continue

                    # Validate staff type
                    staff_type_str = str(row['staff_type']).strip().lower()
                    staff_type_map = {
                        'teaching': StaffType.TEACHING,
                        'non_teaching': StaffType.NON_TEACHING,
                        'admin': StaffType.ADMIN,
                        'teacher': StaffType.TEACHING,
                        'administrator': StaffType.ADMIN,
                        'support': StaffType.NON_TEACHING,
                    }

                    if staff_type_str not in staff_type_map:
                        self.errors.append(f"Row {index + 2}: Invalid staff type '{row['staff_type']}'. Must be 'teaching', 'non_teaching', or 'admin'")
                        continue

                    staff_type = staff_type_map[staff_type_str]

                    # Create staff data
                    staff_data = StaffCreate(
                        staff_id=str(row['staff_id']).strip(),
                        first_name=str(row['first_name']).strip(),
                        last_name=str(row['last_name']).strip(),
                        other_names=str(row['other_names']).strip() if 'other_names' in row and pd.notna(row['other_names']) else None,
                        email=str(row['email']).strip().lower(),
                        phone=str(row['phone']).strip(),
                        date_of_birth=self._validate_date(str(row['date_of_birth'])),
                        gender=str(row['gender']).strip().lower(),
                        staff_type=staff_type,
                        position=str(row['position']).strip(),
                        department=str(row['department']).strip() if 'department' in row and pd.notna(row['department']) else None,
                        qualification=str(row['qualification']).strip() if 'qualification' in row and pd.notna(row['qualification']) else None,
                        date_joined=self._validate_date(str(row['date_joined'])),
                        address=str(row['address']).strip() if 'address' in row and pd.notna(row['address']) else None,
                        photo_url=str(row['photo_url']).strip() if 'photo_url' in row and pd.notna(row['photo_url']) else None,
                    )

                    # Create staff
                    staff = Staff(school_id=self.school_id, **staff_data.model_dump())
                    self.session.add(staff)
                    self.success_count += 1

                except Exception as e:
                    self.errors.append(f"Row {index + 2}: {str(e)}")

            await self.session.commit()

            message = f'Imported {self.success_count} staff members successfully'
            if self.errors:
                message = f"{message} with {len(self.errors)} errors"

            return {
                'success': len(self.errors) == 0,
                'message': message,
                'errors': self.errors,
                'success_count': self.success_count
            }

        except Exception as e:
            return {
                'success': False,
                'message': f'Import failed: {str(e)}',
                'errors': self.errors,
                'success_count': 0
            }