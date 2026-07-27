Read `CLAUDE.md` before starting.

# Authentication

We're implementing the complete authentication system for the Legal Case Management Platform.

## Objective

Implement a secure JWT-based authentication system that allows users to sign in, maintain authenticated sessions, and securely access protected resources.

This feature establishes user identity only.

Do not implement role-based permissions or business features.

---

## Dependencies

Install the following packages if they are not already installed.

### Backend

- python-jose[cryptography]
- passlib[bcrypt]
- python-multipart

### Frontend

Install any required authentication libraries only if necessary.

Prefer the built-in Next.js capabilities unless a dependency is required.

If new packages are installed:

- Update `requirements.txt`
- Update `package.json`

---

## Backend

Implement a complete authentication module.

Create:

- Authentication router
- Authentication service
- Authentication schemas
- Authentication dependencies
- Authentication utilities

Implement:

- Login
- Logout
- Refresh Access Token
- Get Current User
- Change Password

Do not implement registration.

Users will be managed by administrators in a future feature.

---

## Authentication Method

Use:

- JWT Access Token
- JWT Refresh Token

Requirements:

- Access Token expires after 15 minutes.
- Refresh Token expires after 7 days.
- Tokens must be signed securely.
- Secrets must come from environment variables.

Never hardcode secrets.

---

## Password Security

Passwords must:

- Be hashed using bcrypt.
- Never be stored in plain text.
- Be verified securely.

---

## Authentication Flow

Implement the following flow:

1. User submits email and password.
2. Credentials are validated.
3. Password is verified.
4. JWT Access Token is generated.
5. Refresh Token is generated.
6. Tokens are returned to the frontend.
7. Frontend stores the session securely.
8. Protected requests include the Access Token.
9. Expired Access Tokens are refreshed using the Refresh Token.

---

## Frontend

Create:

- Login Page

Implement:

- Login Form
- Form Validation
- Loading State
- Error State
- Success Redirect

Use only Design System components.

Do not create custom UI components.

---

## Route Protection

Protect all dashboard routes.

Unauthenticated users must be redirected to:

/login

Authenticated users should never see the login page again.

---

## Session Management

Implement:

- Session initialization
- Session persistence
- Automatic logout
- Automatic token refresh

The application should restore the session after page refresh.

---

## API Endpoints

Implement:

POST /api/v1/auth/login

POST /api/v1/auth/logout

POST /api/v1/auth/refresh

GET /api/v1/auth/me

PATCH /api/v1/auth/change-password

Follow REST conventions.

---

## Validation

Validate:

- Email format
- Required fields
- Password length

Return meaningful validation errors.

---

## Error Handling

Handle:

- Invalid credentials
- Expired tokens
- Invalid tokens
- Missing tokens
- Disabled accounts
- Server errors

Return consistent JSON responses.

Never expose internal errors.

---

## Logging

Log:

- Successful login
- Failed login
- Logout
- Password change

Never log:

- Passwords
- JWTs
- Secrets

---

## Security

Implement:

- Password hashing
- JWT verification
- Refresh Token validation
- Protected endpoints
- CSRF-safe authentication strategy where applicable

Never expose sensitive information.

---

## Environment Variables

Add all required variables to:

.env.example

Include:

- JWT Secret
- JWT Algorithm
- Access Token Expiration
- Refresh Token Expiration

---

## Out of Scope

Do NOT implement:

- User Registration
- Email Verification
- Password Reset via Email
- Two-Factor Authentication
- OAuth
- Social Login
- Role-Based Authorization
- Permissions
- User Management

These will be implemented in future features.

---

## Testing

Implement:

- Backend unit tests
- Authentication integration tests
- Frontend authentication flow tests

Test:

- Successful login
- Failed login
- Invalid credentials
- Expired token
- Refresh token
- Logout
- Protected routes

---

## Validation Checklist

Before finishing, verify:

- Login works.
- Logout works.
- Access Tokens are generated.
- Refresh Tokens are generated.
- Passwords are hashed.
- Sessions persist after refresh.
- Protected routes redirect correctly.
- Authentication middleware works.
- API documentation is updated.
- No TypeScript errors.
- No linting errors.
- No failing tests.
- No console errors.
- No security warnings.

When complete, the platform should support secure authentication and be ready for implementing authorization and user management.