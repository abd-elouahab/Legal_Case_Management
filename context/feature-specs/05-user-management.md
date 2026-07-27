Read `CLAUDE.md` before starting.

# User Management

We're implementing the User Management module for the Legal Case Management Platform.

## Objective

Implement a complete user management system that allows administrators to create, manage, update, deactivate, and search users.

This feature must integrate with the existing Authentication and Authorization (RBAC) systems.

Only administrators should have access to this module.

---

## Dependencies

No additional dependencies should be required.

If additional packages become necessary:

- Install only if absolutely required.
- Update `requirements.txt`.
- Update `package.json` if frontend dependencies are added.

---

# Backend

Implement a complete User Management module.

Create:

- User Service
- User Repository
- User Schemas
- User API Router
- User Validation
- User Utilities

Reuse the existing Authentication and RBAC systems.

---

# User Model

Implement the complete User entity.

Fields:

- id
- first_name
- last_name
- email
- password_hash
- phone
- profile_image
- role
- status
- last_login
- created_at
- updated_at
- created_by
- updated_by

The password must never be returned in API responses.

---

# User Status

Support the following account statuses:

- Active
- Inactive
- Suspended

Status should determine whether the user can authenticate.

---

# User CRUD

Implement the following endpoints.

## List Users

GET /api/v1/users

Support:

- Pagination
- Search
- Sorting
- Filtering

---

## Get User

GET /api/v1/users/{id}

Return complete user information.

---

## Create User

POST /api/v1/users

Administrator creates a new user.

Requirements:

- Validate email uniqueness.
- Generate hashed password.
- Assign role.
- Assign status.
- Store audit information.

---

## Update User

PATCH /api/v1/users/{id}

Allow updating:

- Personal information
- Phone
- Profile image
- Role
- Status

Do not allow changing the password through this endpoint.

---

## Deactivate User

DELETE /api/v1/users/{id}

Use soft delete.

Do not permanently remove users from the database.

Mark the account as inactive.

---

# Password Management

Implement:

## Reset Password

POST /api/v1/users/{id}/reset-password

Administrator resets a user's password.

Generate a temporary password.

Hash the password before storing.

Require the user to change it on the next login.

---

## Force Password Change

Support forcing password change during the next authentication.

---

# Validation

Validate:

- Email format
- Unique email
- Required fields
- Phone format (if provided)
- Role exists
- Status exists

Return meaningful validation errors.

---

# Search

Support searching users by:

- First name
- Last name
- Email

Search should be case insensitive.

---

# Filtering

Support filtering by:

- Role
- Status

Filters should be combinable.

---

# Sorting

Support sorting by:

- Name
- Email
- Created Date
- Last Login

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

Implement a complete User Management interface.

Create:

- User List Page
- User Details Page
- Create User Dialog
- Edit User Dialog
- Delete Confirmation Dialog

Use only components from the Design System.

---

# User Table

Display:

- Avatar
- Full Name
- Email
- Role
- Status
- Last Login
- Created Date
- Actions

---

# Actions

Support:

- View
- Edit
- Reset Password
- Activate
- Deactivate

---

# User Form

Create reusable forms for:

- Create User
- Edit User

Fields:

- First Name
- Last Name
- Email
- Phone
- Role
- Status

Display validation errors.

---

# User Details

Display:

- Personal Information
- Contact Information
- Role
- Status
- Created Date
- Updated Date
- Last Login

---

# Empty States

Display meaningful empty states when:

- No users exist.
- Search returns no results.

---

# Loading States

Implement:

- Skeleton loaders
- Loading indicators

---

# Error Handling

Handle:

- Duplicate email
- User not found
- Validation errors
- Unauthorized access
- Forbidden access
- Internal server errors

Return consistent API responses.

---

# Permissions

Reuse the RBAC system.

Only users with the appropriate permissions should access this module.

Examples:

- users:view
- users:create
- users:update
- users:delete

Do not hardcode role names inside the UI.

Use permission checks.

---

# Audit Information

Store:

- Created By
- Updated By
- Created At
- Updated At

Automatically populate audit fields.

---

# Logging

Log:

- User creation
- User updates
- Password resets
- User deactivation

Do not log:

- Passwords
- Tokens
- Sensitive information

---

# API Documentation

Update the OpenAPI documentation.

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

- User CRUD tests
- Validation tests
- Authorization tests
- Password reset tests
- Pagination tests
- Search tests
- Filter tests

## Frontend

Test:

- User creation
- User editing
- User deletion
- Search
- Filters
- Pagination
- Unauthorized access

---

# Validation Checklist

Before finishing, verify:

- Users can be created.
- Users can be edited.
- Users can be viewed.
- Users can be deactivated.
- Password reset works.
- Email uniqueness is enforced.
- Passwords are hashed.
- Search works.
- Filtering works.
- Pagination works.
- Sorting works.
- Audit fields are populated.
- Only authorized users can access this module.
- OpenAPI documentation is updated.
- No TypeScript errors.
- No linting errors.
- No failing tests.
- No runtime errors.

---

# Out of Scope

Do NOT implement:

- Case Management
- Document Management
- Timeline
- Notifications
- OCR
- AI Assistant
- Reports
- Dashboard
- Search Engine
- Email Notifications
- WhatsApp Integration

These features will be implemented separately.

---

# Implementation Constraints

Do not modify the existing project architecture.

Do not rename existing files or folders.

Reuse the existing Authentication and RBAC systems.

Reuse shared UI components from the Design System.

Do not duplicate business logic.

Keep the implementation modular and reusable.

Do not implement functionality outside the scope of this feature.

When this feature is complete, stop implementation and wait for the next feature specification.