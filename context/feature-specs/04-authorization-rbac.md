Read `CLAUDE.md` before starting.

# Authorization (RBAC)

We're implementing the Role-Based Access Control (RBAC) system for the Legal Case Management Platform.

## Objective

Implement a centralized authorization system that controls access to the platform based on user roles and permissions.

This feature should integrate with the existing authentication system but must not implement any business features.

---

## Dependencies

No additional dependencies should be required.

If additional packages become necessary:

- Install only if absolutely required.
- Update `requirements.txt`.
- Update `package.json` if frontend dependencies are added.

---

## Backend

Implement a complete RBAC module.

Create:

- Role definitions
- Permission definitions
- Authorization service
- Authorization dependencies
- Permission decorators/dependencies
- Permission utilities

The authorization system must be reusable across the entire application.

---

## Roles

Implement the following system roles:

- Administrator
- Lawyer
- Court Representative

Use a centralized role definition.

Do not hardcode role names throughout the codebase.

---

## Permissions

Implement a centralized permission system.

Every permission must have a unique identifier.

Suggested permissions include:

### User Management

- users:create
- users:view
- users:update
- users:delete

### Case Management

- cases:create
- cases:view
- cases:update
- cases:delete
- cases:assign

### Document Management

- documents:upload
- documents:view
- documents:update
- documents:delete

### Timeline

- timeline:view
- timeline:create

### Reports

- reports:view
- reports:generate

### Notifications

- notifications:view
- notifications:manage

### AI

- ai:chat
- ai:generate-report

### Settings

- settings:view
- settings:update

The permission system should be easily extensible.

---

## Role Permissions

Configure default permissions.

### Administrator

Has full access to the platform.

### Lawyer

Has access only to:

- Assigned cases
- Assigned documents
- AI Assistant
- Timeline
- Reports related to assigned cases

### Court Representative

Has access only to:

- Assigned cases
- Court updates
- Hearing management
- Timeline

These permissions will be refined by future features.

---

## Authorization

Implement reusable authorization checks.

Support:

- Require Role
- Require Permission
- Require Any Permission
- Require All Permissions

Authorization logic must be reusable by all future API endpoints.

---

## Route Protection

Protect backend endpoints using reusable authorization dependencies.

Endpoints should return:

- HTTP 401 for unauthenticated users
- HTTP 403 for unauthorized users

Never expose internal authorization logic.

---

## Frontend

Implement reusable authorization utilities.

Create:

- Permission Hook
- Role Hook
- Protected Component
- Protected Route
- Unauthorized Page

Navigation should automatically hide inaccessible items.

---

## Sidebar

Prepare role-aware navigation.

Navigation items should only appear if the current user has permission.

Do not hardcode permissions inside components.

Navigation should use the centralized permission system.

---

## UI Behavior

If a user attempts to access a page without permission:

- Display the Unauthorized page.
- Do not render protected content.

---

## API Integration

Authorization must integrate with the existing authentication system.

Authenticated users should automatically expose:

- Current Role
- Current Permissions

through the authentication context.

---

## Error Handling

Handle:

- Missing permissions
- Invalid roles
- Invalid permissions
- Unauthorized access

Return consistent JSON error responses.

---

## Logging

Log:

- Authorization failures
- Permission denials

Do not log sensitive user information.

---

## Code Quality

Follow:

- `CLAUDE.md`
- `code-standards.md`

Write reusable, centralized authorization logic.

Avoid duplicated permission checks.

---

## Out of Scope

Do NOT implement:

- User Management
- Case Management
- Document Management
- Dashboard
- Notifications
- OCR
- AI
- Reports
- Search

Do not assign users to roles through the UI.

That will be implemented in User Management.

---

## Testing

Implement:

- Backend authorization tests
- Permission tests
- Role tests
- Frontend protected route tests

Verify:

- Administrator access
- Lawyer restrictions
- Court Representative restrictions
- Unauthorized access
- Missing permission handling

---

## Validation Checklist

Before finishing, verify:

- RBAC integrates with Authentication.
- Roles are centralized.
- Permissions are centralized.
- Authorization dependencies work.
- Protected routes work.
- Unauthorized users receive HTTP 403.
- Sidebar hides unauthorized items.
- Unauthorized page renders correctly.
- No duplicated permission logic exists.
- No TypeScript errors.
- No linting errors.
- No failing tests.
- No runtime errors.

---

## Implementation Constraints

Do not modify existing architecture.

Do not rename existing files.

Reuse existing authentication logic.

Do not hardcode roles or permissions.

Keep the authorization system generic so future features can easily define and reuse permissions.

When this feature is complete, stop implementation and wait for the next feature specification.