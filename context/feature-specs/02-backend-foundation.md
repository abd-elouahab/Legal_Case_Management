Read `CLAUDE.md` before starting.

# Backend Foundation

We're implementing the backend foundation for the Legal Case Management Platform.

## Objective

Set up the backend infrastructure and application architecture that all future backend features will build upon.

Do not implement any business logic.

---

## Dependencies

Install the following packages if they are not already installed.

### Runtime

- fastapi
- uvicorn
- sqlalchemy
- alembic
- psycopg[binary]
- pydantic-settings
- python-dotenv
- redis
- minio
- qdrant-client
- structlog

### Development

- pytest
- pytest-asyncio
- httpx
- ruff
- mypy

If new packages are installed:

- Update `requirements.txt`.
- Ensure dependency versions remain compatible.

---

## FastAPI Setup

Configure the FastAPI application.

Implement:

- Application entry point
- Application configuration
- API versioning (`/api/v1`)
- Router registration
- Lifespan events
- Middleware registration
- Exception handlers

Do not create feature-specific routes.

---

## Configuration

Configure application settings using `pydantic-settings`.

Load configuration from environment variables.

Support:

- Development
- Production
- Testing

Validate required environment variables during startup.

Fail fast if configuration is invalid.

---

## Logging

Configure structured logging.

Requirements:

- Request logging
- Startup logs
- Shutdown logs
- Error logging
- Configurable log levels

Do not use `print()` statements.

---

## Database

Configure PostgreSQL.

Implement:

- SQLAlchemy engine
- Session management
- Base model
- Database dependency
- Connection testing

Configure Alembic.

Initialize migration support.

Do not create business models.

Do not create business tables.

---

## Redis

Configure Redis.

Implement:

- Client initialization
- Connection pooling
- Health check

Do not implement caching.

Do not implement Pub/Sub.

---

## MinIO

Configure MinIO client.

Verify connection.

Do not upload files.

Do not create buckets.

---

## Qdrant

Configure the Qdrant client.

Verify connectivity.

Do not create collections.

Do not generate embeddings.

---

## Middleware

Configure:

- CORS
- Request logging
- Error handling
- Trusted hosts (if applicable)

Authentication middleware will be implemented later.

---

## Health Endpoints

Implement:

- GET `/health`
- GET `/ready`
- GET `/version`

These endpoints should verify that the application is running correctly.

---

## Project Structure

Use the existing project structure.

Do not modify folder organization.

Do not create unnecessary modules.

---

## API Documentation

Ensure Swagger and ReDoc are enabled.

Verify that all configured routes appear correctly.

---

## Error Handling

Implement global exception handlers.

Return consistent JSON error responses.

Do not expose internal stack traces.

---

## Code Quality

Follow all rules defined in:

- `CLAUDE.md`
- `code-standards.md`

Write production-ready code.

Do not leave TODOs.

Do not leave placeholder implementations.

---

## Out of Scope

Do NOT implement:

- Authentication
- Authorization
- Users
- Cases
- Documents
- Timeline
- Notifications
- OCR
- AI
- Reports
- Search

Only infrastructure should be implemented.

---

## Validation

Before finishing, verify:

- Backend starts successfully.
- Swagger UI loads correctly.
- ReDoc loads correctly.
- `/health` returns HTTP 200.
- `/ready` returns HTTP 200.
- `/version` returns HTTP 200.
- PostgreSQL connection succeeds.
- Redis connection succeeds.
- MinIO connection succeeds.
- Qdrant connection succeeds.
- Alembic is initialized correctly.
- No TypeScript errors (frontend).
- No Python linting errors.
- No failing tests.
- No startup warnings.
- No runtime errors.

When complete, the backend should provide a stable foundation for implementing business features.