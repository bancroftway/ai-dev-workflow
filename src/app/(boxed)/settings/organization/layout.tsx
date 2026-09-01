import { notFound } from "next/navigation";
import { auth } from "@/auth";
import { E2E_MODE } from "@/lib/e2e";

/**
 * Server-side page gate for Org settings (Entra App Role "Admin", CI/CD plan Phase 6): the page
 * itself is a client component and cannot check roles, so this layout 404s non-admins before it
 * renders. notFound (not a 403 page): don't advertise the surface to people who can't use it.
 * Courtesy layer only -- the API routes underneath re-check the role on every call.
 */
export default async function OrganizationSettingsLayout({ children }: { children: React.ReactNode }) {
  if (!E2E_MODE) {
    const session = await auth();
    if (!session?.isAdmin) notFound();
  }
  return <>{children}</>;
}
