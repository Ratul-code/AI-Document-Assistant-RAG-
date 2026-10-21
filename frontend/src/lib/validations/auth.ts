/**
 * Zod schemas for auth forms. These are the source of truth —
 * types are inferred below so changing a rule updates the type too.
 */

import { z } from "zod";

/* ---------- Login ---------- */

export const loginSchema = z.object({
  email: z.string().min(1, "Email is required").email("Invalid email address"),
  password: z.string().min(1, "Password is required"),
});

export type LoginInput = z.infer<typeof loginSchema>;

/* ---------- Signup ---------- */

export const signupSchema = z
  .object({
    name: z.string().min(2, "Name must be at least 2 characters"),
    email: z.string().min(1, "Email is required").email("Invalid email address"),
    password: z
      .string()
      .min(8, "Password must be at least 8 characters")
      .regex(/[A-Z]/, "Must contain at least one uppercase letter")
      .regex(/[0-9]/, "Must contain at least one number"),
    confirmPassword: z.string().min(1, "Please confirm your password"),
  })
  .refine((data) => data.password === data.confirmPassword, {
    message: "Passwords do not match",
    path: ["confirmPassword"],
  });

export type SignupInput = z.infer<typeof signupSchema>;

/* ---------- Action result (used by handlers) ---------- */

export type AuthActionResult =
  | { success: true; message?: string }
  | {
      success: false;
      message: string;
      fieldErrors?: Record<string, string[] | undefined>;
    };