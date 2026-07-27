Read `CLAUDE.md` before starting.

# Document Management

We're implementing the Document Management module for the Legal Case Management Platform.

## Objective

Implement a secure document management system that allows authorized users to upload, organize, version, preview, download, and archive documents associated with legal cases.

This feature is responsible only for document storage and metadata management.

Document OCR, indexing, embeddings, semantic search, and AI analysis will be implemented in future features.

Reuse the existing Authentication, Authorization (RBAC), User Management, and Case Management modules.

---

# Dependencies

Use the existing MinIO integration implemented in the Backend Foundation.

No additional dependencies should be installed unless absolutely necessary.

If new packages are required:

- Update `requirements.txt`
- Update `package.json`

---

# Backend

Implement a complete Document Management module.

Create:

- Document Service
- Document Repository
- Document Schemas
- Document API Router
- Document Validation
- MinIO Storage Service
- Document Utilities

Reuse the existing architecture.

---

# Document Model

Implement the Document entity.

Fields:

- id
- case_id
- original_filename
- stored_filename
- file_extension
- mime_type
- file_size
- storage_bucket
- storage_key
- category
- description
- version
- uploaded_by
- created_at
- updated_at
- deleted_at

Document metadata must be stored in PostgreSQL.

Document binary files must be stored in MinIO.

---

# Document Categories

Support the following categories:

- Contract
- Evidence
- Court Decision
- Pleading
- Correspondence
- Invoice
- Identity Document
- Other

The category list should be centralized and easily extendable.

---

# File Upload

Implement:

POST /api/v1/documents/upload

Requirements:

- Upload to MinIO.
- Store metadata in PostgreSQL.
- Associate the document with a Case.
- Validate uploaded file.
- Generate a unique storage filename.
- Preserve the original filename.

---

# File Download

Implement:

GET /api/v1/documents/{id}/download

Return the original file.

Respect RBAC permissions.

---

# Document Preview

Implement:

GET /api/v1/documents/{id}/preview

Support browser preview for supported file types.

If preview is unavailable, allow download instead.

---

# Update Document Metadata

Implement:

PATCH /api/v1/documents/{id}

Allow updating:

- Category
- Description

Do not modify binary content.

---

# Replace Document

Implement:

POST /api/v1/documents/{id}/replace

Requirements:

- Upload a new version.
- Preserve previous versions.
- Increment version number.
- Maintain complete version history.

Never overwrite previous files.

---

# Delete Document

Implement:

DELETE /api/v1/documents/{id}

Use soft delete.

Do not immediately remove the file from MinIO.

Future cleanup jobs can permanently remove archived files.

---

# Versioning

Implement document versioning.

Each upload replacement creates:

Version 1

↓

Version 2

↓

Version 3

Maintain:

- Version Number
- Upload Date
- Uploaded By

Users should be able to access previous versions.

---

# Search

Support metadata search by:

- Original Filename
- Category
- Description

Search should be case insensitive.

Do not implement OCR or semantic search.

---

# Filtering

Support filtering by:

- Category
- Uploaded By
- Upload Date
- File Type

Filters should be combinable.

---

# Sorting

Support sorting by:

- Upload Date
- File Name
- File Size
- Category
- Version

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

# File Validation

Validate:

- Maximum file size
- Allowed file types
- Missing files
- Empty files
- Corrupted uploads

Return meaningful validation errors.

---

# Supported File Types

At minimum support:

- PDF
- DOCX
- DOC
- TXT
- JPG
- JPEG
- PNG

The list should be configurable.

---

# MinIO Integration

Implement:

- Upload Object
- Download Object
- Delete Object (logical only)
- Retrieve Metadata

Handle MinIO failures gracefully.

---

# Frontend

Implement:

- Document List Page
- Upload Dialog
- Document Details Dialog
- Replace Document Dialog
- Delete Confirmation Dialog

Use only components from the Design System.

---

# Document Table

Display:

- File Name
- Category
- File Size
- Version
- Uploaded By
- Upload Date
- Actions

---

# Actions

Support:

- View Details
- Preview
- Download
- Replace
- Delete

---

# Upload Form

Allow users to:

- Select file
- Select category
- Add description

Display upload progress.

Handle upload failures gracefully.

---

# Version History

Display:

- Version Number
- Upload Date
- Uploaded By

Allow downloading previous versions.

---

# Empty States

Display meaningful empty states when:

- No documents exist.
- Search returns no results.

---

# Loading States

Implement:

- Skeleton loaders
- Upload progress indicator
- Download loading state

---

# Permissions

Reuse the RBAC system.

Examples:

Administrator

- documents:view
- documents:upload
- documents:update
- documents:delete

Lawyer

- Upload documents to assigned cases.
- View documents for assigned cases.
- Download assigned documents.

Court Representative

- Upload court documents.
- View assigned case documents.
- Download assigned documents.

Use centralized permission checks.

Do not hardcode role names.

---

# Logging

Log:

- Upload
- Download
- Replace
- Delete
- Preview

Do not log file contents.

Do not log sensitive document information.

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

- Upload tests
- Download tests
- Versioning tests
- Validation tests
- Search tests
- Filter tests
- Pagination tests
- Authorization tests

## Frontend

Test:

- Upload
- Preview
- Download
- Replace
- Delete
- Search
- Filters
- Unauthorized access

---

# Validation Checklist

Before finishing, verify:

- Documents upload successfully.
- Files are stored in MinIO.
- Metadata is stored in PostgreSQL.
- Downloads work.
- Preview works for supported file types.
- Versioning works.
- Previous versions remain accessible.
- Search works.
- Filtering works.
- Pagination works.
- RBAC restrictions are enforced.
- OpenAPI documentation is updated.
- No TypeScript errors.
- No linting errors.
- No failing tests.
- No runtime errors.

---

# Out of Scope

Do NOT implement:

- OCR
- Text Extraction
- Embeddings
- Vector Storage
- Semantic Search
- AI Assistant
- AI Report Generation
- Automatic Document Classification
- Automatic Summarization
- Timeline Events
- Notifications

These features will be implemented separately.

---

# Implementation Constraints

Do not modify the existing project architecture.

Do not rename existing files or folders.

Reuse the existing Authentication, Authorization, User Management, Case Management, and MinIO infrastructure.

Store binary files only in MinIO.

Store metadata only in PostgreSQL.

Do not duplicate business logic.

Keep the implementation modular and reusable.

Prepare the module so future OCR, indexing, AI, and search features can integrate without requiring structural changes.

Do not implement functionality outside the scope of this feature.

When this feature is complete, stop implementation and wait for the next feature specification.