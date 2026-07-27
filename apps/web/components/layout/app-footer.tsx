/**
 * Application footer.
 *
 * Minimal shell footer beneath the main content. Purely presentational — the
 * year is computed at render time; there is no business logic here.
 */
export function AppFooter() {
  const year = new Date().getFullYear();

  return (
    <footer className="border-t border-border px-4 py-4 sm:px-6">
      <div className="mx-auto flex w-full max-w-7xl flex-col items-center justify-between gap-2 text-xs text-muted-foreground sm:flex-row">
        <p>© {year} Legal Case Management Platform</p>
        <p>AI-powered legal collaboration</p>
      </div>
    </footer>
  );
}
