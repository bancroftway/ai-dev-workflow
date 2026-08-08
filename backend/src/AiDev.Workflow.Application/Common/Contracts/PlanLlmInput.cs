namespace AiDev.Workflow.Application.Common.Contracts;

/// <summary>
/// Structured input handed to PlanAgent on every turn. ApprovedSpec is set only on the first turn
/// (Plan only ever starts once Spec is approved) — later revision turns rely on conversation
/// history for the spec content and instead set QuestionAnswers/FreeformNote. Carries the same
/// ApprovedSpec type SpecLlmOutput produces, not a re-flattened string, so PlanAgent has direct
/// access to each UserStory's AcceptanceCriteria and their stable Ids.
/// </summary>
public sealed record PlanLlmInput(
	ApprovedSpec? ApprovedSpec,
	IReadOnlyDictionary<string, string>? QuestionAnswers,
	string? FreeformNote);
