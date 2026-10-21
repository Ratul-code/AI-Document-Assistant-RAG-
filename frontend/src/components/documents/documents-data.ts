/**
 * Local types for the documents UI.
 *
 * Document model types live in `lib/api/documents.ts` (the FastAPI
 * contract). This file holds the few UI-only helpers we still need
 * locally — the upload dialog's filetype picker, for example.
 */

export type FileKind = "pdf" | "docx" | "txt" | "md";

export const FILE_KINDS: ReadonlyArray<{
  value: FileKind;
  label: string;
}> = [
  { value: "pdf", label: "PDF" },
  { value: "docx", label: "DOCX" },
  { value: "txt", label: "TXT" },
  { value: "md", label: "MD" },
];
