import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { cn } from "@/lib/utils";
import { QueryProvider } from "@/providers/query-provider";
import { RealtimeBridge } from "@/providers/realtime-bridge";
import { WSProvider } from "@/providers/ws-provider";
import { Toaster } from "@/components/ui/sonner";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Doc Assistant",
  description: "Chat with your documents, grounded in your own files.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={cn(
        "h-full",
        "antialiased",
        "dark",
        geistSans.variable,
        geistMono.variable,
        "font-sans",
      )}
    >
      <body className="min-h-full flex flex-col">
        <QueryProvider>
          {/* WSProvider opens/closes the global /ws connection. */}
          <WSProvider>
            {/* RealtimeBridge subscribes once to all incoming
                messages and routes them to toasts / query cache. */}
            <RealtimeBridge />
            {children}
            {/* Toaster renders where toast(...) calls appear. We
                pass `closeButton` so users can dismiss any toast; the
                surface itself is fully styled by the design system
                tokens in `components/ui/sonner.tsx`. */}
            <Toaster closeButton />
          </WSProvider>
        </QueryProvider>
      </body>
    </html>
  );
}
