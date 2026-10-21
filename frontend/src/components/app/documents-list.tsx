"use client";

import { useQuery } from "@tanstack/react-query";
import {
  FileSpreadsheet,
  FileText,
  FileType,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";

import { DeleteDocumentDialog } from "@/components/documents/delete-document-dialog";
import { DocumentDetailDialog } from "@/components/documents/document-detail-dialog";
import { EditDocumentDialog } from "@/components/documents/edit-document-dialog";
import {
  documentKeys,
  getReadyDocuments,
  type Document,
} from "@/lib/api/documents";
import { cn } from "@/lib/utils";
import { SidebarSection } from "./sidebar-section";
import { useExpandableList } from "./use-expandable-list";

const INITIAL = 3;
const STEP = 5;
const READY_LIMIT = 50;

type DocumentKind = "pdf" | "docx" | "txt" | "md" | "xlsx" | "csv";

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
      return "pdf";
  }
}

type Props = { collapsed: boolean; };

const ICON_BY_KIND: Record<DocumentKind, LucideIcon> = {
  pdf: FileText,
  docx: FileType,
  txt: FileType,
  md: FileType,
  xlsx: FileSpreadsheet,
  csv: FileSpreadsheet,
};

export function DocumentsList({ collapsed }: Props) {
  const readyQuery = useQuery({
    queryKey: documentKeys.list({ status: "ready", limit: READY_LIMIT, offset: 0 }),
    queryFn: () => getReadyDocuments(READY_LIMIT),
  });

  const items = readyQuery.data?.items ?? [];
  const { shown, canExpand, expand, total, visible } = useExpandableList<Document>(
    items,
    { initial: INITIAL, step: STEP },
  );

  if (collapsed) {
    return (
      <div className="flex flex-col items-center gap-1 px-2 pt-4">
        <FileText className="size-4 text-muted-foreground" />
      </div>
    );
  }

  const isLoading = readyQuery.isPending;
  const hasError = readyQuery.isError;

  return (
    <SidebarSection
      label="Documents"
      href="/documents"
      totalCount={isLoading ? undefined : total}
      visibleCount={visible}
      collapsed={collapsed}
      canExpand={canExpand}
      onExpand={expand}
    >
      {isLoading ? (
        <p className="px-2 py-2 text-xs text-muted-foreground">
          Loading documents…
        </p>
      ) : hasError ? (
        <p className="px-2 py-2 text-xs text-destructive">
          Couldn&apos;t load documents.
        </p>
      ) : shown.length === 0 ? (
        <p className="px-2 py-2 text-xs text-muted-foreground">
          No documents uploaded
        </p>
      ) : (
        shown.map((doc) => <DocumentRow key={doc.id} doc={doc} />)
      )}
    </SidebarSection>
  );
}

function DocumentRow({ doc }: { doc: Document; }) {
  const Icon = ICON_BY_KIND[kindFromFilename(doc.filename)];
  // Dialog state. The sidebar row only exposes the detail dialog
  // today; Edit / Delete are reachable from inside the detail
  // dialog's own header / footer, so the parent still owns a
  // single set of open flags per row — same shape as the page's
  // `DocumentRow`, but inlined because the sidebar row is a much
  // simpler trigger.
  const [detailOpen, setDetailOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  return (
    <>
      <button
        type="button"
        onClick={() => setDetailOpen(true)}
        aria-label={`Open details for ${doc.title}`}
        className={cn(
          "group/doc flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left",
          "text-sm text-sidebar-foreground/80 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
          "focus-visible:outline-none focus-visible:bg-sidebar-accent/60 focus-visible:text-sidebar-foreground",
          "transition-colors",
        )}
      >
        <Icon className="size-3.5 shrink-0 text-muted-foreground group-hover/doc:text-foreground transition-colors" />
        <span className="truncate">{doc.title}</span>
      </button>
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
    </>
  );
}