Read `CLAUDE.md` before starting.

# App Foundation

We're implementing the application's foundation and shared layout.

## Objective

Build the core application shell that every future feature will use.

This feature must not implement any business logic.

## Tasks

### Frontend

Create the application's root structure using the App Router.

Implement:

- Root Layout
- Dashboard Layout
- Authentication Layout

Create shared layouts for:

- Public pages
- Protected pages

Implement a responsive application shell including:

- Sidebar
- Top Navigation
- Page Header
- Breadcrumbs
- Main Content Area
- Footer (if needed)

The layout must be reusable across all future pages.

---

### Providers

Configure all global providers.

Include:

- Theme Provider
- React Query Provider
- Tooltip Provider
- Toast Provider

Wrap the application correctly.

---

### Routing

Create the initial route structure.

Include placeholder pages for:

- Login
- Dashboard
- Cases
- Documents
- Lawyers
- Court Updates
- Reports
- Notifications
- AI Assistant
- Settings

Every page should contain only a minimal placeholder.

Do not implement any feature logic.

---

### Navigation

Create a reusable navigation configuration.

Implement:

- Sidebar navigation
- Top navigation
- Active route highlighting
- Breadcrumb generation

Navigation items should match the project structure.

---

### Shared Components

Create reusable layout components.

Include:

- AppSidebar
- AppHeader
- PageContainer
- PageHeader
- Breadcrumbs
- UserMenu (placeholder)
- NotificationButton (placeholder)
- SearchBar (placeholder)

Use only the Design System components.

---

### Global States

Prepare the global state structure.

Create stores/hooks for:

- Sidebar state
- Theme state
- User session placeholder

Do not implement authentication.

---

### Utilities

Create reusable utilities if needed.

Examples:

- Route constants
- Navigation configuration
- Breadcrumb helper
- Page metadata helper

---

### Loading & Error States

Create reusable pages/components for:

- Loading
- Skeleton Loading
- Empty State
- Error State
- 404 Not Found
- Access Denied (placeholder)

---

### Responsiveness

Ensure:

- Mobile sidebar
- Desktop sidebar
- Responsive layouts
- Proper spacing
- Consistent breakpoints

---

### Accessibility

Ensure:

- Keyboard navigation
- Focus states
- Proper landmarks
- Accessible navigation

---

### Constraints

Do NOT implement:

- Authentication
- API calls
- Database access
- User management
- Case management
- Notifications
- AI features
- Business logic

Use mocked placeholder data only.

---

### Validation

Before finishing, verify:

- Application builds successfully.
- No TypeScript errors.
- No ESLint errors.
- No hydration errors.
- No console errors.
- All routes load correctly.
- Sidebar navigation works.
- Breadcrumbs update correctly.
- Layout is fully responsive.
- Theme is consistent with the Design System.
- All placeholder pages render successfully.

When this feature is complete, the project should have a polished, reusable application shell ready for implementing business features.