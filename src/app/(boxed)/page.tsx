import { redirect } from "next/navigation";
import { auth, signIn } from "@/auth";

const APPROVAL_STEPS = [
  {
    step: "01",
    title: "Describe what you need",
    body: "Write your requirements in plain language. The Specification Agent turns them into user stories and acceptance criteria you can actually review.",
  },
  {
    step: "02",
    title: "Approve the Specification",
    body: "Nothing moves forward until you sign off. Ask for revisions, or approve it and let the Planning Agent draft the Implementation Plan — wireframes included, so you see the UI before a line of code is written.",
  },
  {
    step: "03",
    title: "Approve the Plan, then build",
    body: "Review the ordered build steps and wireframes, approve them, and the workflow generates the code against a plan you've already signed off on. No surprises at the end.",
  },
];

const FEATURES = [
  {
    title: "95% automated test coverage",
    body: "Generated code ships with a test suite that covers it — not a demo with untested edges.",
  },
  {
    title: "Hundreds of enforced code quality rules",
    body: "Every change is held to a rule set that goes well beyond a typical linter, exceeding common industry bars for production code.",
  },
  {
    title: "Automated security scanning",
    body: "Code is scanned for security issues as part of the workflow, not bolted on after the fact.",
  },
  {
    title: "Architecture & entity-relationship diagrams",
    body: "The system's structure and data model are documented and kept alongside the code, not left to go stale.",
  },
  {
    title: "Living Specification & Plan documents",
    body: "The documents you approved stay attached to the code they produced — a durable record of what was built and why.",
  },
  {
    title: "Wireframes before code",
    body: "During planning, wireframes are generated for your approval, so the UI is agreed on before implementation starts.",
  },
];

export default async function Home() {
  // Signed-in users never see the marketing/sign-in page -- proxy.ts only bounces the
  // unauthenticated direction, so this is the one place that sends them to the workspace.
  if ((await auth())?.user) redirect("/select");
  return (
    <main className="flex-1">
      {/* Hero */}
      <section className="mx-auto max-w-4xl px-6 py-24 text-center sm:py-32">
        <p className="text-sm font-medium tracking-wide text-amber-900">
          Structured AI-assisted development — not vibe coding
        </p>
        <h1 className="mt-4 text-4xl font-semibold tracking-tight text-neutral-900 sm:text-5xl">
          Ship AI-generated code you don&apos;t have to double-check.
        </h1>
        <p className="mx-auto mt-6 max-w-2xl text-lg text-neutral-600">
          AI Dev Workflow turns a rough idea into a reviewed Specification, an
          approved Implementation Plan, and production-ready code — with a
          human sign-off at every gate. The result can go to production
          without the hesitation that comes with vibe-coded apps.
        </p>
        <form
          action={async () => {
            "use server";
            await signIn("microsoft-entra-id", { redirectTo: "/select" });
          }}
          className="mt-10"
        >
          <button
            type="submit"
            className="inline-flex items-center gap-2 rounded-lg bg-neutral-900 px-6 py-3 text-sm font-medium text-white transition-colors hover:bg-neutral-800"
          >
            Sign in with Microsoft
          </button>
        </form>
      </section>

      {/* Human-in-the-loop approval gates */}
      <section className="border-t border-neutral-200 bg-neutral-50 py-20">
        <div className="mx-auto max-w-5xl px-6">
          <h2 className="text-2xl font-semibold text-neutral-900">
            You approve every gate. Nothing ships blind.
          </h2>
          <p className="mt-3 max-w-2xl text-neutral-600">
            The workflow pauses at the moments that matter and waits for you.
          </p>
          <div className="mt-10 grid gap-8 sm:grid-cols-3">
            {APPROVAL_STEPS.map((s) => (
              <div key={s.step} className="rounded-lg border border-neutral-200 bg-white p-6">
                <span className="text-sm font-medium text-amber-900">{s.step}</span>
                <h3 className="mt-2 font-semibold text-neutral-900">{s.title}</h3>
                <p className="mt-2 text-sm text-neutral-600">{s.body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Feature highlights */}
      <section className="py-20">
        <div className="mx-auto max-w-5xl px-6">
          <h2 className="text-2xl font-semibold text-neutral-900">
            Built for production, not a demo.
          </h2>
          <p className="mt-3 max-w-2xl text-neutral-600">
            Responsible AI-assisted development means holding generated code
            to the same bar you&apos;d hold your own team to.
          </p>
          <div className="mt-10 grid gap-6 sm:grid-cols-2">
            {FEATURES.map((f) => (
              <div key={f.title} className="rounded-lg border border-amber-300 bg-amber-50 p-6">
                <h3 className="font-semibold text-neutral-900">{f.title}</h3>
                <p className="mt-2 text-sm text-neutral-700">{f.body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Closing CTA */}
      <section className="border-t border-neutral-200 py-20">
        <div className="mx-auto max-w-2xl px-6 text-center">
          <h2 className="text-2xl font-semibold text-neutral-900">
            Sign in with Microsoft to start your first workflow.
          </h2>
          <form
            action={async () => {
              "use server";
              await signIn("microsoft-entra-id", { redirectTo: "/select" });
            }}
            className="mt-8"
          >
            <button
              type="submit"
              className="inline-flex items-center gap-2 rounded-lg bg-neutral-900 px-6 py-3 text-sm font-medium text-white transition-colors hover:bg-neutral-800"
            >
              Sign in with Microsoft
            </button>
          </form>
        </div>
      </section>
    </main>
  );
}
