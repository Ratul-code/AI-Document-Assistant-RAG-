"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";

import { loginSchema, type LoginInput } from "@/lib/validations/auth";
import { signIn } from "@/lib/api/auth";
import { ApiError } from "@/lib/api/client";
import { useWSStore } from "@/lib/realtime/ws-store";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { AuthFooterLink, AuthHeader } from "@/components/auth/AuthShell";

export function LoginForm() {
  const router = useRouter();

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<LoginInput>({
    resolver: zodResolver(loginSchema),
    defaultValues: { email: "", password: "" },
  });

  // The mutation owns: in-flight state (isPending), success/error transitions,
  // and the post-success side-effect (navigate to the next page).
  const mutation = useMutation({
    mutationFn: (values: LoginInput) => signIn(values),
    onSuccess: () => {
      // Cookies are set by FastAPI via Set-Cookie. Just navigate.
      // Honor the `next` query param so middleware's redirect-back
      // continues to work after login.
      const next = new URLSearchParams(window.location.search).get("next");
      // Re-open the WebSocket now that we have fresh auth cookies.
      // If the user got here via the auth-expired redirect, the WS
      // store will be in `auth-expired`; this also clears that
      // latch inside connect() and resumes realtime updates.
      useWSStore.getState().connect();
      router.push(next ?? "/");
      router.refresh();
    },
  });

  // Derive the user-facing error message from the mutation state.
  const serverError =
    mutation.error instanceof ApiError
      ? mutation.error.message
      : mutation.isError
        ? "Something went wrong. Please try again."
        : null;

  function onSubmit(values: LoginInput) {
    mutation.mutate(values);
  }

  return (
    <div className="flex flex-col gap-6">
      <AuthHeader
        title="Welcome back"
        subtitle="Sign in to your account to continue."
      />

      <form
        onSubmit={handleSubmit(onSubmit)}
        noValidate
        className="flex flex-col gap-4"
      >
        {serverError ? (
          <div
            role="alert"
            className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          >
            {serverError}
          </div>
        ) : null}

        <div className="flex flex-col gap-2">
          <Label htmlFor="email">Email</Label>
          <Input
            id="email"
            type="email"
            autoComplete="email"
            placeholder="you@example.com"
            aria-invalid={!!errors.email}
            {...register("email")}
          />
          {errors.email ? (
            <p className="text-xs text-destructive">{errors.email.message}</p>
          ) : null}
        </div>

        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <Label htmlFor="password">Password</Label>
            <Link
              href="/forgot-password"
              className="text-xs text-muted-foreground hover:text-primary transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 focus-visible:rounded-sm"
            >
              Forgot password?
            </Link>
          </div>
          <Input
            id="password"
            type="password"
            autoComplete="current-password"
            placeholder="••••••••"
            aria-invalid={!!errors.password}
            {...register("password")}
          />
          {errors.password ? (
            <p className="text-xs text-destructive">
              {errors.password.message}
            </p>
          ) : null}
        </div>

        <Button
          type="submit"
          disabled={mutation.isPending}
          className="mt-2 w-full"
        >
          {mutation.isPending ? "Signing in..." : "Sign in"}
        </Button>
      </form>

      <AuthFooterLink
        prompt="Don't have an account?"
        href="/signup"
        linkText="Sign up"
      />
    </div>
  );
}