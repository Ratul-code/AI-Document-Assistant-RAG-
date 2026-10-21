"use client";

import { AlertCircle, Loader2 } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import { type FileKind } from "./documents-data";
import { UploadDropzone } from "./upload-dropzone";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /**
   * Called when the user submits a valid form. The parent owns the
   * upload-intent POST, the activity-log row, and the dialog close.
   */
  onSubmit: (input: { title: string; kind: FileKind; file: File; }) => void;
  /** Disables the form while the parent's upload-intent is in flight. */
  isPending?: boolean;
  /** Error message from the parent's mutation, if any. */
  errorMessage?: string | null;
};

export function UploadDocumentDialog({
  open,
  onOpenChange,
  onSubmit,
  isPending = false,
  errorMessage = null,
}: Props) {
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState<FileKind>("pdf");
  const [file, setFile] = useState<File | null>(null);

  // Reset form state every time the dialog re-opens. We do this via
  // a render-time branch on `open` rather than an effect, since the
  // "previous value of open" is the only state we need to remember.
  // (`open` flips false → true → false; the second `false` keeps
  // the last values but the next `true` resets them.)
  const [lastOpen, setLastOpen] = useState(open);
  if (open && !lastOpen) {
    setTitle("");
    setKind("pdf");
    setFile(null);
    setLastOpen(true);
  } else if (!open && lastOpen) {
    setLastOpen(false);
  }

  const titleError =
    title.trim().length === 0
      ? null
      : title.trim().length > 120
        ? "Title is too long (max 120 chars)."
        : null;

  const canSubmit =
    title.trim().length > 0 && !!file && !titleError && !isPending;

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit || !file) return;
    onSubmit({ title: title.trim(), kind, file });
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Upload document</DialogTitle>
          <DialogDescription>
            Add a new document to your library. We&apos;ll analyze it so the
            AI can answer questions grounded in its content.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="doc-title">Title</Label>
            <Input
              id="doc-title"
              type="text"
              placeholder="Q4 Strategy Notes"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              maxLength={120}
              disabled={isPending}
            />
            {titleError ? (
              <p className="text-xs text-destructive" role="alert">
                {titleError}
              </p>
            ) : null}
          </div>

          {/* <div className="flex flex-col gap-2">
            <Label htmlFor="doc-kind">File type</Label>
            <select
              id="doc-kind"
              value={kind}
              onChange={(e) => setKind(e.target.value as FileKind)}
              disabled={isPending}
              className="h-9 rounded-md border border-input bg-background px-2.5 text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {FILE_KINDS.map((k) => (
                <option key={k.value} value={k.value}>
                  {k.label}
                </option>
              ))}
            </select>
          </div> */}

          <div className="flex flex-col gap-2">
            <Label>File</Label>
            <UploadDropzone
              file={file}
              onFileChange={setFile}
              disabled={isPending}
            />
          </div>

          {errorMessage ? (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive"
            >
              <AlertCircle className="mt-0.5 size-4 shrink-0" />
              <span className="min-w-0">{errorMessage}</span>
            </div>
          ) : null}

          <DialogFooter className="gap-2 sm:gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={!canSubmit}>
              {isPending ? (
                <>
                  <Loader2 className="size-4 animate-spin" />
                  Uploading…
                </>
              ) : (
                "Upload"
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}