"use client";

import {
  FileSpreadsheet,
  FileText,
  FileType,
  Loader2,
  type LucideIcon
} from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import {
  downloadDocument,
  openDocument,
  type Document,
} from "@/lib/api/documents";
import { cn, formatRelativeTime, humanError } from "@/lib/utils";

import { DeleteDocumentDialog } from "./delete-document-dialog";
import { DocumentDetailDialog } from "./document-detail-dialog";
import { EditDocumentDialog } from "./edit-document-dialog";

type DocumentKind = "pdf" | "docx" | "txt" | "md" | "xlsx" | "csv";

/**
 * Map a filename to an icon-friendly "kind". Falls back to a generic
 * file icon for anything we don't recognise.
 */
function kindFromFilename(filename: string): DocumentKind {
  const ext = filename.split(".").pop()?.toLowerCase() ?? "";
  switch (ext) {
    case "pdf":
      return "pdf";
    case "docx":
    case "doc":
      return "docx";
    case "txt":
      return "txt";
    case "md":
    case "markdown":
      return "md";
    case "xlsx":
    case "xls":
      return "xlsx";
    case "csv":
      return "csv";
    default:
      // Default pdf — most common upload. The row still renders fine.
      return "pdf";
  }
}

const ICON_BY_KIND: Record<DocumentKind, LucideIcon> = {
  pdf: FileText,
  docx: FileType,
  txt: FileType,
  md: FileType,
  xlsx: FileSpreadsheet,
  csv: FileSpreadsheet,
};

type Props = {
  doc: Document;
};

export function DocumentRow({ doc }: Props) {
  const Icon = ICON_BY_KIND[kindFromFilename(doc.filename)];
  const uploadedAt = formatRelativeTime(doc.uploaded_at ?? doc.created_at);
  // Dialog state. The 3-dot menu AND the row's own click both flow
  // through these setters; only one dialog is open at a time.
  const [detailOpen, setDetailOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  // Tracks which file-action (if any) is in flight. While one is
  // running, both menu items stay clickable so the user can switch
  // between View and Download without waiting, but the active one
  // shows a spinner so the click feels acknowledged. We only guard
  // against double-clicking the same action — that would trigger two
  // downloads / popups for one user intent.
  const [pendingAction, setPendingAction] = useState<"view" | "download" | null>(
    null,
  );

  // Action handlers for the row's 3-dot menu. The dialog's View /
  // Download buttons are wired separately inside
  // `DocumentDetailDialog`'s footer, but they call into the same
  // helpers so the UX stays identical between the row and the
  // dialog. Errors land in a sonner toast with a short human
  // message — the same shape the upload / edit / delete dialogs
  // use, so the user gets one consistent error treatment.
  function handleView() {
    if (pendingAction) return;
    setPendingAction("view");
    try {
      // Synchronous: opens a new tab (or navigates the current
      // tab if pop-ups are blocked). No error path — see the
      // matching note in `DocumentDetailDialog.handleView`.
      openDocument(doc.id);
    } finally {
      requestAnimationFrame(() => setPendingAction(null));
    }
  }

  async function handleDownload() {
    if (pendingAction) return;
    setPendingAction("download");
    try {
      await downloadDocument(doc.id);
    } catch (err) {
      toast.error("Couldn't download document", {
        description: humanError(err, "Something went wrong while saving the file."),
      });
    } finally {
      setPendingAction(null);
    }
  }


  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={`Open details for ${doc.title}`}
      onClick={() => setDetailOpen(true)}
      // onKeyDown={handleRowKeyDown}
      className={cn(
        "group flex cursor-pointer items-center gap-3 rounded-md border border-border bg-card px-3 py-2.5",
        "transition-colors hover:bg-muted/60",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
      )}
    >
      <div className="grid size-9 shrink-0 place-items-center rounded-md bg-primary/10 text-primary">
        <Icon className="size-4" />
      </div>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-foreground">
          {doc.title}
        </p>
        <p className="truncate text-xs text-muted-foreground">
          {uploadedAt ?? doc.filename}
        </p>
      </div>
      <StatusBadge status={doc.status} />
      {/* <DropdownMenu>
        <DropdownMenuTrigger
          data-row-action
          aria-label={`Actions for ${doc.title}`}
          // Stop propagation so clicking the 3-dot doesn't ALSO open
          // the detail dialog. The trigger button itself sits inside
          // a `role="button"` row, so without this both handlers fire.
          onClick={(event) => event.stopPropagation()}
          className={cn(
            "grid size-7 place-items-center rounded-md text-muted-foreground",
            "transition-colors hover:bg-muted hover:text-foreground cursor-pointer",
          )}
        >
          <MoreVertical className="size-4" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-44">
          <DropdownMenuItem
            data-row-action
            onClick={(event) => event.stopPropagation()}
            disabled={pendingAction !== null}
            onSelect={(event) => {
              // Keep the menu open until the action resolves — the
              // async fetch can take a beat, and we don't want the
              // trigger to lose focus mid-call.
              event.preventDefault();
              void handleView();
            }}
            aria-label={`View “${doc.title}”`}
          >
            {pendingAction === "view" ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <Eye className="size-4" />
            )}
            View
          </DropdownMenuItem>
          <DropdownMenuItem
            data-row-action
            onClick={(event) => event.stopPropagation()}
            disabled={pendingAction !== null}
            onSelect={(event) => {
              event.preventDefault();
              void handleDownload();
            }}
            aria-label={`Download “${doc.title}”`}
          >
            {pendingAction === "download" ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <Download className="size-4" />
            )}
            Download
          </DropdownMenuItem>
          <DropdownMenuItem
            data-row-action
            onClick={(event) => event.stopPropagation()}
            onSelect={(event) => {
              // Prevent Radix from dismissing the trigger before our
              // dialog mounts — the dialog renders on top of the page
              // anyway, but we want the trigger to stay focused.
              event.preventDefault();
              setEditOpen(true);
            }}
          >
            <Pencil className="size-4" />
            Edit
          </DropdownMenuItem>
          <DropdownMenuItem
            data-row-action
            onClick={(event) => event.stopPropagation()}
            onSelect={(event) => {
              event.preventDefault();
              setDeleteOpen(true);
            }}
          >
            <Trash2 className="size-4" />
            Delete
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu> */}
      <DocumentDetailDialog
        doc={doc}
        open={detailOpen}
        onOpenChange={setDetailOpen}
        onEdit={() => {
          setDetailOpen(false);
          setEditOpen(true);
        }}
        onDelete={() => {
          setDetailOpen(false);
          setDeleteOpen(true);
        }}
      />
      <EditDocumentDialog
        doc={doc}
        open={editOpen}
        onOpenChange={setEditOpen}
      />
      <DeleteDocumentDialog
        doc={doc}
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
      />
    </div>
  );
}

/**
 * The right-hand status indicator on a document row.
 *
 * Three states render differently:
 *  - **In-progress** (`uploaded`, `processing`, `learning`) — a
 *    `Loader2` spinner + the verb, so the user can see at a glance
 *    that work is happening while the doc is not yet chat-ready.
 *  - **Ready** — the existing subtle brand-tinted pill.
 *  - **Failed** — destructive-tinted pill, no spinner.
 *
 * Kept local to this file because the row pill and the dialog pill
 * are not visually identical (the row is more compact, with the
 * spinner inline) and lifting this up to share would force a
 * variant prop on a one-off component.
 */
function StatusBadge({ status }: { status: Document["status"]; }) {
  // In-progress statuses get the spinner. The label is the verb the
  // backend uses for the lifecycle stage; `humanizeLogType` is reused
  // for consistency with the activity strip.
  if (
    status === "uploaded" ||
    status === "processing" ||
    status === "learning"
  ) {
    const label =
      status === "uploaded"
        ? "Uploaded"
        : status === "processing"
          ? "Processing"
          : "Learning";
    return (
      <span
        // `role="status"` + `aria-live="polite"` so screen readers
        // announce when the status changes (e.g. upload → learning)
        // without interrupting whatever the user is currently reading.
        role="status"
        aria-live="polite"
        className="inline-flex shrink-0 items-center gap-1.5 rounded-full bg-secondary px-2 py-0.5 text-[11px] font-medium text-secondary-foreground"
      >
        <Loader2
          className="size-3 animate-spin text-muted-foreground"
          aria-hidden
        />
        {label}
      </span>
    );
  }
  return (
    <span
      className={cn(
        "shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium",
        status === "ready" && "bg-accent text-accent-foreground",
        status === "failed" && "bg-destructive/10 text-destructive",
      )}
    >
      {status === "ready" ? "Ready" : "Failed"}
    </span>
  );
}
