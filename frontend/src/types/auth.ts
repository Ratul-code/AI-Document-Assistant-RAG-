/**
 * Auth-related types. The Zod schemas in lib/validations/auth.ts
 * are the single source of truth — these types are inferred from them
 * so they stay in sync automatically.
 */

import type {
  LoginInput,
  SignupInput,
  AuthActionResult,
} from "@/lib/validations/auth";

export type { LoginInput, SignupInput, AuthActionResult };

/** Shape of the currently authenticated user (used by client state later). */
export interface AuthUser {
  id: string;
  email: string;
  name: string;
}

/** Generic field-error map returned by the action result. */
export type FieldErrors<T> = Partial<Record<keyof T, string[]>>;