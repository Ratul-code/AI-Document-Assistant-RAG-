"use client";

import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";

import { signupSchema, type SignupInput } from "@/lib/validations/auth";
import { signUp } from "@/lib/api/auth";
import { ApiError } from "@/lib/api/client";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { AuthFooterLink, AuthHeader } from "@/components/auth/AuthShell";

export function SignupForm() {
  const router = useRouter();

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<SignupInput>({
    resolver: zodResolver(signupSchema),
    defaultValues: {
      name: "",
      email: "",
      password: "",
      confirmPassword: "",
    },
  });

  const mutation = useMutation({
    mutationFn: (values: SignupInput) => signUp(values),
    onSuccess: () => {
      // Account created — send them to the login page to sign in.
      router.push("/login");
      router.refresh();
    },
  });

  const serverError =
    mutation.error instanceof ApiError
      ? mutation.error.message
      : mutation.isError
        ? "Something went wrong. Please try again."
        : null;

  function onSubmit(values: SignupInput) {
    mutation.mutate(values);
  }

  return (
    <div className="flex flex-col gap-6">
      <AuthHeader
        title="Create your account"
        subtitle="Start chatting with your documents in minutes."
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
          <Label htmlFor="name">Name</Label>
          <Input
            id="name"
            type="text"
            autoComplete="name"
            placeholder="Jane Doe"
            aria-invalid={!!errors.name}
            {...register("name")}
          />
          {errors.name ? (
            <p className="text-xs text-destructive">{errors.name.message}</p>
          ) : null}
        </div>

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
          <Label htmlFor="password">Password</Label>
          <Input
            id="password"
            type="password"
            autoComplete="new-password"
            placeholder="••••••••"
            aria-invalid={!!errors.password}
            {...register("password")}
          />
          {errors.password ? (
            <p className="text-xs text-destructive">
              {errors.password.message}
            </p>
          ) : (
            <p className="text-xs text-muted-foreground">
              At least 8 characters, one uppercase letter and one number.
            </p>
          )}
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="confirmPassword">Confirm password</Label>
          <Input
            id="confirmPassword"
            type="password"
            autoComplete="new-password"
            placeholder="••••••••"
            aria-invalid={!!errors.confirmPassword}
            {...register("confirmPassword")}
          />
          {errors.confirmPassword ? (
            <p className="text-xs text-destructive">
              {errors.confirmPassword.message}
            </p>
          ) : null}
        </div>

        <Button
          type="submit"
          disabled={mutation.isPending}
          className="mt-2 w-full"
        >
          {mutation.isPending ? "Creating account..." : "Create account"}
        </Button>
      </form>

      <AuthFooterLink
        prompt="Already have an account?"
        href="/login"
        linkText="Sign in"
      />
    </div>
  );
}