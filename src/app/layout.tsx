import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { Providers } from "./providers";
import { auth } from "@/auth";
import { WorkspaceHeader } from "@/components/WorkspaceHeader";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "AI-Assisted Specification & Planning",
  description: "Draft, review, and approve a Specification and Implementation Plan.",
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const session = await auth();
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="h-full flex flex-col">
        {/* THE single page shape, for every route with no exceptions: a frozen header (always
            this exact component, never re-rendered per-page) and a full-bleed, independently
            scrolling body below it. One place to change either -- a second copy is exactly how
            pages drifted out of sync with each other before. */}
        <Providers session={session}>
          <div className="flex h-full w-full flex-col overflow-hidden">
            <div className="shrink-0">
              <WorkspaceHeader />
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
          </div>
        </Providers>
      </body>
    </html>
  );
}
