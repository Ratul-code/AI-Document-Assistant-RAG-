import { DocumentsPage } from "@/components/documents/documents-page";

/**
 * Documents workspace — list of uploaded documents on the left,
 * live upload activity on the right. Upload modal is owned by
 * the page component (which also owns the mock event pipeline).
 */
export default function DocumentsPageRoute() {
  return <DocumentsPage />;
}