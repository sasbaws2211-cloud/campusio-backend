# E-Library Module Implementation Plan

## Goal
Build a standard school e-library module that lets school admins manage digital learning resources and lets students browse, read, and watch them from the student portal.

## Why this fits the current workspace
The current codebase already has the right building blocks:
- Backend: FastAPI app with modular routers and file upload support via the existing uploads folder.
- Frontend: role-based routing, a persistent app shell, and a student portal page that can host a new tab.
- Existing data patterns: the backend already has models for learning materials and teacher resources, which can be adapted for a more complete library system.

## Recommended architecture

### 1) Backend
Create a dedicated library module with these components:
- Router: /api/library
- Models:
  - LibraryItem
  - LibraryCategory
  - Optional: LibraryItemClassAccess for class-level visibility
- File handling:
  - Upload files into /uploads/library
  - Serve them through the existing /uploads static mount

### Suggested fields for LibraryItem
- id
- school_id
- title
- description
- content_type: pdf, video, ebook, audio, link
- material_type: book, textbook, academic_material, educational_video, worksheet, reference
- category_id
- subject_id
- class_ids (JSON or relation table)
- file_url
- thumbnail_url
- preview_url
- tags
- is_published
- is_featured
- access_level: school, class, selected_classes
- created_by
- updated_by
- created_at
- updated_at

### Suggested fields for LibraryCategory
- id
- school_id
- name
- slug
- parent_id
- description

### API endpoints
- GET /api/library/categories
- POST /api/library/categories
- GET /api/library/items
- GET /api/library/items/{id}
- POST /api/library/items (multipart upload)
- PUT /api/library/items/{id}
- DELETE /api/library/items/{id}
- GET /api/student-portal/library

### Access rules
- School admin and super admin: full CRUD access
- Teachers: read access, optional upload rights if needed later
- Students: read-only access

## 2) Frontend

### Admin/library management page
Create a dedicated page for school admins:
- Route: /library
- Features:
  - Add new resource
  - Edit existing resource
  - Delete resource
  - Upload PDF/video/file
  - Choose category, material type, class level, subject, and tags
  - Filter by type, category, class, subject, and search term
  - Preview resource before publishing

### Student portal e-library tab
Extend the student portal with a new tab such as E-Library:
- Browse by category and class
- Search across materials
- Filter by type: books, textbooks, academic materials, videos
- Open PDFs in an embedded viewer
- Play videos in a built-in player
- Show recent and featured items

## 3) Recommended UI experience
### Admin experience
- A card/table view with pagination
- A modal form for create/edit
- A filter bar for quick browsing
- A publish/unpublish toggle
- Clear status badges: Draft, Published, Archived

### Student experience
- A clean catalog with cards and category chips
- Each item shows:
  - title
  - type
  - category
  - class relevance
  - preview image/thumbnail
- Clicking an item opens a detail view with:
  - description
  - read/watch buttons
  - file metadata

## 4) Best implementation approach for this repo
Use a new module rather than forcing this into assignments or teacher resources. That gives you cleaner permissions, better UI, and easier future growth.

### Best fit for the existing codebase
- Backend: add a dedicated router and models, then register it in the main FastAPI app.
- Frontend: add a new admin page and a student-portal section using the established API helper and toast patterns.
- Storage: reuse the existing uploads handling and static file serving.

## 5) Suggested file additions
### Backend
- campusio_backend/models/library.py
- campusio_backend/routers/library.py
- Register the router in server.py
- Add Alembic migration for the new tables

### Frontend
- campusio_frontend/src/pages/library/LibraryAdminPage.js
- campusio_frontend/src/pages/student/StudentELibrarySection.js
- campusio_frontend/src/services/libraryService.js
- Add route imports in App.js
- Add sidebar/portal navigation entries in Layout.js
- Add CSS for the library UI

## 6) Recommended rollout order
1. MVP backend CRUD and uploads
2. Admin UI for add/edit/delete and filtering
3. Student portal catalog and viewer
4. Advanced features: featured items, recommendations, download tracking, ratings, and analytics

## 7) Recommended MVP scope
To ship quickly and reliably, start with:
- PDF and video upload support
- Categories and filters
- Class-level visibility
- Admin CRUD
- Student read/watch access

This is the best balance between value, speed, and maintainability for the current Campusio structure.
