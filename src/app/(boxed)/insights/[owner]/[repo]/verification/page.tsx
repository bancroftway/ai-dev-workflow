"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { useFetchedPipeline } from "@/lib/pipeline";

/** Mirrors verify_check_store.check_stats (GET /repos/{owner}/{repo}/verify-insights). */
interface VerifyInsights {
  checks: { check_id: string; stage: string; runs: number; fails: number; infra: number; fail_rate: number; last_failed: string | null }[];
  stages: { stage: string; avg_attempts_to_pass: number | null; sessions: number }[];
}

/**
 * Repo-scoped verification insights (plan §4): which deterministic checks fail most across this
 * repo's sessions, and how many attempts each stage takes to pass. Labels come from the live
 * pipeline descriptor; an id no current gate declares shows as "retired check (id)".
 */
export default function VerificationInsightsPage() {
  const params = useParams<{ owner: string; repo: string }>();
  const owner = decodeURIComponent(params.owner);
  const repo = decodeURIComponent(params.repo);
  const pipeline = useFetchedPipeline();
  const [mode, setMode] = useState("");
  const [since, setSince] = useState("");
  const [data, setData] = useState<VerifyInsights | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const qs = new URLSearchParams();
    if (mode) qs.set("mode", mode);
    if (since) qs.set("since", since);
    fetch(`/api/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/verify-insights?${qs}`)
      .then(async (r) => {
        const body = await r.json().catch(() => null);
        if (!r.ok) throw new Error(body?.detail ?? `failed to load (${r.status})`);
        return body as VerifyInsights;
      })
      .then((b) => {
        if (cancelled) return;
        setData(b);
        setError(null);
      })
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : "failed to load"));
    return () => {
      cancelled = true;
    };
  }, [owner, repo, mode, since]);

  const checkLabel = (id: string) => {
    const all = [
      ...(pipeline?.descriptor.wrapper_checks ?? []),
      ...(pipeline?.tabs.flatMap((t) => t.stages.flatMap((s) => s.gate?.checks ?? [])) ?? []),
    ];
    const found = all.find((c) => c.id === id);
    return found ? found.label : pipeline ? `retired check (${id})` : id;
  };
  const stageLabel = (key: string) => pipeline?.stageLabel(key) ?? key;

  return (
    <div className="flex h-full w-full flex-col gap-6 overflow-y-auto p-6">
      <div>
        <Link
          href={`/tickets/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`}
          className="text-sm text-neutral-500 hover:text-neutral-800"
        >
          ← Back to tickets
        </Link>
        <h1 className="mt-2 text-lg font-semibold">
          Verification insights — {owner}/{repo}
        </h1>
        <p className="text-sm text-neutral-500">
          How often each deterministic check fails across this repository’s sessions.
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-4 text-sm">
        <label className="flex flex-col gap-1">
          <span className="text-neutral-700">Mode</span>
          <select
            className="rounded-md border border-neutral-300 px-3 py-2"
            value={mode}
            onChange={(e) => setMode(e.target.value)}
          >
            <option value="">All modes</option>
            {pipeline?.modes.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-neutral-700">Since</span>
          <input
            type="date"
            className="rounded-md border border-neutral-300 px-3 py-2"
            value={since}
            onChange={(e) => setSince(e.target.value)}
          />
        </label>
      </div>

      {error && <p className="text-sm text-red-700">{error}</p>}
      {!data && !error && <p className="text-sm text-neutral-500">Loading…</p>}

      {data && (
        <>
          <section className="flex flex-col gap-2">
            <h2 className="text-sm font-medium text-neutral-700">Checks ranked by fail rate</h2>
            {data.checks.length === 0 ? (
              <p className="text-sm text-neutral-500">No verify attempts recorded for this filter yet.</p>
            ) : (
              <div className="overflow-x-auto rounded-lg border border-neutral-200">
                <table className="w-full text-left text-sm">
                  <thead className="border-b border-neutral-200 bg-neutral-50 text-xs text-neutral-500">
                    <tr>
                      {["Check", "Stage", "Runs", "Fails", "Infra", "Fail rate", "Last failed"].map((h) => (
                        <th key={h} className="px-3 py-2 font-medium">
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.checks.map((c) => (
                      <tr key={`${c.stage}:${c.check_id}`} className="border-b border-neutral-100 last:border-0">
                        <td className="px-3 py-2">
                          <div>{checkLabel(c.check_id)}</div>
                          <div className="text-xs text-neutral-400">{c.check_id}</div>
                        </td>
                        <td className="px-3 py-2 text-neutral-600">{stageLabel(c.stage)}</td>
                        <td className="px-3 py-2 tabular-nums">{c.runs}</td>
                        <td className="px-3 py-2 tabular-nums">{c.fails}</td>
                        <td className="px-3 py-2 tabular-nums">{c.infra}</td>
                        <td className="px-3 py-2 tabular-nums">{Math.round(c.fail_rate * 100)}%</td>
                        <td className="px-3 py-2 text-neutral-600">
                          {c.last_failed ? new Date(c.last_failed).toLocaleString() : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="flex flex-col gap-2">
            <h2 className="text-sm font-medium text-neutral-700">Average attempts to pass, per stage</h2>
            {data.stages.length === 0 ? (
              <p className="text-sm text-neutral-500">Nothing recorded yet.</p>
            ) : (
              <div className="overflow-x-auto rounded-lg border border-neutral-200">
                <table className="w-full text-left text-sm">
                  <thead className="border-b border-neutral-200 bg-neutral-50 text-xs text-neutral-500">
                    <tr>
                      {["Stage", "Avg attempts to pass", "Sessions"].map((h) => (
                        <th key={h} className="px-3 py-2 font-medium">
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {[...data.stages]
                      .sort((a, b) => (pipeline?.stageOrderIndex(a.stage) ?? 0) - (pipeline?.stageOrderIndex(b.stage) ?? 0))
                      .map((s) => (
                        <tr key={s.stage} className="border-b border-neutral-100 last:border-0">
                          <td className="px-3 py-2">{stageLabel(s.stage)}</td>
                          <td className="px-3 py-2 tabular-nums">
                            {s.avg_attempts_to_pass == null ? "never passed" : s.avg_attempts_to_pass.toFixed(1)}
                          </td>
                          <td className="px-3 py-2 tabular-nums">{s.sessions}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
