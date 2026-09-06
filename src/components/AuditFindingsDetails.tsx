/** Collapsed "what the adversarial audit changed" block, shared by SpecificationView/PlanView. */
export function AuditFindingsDetails({ findings }: { findings: string[] }) {
  if (findings.length === 0) return null;
  return (
    <details className="rounded-lg border border-neutral-200 px-3 py-2 text-sm">
      <summary className="cursor-pointer text-neutral-700">
        Adversarial audit revised this draft — {findings.length} finding(s) addressed
      </summary>
      <ul className="mt-1 list-inside list-disc text-xs text-neutral-600">
        {findings.map((finding, index) => (
          <li key={index}>{finding}</li>
        ))}
      </ul>
    </details>
  );
}
