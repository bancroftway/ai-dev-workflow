import type { ClarifyingQuestion } from "@/lib/workflow-types";

/** Shared amber clarifying-questions block. Question ids are only stable within the turn that
 * produced them, scoped per stage -- React keys are stage-qualified so two stages independently
 * minting "CQ-1" never collide (same rule RequirementsView documented before extraction). */
export function ClarifyingQuestions({
  stageKey,
  questions,
  hint,
}: {
  stageKey: string;
  questions: ClarifyingQuestion[];
  hint: string;
}) {
  if (questions.length === 0) return null;
  return (
    <div className="space-y-3 rounded-lg border border-amber-300 bg-amber-50 p-4">
      <h2 className="text-sm font-medium text-amber-900">Clarifying Questions</h2>
      <ul className="space-y-2">
        {questions.map((q) => (
          <li key={`${stageKey}-${q.id}`} className="text-sm text-amber-900">
            <span className="mr-1 font-mono text-xs text-amber-700">{q.id}</span>
            {q.question}
            {q.suggested_choices.length > 0 && (
              <div className="mt-1 text-xs text-amber-700">
                Suggestions: {q.suggested_choices.join(", ")}
              </div>
            )}
          </li>
        ))}
      </ul>
      <p className="text-xs text-amber-700">{hint}</p>
    </div>
  );
}
