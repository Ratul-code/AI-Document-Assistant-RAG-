import Link from "next/link";

import { cn } from "@/lib/utils";

/**
 * Shared header + footer for /login and /signup.
 *
 * Visual:
 *  - The page title uses `text-foreground` (near-white in dark) so the
 *    heading reads confidently against the dark canvas.
 *  - The action link uses `text-primary` (lime-green) so it harmonises
 *    with the primary submit button without competing for attention.
 *  - Spacing follows the design system (24px between header and form,
 *    24px between form and footer).
 */
export function AuthHeader({
  title,
  subtitle,
}: {
  title: string;
  subtitle: string;
}) {
  return (
    <header className="flex flex-col gap-2">
      <h1 className="text-[28px] font-semibold tracking-tight leading-tight text-foreground">
        {title}
      </h1>
      <p className="text-sm text-muted-foreground leading-relaxed">
        {subtitle}
      </p>
    </header>
  );
}

export function AuthFooterLink({
  prompt,
  href,
  linkText,
}: {
  prompt: string;
  href: string;
  linkText: string;
}) {
  return (
    <p className="text-center text-sm text-muted-foreground">
      {prompt}{" "}
      <Link
        href={href}
        className={cn(
          "font-medium text-primary underline-offset-4 transition-colors",
          "hover:underline hover:text-primary/90",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 focus-visible:rounded-sm",
        )}
      >
        {linkText}
      </Link>
    </p>
  );
}
