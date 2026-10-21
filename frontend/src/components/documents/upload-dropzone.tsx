"use client";

import { File as FileIcon, Upload, X } from "lucide-react";
import {
  type ChangeEvent,
  type DragEvent,
  useRef,
  useState,
} from "react";

import { cn } from "@/lib/utils";

type Props = {
  file: File | null;
  onFileChange: (file: File | null) => void;
  /** Maximum total bytes allowed for an upload. */
  maxBytes?: number;
  /** Human-readable list of accepted file extensions for the copy. */
  acceptedKindsLabel?: string;
  /** While true, the dropzone is non-interactive (used during submit). */
  disabled?: boolean;
};

/** Pretty-print a byte count. */
function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function UploadDropzone({
  file,
  onFileChange,
  maxBytes = 25 * 1024 * 1024,
  acceptedKindsLabel = "PDF, DOCX, TXT, MD",
  disabled = false,
}: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function pickFile() {
    inputRef.current?.click();
  }

  function validateAndAccept(candidate: File | null) {
    setError(null);
    if (!candidate) {
      onFileChange(null);
      return;
    }
    if (candidate.size > maxBytes) {
      setError(`File is too large. Max ${formatBytes(maxBytes)}.`);
      return;
    }
    onFileChange(candidate);
  }

  function onChange(e: ChangeEvent<HTMLInputElement>) {
    validateAndAccept(e.target.files?.[0] ?? null);
    // Allow picking the same file twice in a row.
    e.target.value = "";
  }

  function onDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragOver(false);
    const dropped = e.dataTransfer.files?.[0] ?? null;
    validateAndAccept(dropped);
  }

  function onDragOver(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragOver(true);
  }

  function onDragLeave(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragOver(false);
  }

  function clearFile(e: React.MouseEvent) {
    e.stopPropagation();
    validateAndAccept(null);
  }

  return (
    <div className="flex flex-col gap-2">
      <div
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-disabled={disabled}
        onClick={disabled ? undefined : pickFile}
        onKeyDown={(e) => {
          if (disabled) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            pickFile();
          }
        }}
        onDrop={disabled ? undefined : onDrop}
        onDragOver={disabled ? undefined : onDragOver}
        onDragLeave={disabled ? undefined : onDragLeave}
        className={cn(
          "relative flex min-h-[180px] flex-col items-center justify-center gap-3 rounded-lg border-2 border-dashed px-6 py-8 text-center transition-all",
          disabled
            ? "cursor-not-allowed border-border bg-muted/30 opacity-60"
            : "cursor-pointer",
          !disabled &&
          (dragOver
            ? "border-primary bg-primary/5 scale-[1.01]"
            : "border-border bg-muted/30 hover:border-primary/50 hover:bg-muted/50"),
          error && "border-destructive/50",
        )}
      >
        {/* Hidden native input */}
        <input
          ref={inputRef}
          type="file"
          className="sr-only"
          onChange={onChange}
          accept=".pdf,.docx,.txt,.md"
        />

        {file ? (
          <div className="flex w-full items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-3">
              <div className="grid size-10 shrink-0 place-items-center rounded-md bg-primary/10 text-primary">
                <FileIcon className="size-5" />
              </div>
              <div className="min-w-0 text-left">
                <p className="truncate text-sm font-medium text-foreground">
                  {file.name}
                </p>
                <p className="text-xs text-muted-foreground">
                  {formatBytes(file.size)}
                </p>
              </div>
            </div>
            <button
              type="button"
              onClick={clearFile}
              disabled={disabled}
              aria-label="Remove file"
              className="grid size-8 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
            >
              <X className="size-4" />
            </button>
          </div>
        ) : (
          <>
            <div className="grid size-12 place-items-center rounded-full bg-primary/10 text-primary">
              <Upload className="size-5" />
            </div>
            <div className="space-y-1">
              <p className="text-sm font-medium text-foreground">
                Drag &amp; drop or click to choose a file
              </p>
              <p className="text-xs text-muted-foreground">
                Max {formatBytes(maxBytes)} · {acceptedKindsLabel}
              </p>
            </div>
          </>
        )}
      </div>
      {error ? (
        <p className="text-xs text-destructive" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}