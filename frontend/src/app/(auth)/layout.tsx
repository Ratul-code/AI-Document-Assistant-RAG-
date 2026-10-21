export default function AuthLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="min-h-svh w-full grid lg:grid-cols-2">
      {/* Left panel — branding */}
      <aside className="relative hidden lg:flex flex-col justify-between overflow-hidden bg-sidebar text-sidebar-foreground p-10 border-r border-sidebar-border">
        {/* Subtle brand-tinted ambient highlight (top-right glow). */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0"
          style={{
            background:
              "radial-gradient(60% 50% at 100% 0%, oklch(0.8444 0.2084 130 / 0.08), transparent 60%)",
          }}
        />
        {/* Fine top hairline to separate the panel from the page edge. */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-primary/30 to-transparent"
        />

        <header className="relative flex items-center gap-2.5 text-sm font-semibold tracking-tight">
          <div className="size-8 rounded-md bg-primary text-primary-foreground grid place-items-center text-xs font-bold shadow-[0_0_0_1px_oklch(0.8444_0.2084_130_/_0.25),0_4px_16px_-8px_oklch(0.8444_0.2084_130_/_0.5)]">
            AI
          </div>
          <span>Doc Assistant</span>
        </header>

        <div className="relative space-y-4 max-w-md">
          <blockquote className="text-2xl font-medium leading-snug text-balance text-foreground">
            &ldquo;Upload any document. Ask anything. Get answers grounded in
            your own files.&rdquo;
          </blockquote>
          <p className="text-sm text-muted-foreground">
            Your private AI that reads, understands, and remembers your
            documents.
          </p>
        </div>

        <footer className="relative flex items-center justify-between text-xs text-muted-foreground">
          <span>© {new Date().getFullYear()} AI Doc Assistant</span>
          <span className="inline-flex items-center gap-1.5">
            <span
              aria-hidden
              className="size-1.5 rounded-full bg-primary shadow-[0_0_8px_oklch(0.8444_0.2084_130_/_0.6)]"
            />
            All systems normal
          </span>
        </footer>
      </aside>

      {/* Right panel — auth form slot */}
      <main className="flex items-center justify-center p-6 sm:p-10 bg-background">
        <div className="w-full max-w-sm">{children}</div>
      </main>
    </div>
  );
}
