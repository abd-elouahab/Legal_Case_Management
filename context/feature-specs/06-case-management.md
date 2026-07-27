Read `CLAUDE.md` before starting.

# Case Management

We're implementing the Case Management module for the Legal Case Management Platform.

## Objective

Implement the core Case Management system that allows authorized users to create, manage, assign, update, search, and archive legal cases.

This feature establishes the central business entity of the platform.

All future modules (Documents, Timeline, OCR, AI, Notifications, Reports, etc.) will be linked to a Case.

Reuse the existing Authentication, Authorization (RBAC), and User Management modules.

---

# Dependencies

No additional dependencies should be required.

If additional packages become necessary:

- Install only if absolutely required.
- Update `requirements.txt`.
- Update `package.json` if frontend dependencies are added.

---

# Backend

Implement a complete Case Management module.

Create:

- Case Service
- Case Repository
- Case Schemas
- Case API Router
- Case Validation
- Case Utilities

Reuse existing architecture patterns.

---

# Case Model

Implement the Case entity.

Fields:

- id
- case_number
- title
- description
- category
- status
- priority
- court_name
- filing_date
- next_hearing_date
- assigned_lawyer_id
- assigned_court_representative_id
- created_by
- updated_by
- created_at
- updated_at

Case numbers must be unique.

---

# Case Status

Implement the following statuses:

- Draft
- Open
- In Progress
- Waiting for Hearing
- Closed
- Archived

Support status transitions.

Prevent invalid transitions where appropriate.

---

# Case Priority

Support:

- Low
- Medium
- High
- Urgent

Priority should be editable.

---

# CRUD Operations

Implement the following endpoints.

## List Cases

GET /api/v1/cases

Support:

- Pagination
- Search
- Sorting
- Filtering

---

## Get Case

GET /api/v1/cases/{id}

Return complete case information.

---

## Create Case

POST /api/v1/cases

Requirements:

- Generate unique case number if not provided.
- Validate required fields.
- Assign lawyer.
- Assign court representative.
- Store audit information.

---

## Update Case

PATCH /api/v1/cases/{id}

Allow updating:

- General information
- Status
- Priority
- Court
- Assignments
- Hearing date

Do not allow changing immutable fields.

---

## Archive Case

DELETE /api/v1/cases/{id}

Use soft delete.

Archived cases should remain searchable.

---

# Assignment

Support:

## Lawyer Assignment

- Assign Lawyer
- Change Lawyer
- Remove Lawyer

---

## Court Representative Assignment

- Assign Representative
- Change Representative
- Remove Representative

Assignments should validate user roles.

---

# Search

Support searching by:

- Case Number
- Title
- Description
- Court Name

Search should be case insensitive.

---

# Filtering

Support filtering by:

- Status
- Priority
- Assigned Lawyer
- Assigned Court Representative
- Court
- Filing Date
- Hearing Date

Filters should be combinable.

---

# Sorting

Support sorting by:

- Case Number
- Created Date
- Updated Date
- Filing Date
- Hearing Date
- Priority

Ascending and descending order should be supported.

---

# Pagination

Support:

- Page
- Page Size

Return:

- Total Records
- Current Page
- Total Pages

---

# Frontend

Implement a complete Case Management interface.

Create:

- Case List Page
- Case Details Page
- Create Case Dialog
- Edit Case Dialog
- Archive Confirmation Dialog

Use only components from the Design System.

---

# Case Table

Display:

- Case Number
- Title
- Status
- Priority
- Court
- Assigned Lawyer
- Assigned Representative
- Filing Date
- Next Hearing
- Last Updated
- Actions

---

# Case Details

Display:

## General Information

- Case Number
- Title
- Description
- Category
- Status
- Priority

---

## Assignment

Display:

- Assigned Lawyer
- Assigned Court Representative

---

## Court Information

Display:

- Court Name
- Filing Date
- Next Hearing Date

---

## Audit Information

Display:

- Created By
- Updated By
- Created At
- Updated At

---

## Placeholder Sections

Create placeholder cards for future modules:

- Documents
- Timeline
- Notes
- AI Assistant
- Reports

Do not implement their functionality.

Only prepare the layout.

---

# Case Form

Create reusable forms for:

- Create Case
- Edit Case

Validate:

- Required fields
- Dates
- Assignments

Display validation errors.

---

# Empty States

Display meaningful empty states when:

- No cases exist.
- Search returns no results.

---

# Loading States

Implement:

- Skeleton loaders
- Loading indicators

---

# Permissions

Reuse the RBAC system.

Examples:

Administrator:

- cases:create
- cases:view
- cases:update
- cases:delete
- cases:assign

Lawyer:

- View assigned cases
- Update assigned cases where permitted

Court Representative:

- View assigned cases
- Update hearing-related information where permitted

Do not hardcode role names inside components.

Use centralized permission checks.

---

# Validation

Validate:

- Unique case number
- Existing assigned users
- Valid status
- Valid priority
- Valid dates

Return meaningful validation errors.

---

# Logging

Log:

- Case creation
- Case updates
- Assignment changes
- Status changes
- Archive operations

Do not log sensitive information.

---

# API Documentation

Update OpenAPI documentation.

Every endpoint should include:

- Summary
- Description
- Request schema
- Response schema
- Error responses

---

# Testing

Implement:

## Backend

- CRUD tests
- Assignment tests
- Validation tests
- Search tests
- Filter tests
- Pagination tests
- Authorization tests

## Frontend

Test:

- Case creation
- Case editing
- Case assignment
- Search
- Filters
- Pagination
- Unauthorized access

---

# Validation Checklist

Before finishing, verify:

- Cases can be created.
- Cases can be edited.
- Cases can be archived.
- Case numbers are unique.
- Assignments work correctly.
- Search works.
- Filtering works.
- Sorting works.
- Pagination works.
- Status transitions work.
- Audit fields are populated.
- RBAC restrictions are enforced.
- OpenAPI documentation is updated.
- No TypeScript errors.
- No linting errors.
- No failing tests.
- No runtime errors.

---

# Out of Scope

Do NOT implement:

- Document Uploads
- OCR
- Timeline Management
- Notes
- AI Assistant
- AI Report Generation
- Email Notifications
- WhatsApp Integration
- Real-Time Synchronization
- Dashboard Analytics
- Search Engine
- Localization

These features will be implemented separately.

---

# Implementation Constraints

Do not modify the existing project architecture.

Do not rename existing files or folders.

Reuse the existing Authentication, Authorization, and User Management modules.

Reuse shared UI components from the Design System.

Do not duplicate business logic.

Keep the implementation modular and reusable.

Prepare the module so future features can attach to a Case without requiring structural changes.

Do not implement functionality outside the scope of this feature.

When this feature is complete, stop implementation and wait for the next feature specification.