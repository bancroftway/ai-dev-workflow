namespace AiDev.Workflow.Application.Common.Contracts;

/// <summary>
/// The spec's actual content, deliberately excluding turn-scoped fields (ClarifyingQuestions,
/// ReadyForApproval, A2ui) that belong to SpecLlmOutput but aren't part of "the spec" itself. Shared
/// between SpecLlmOutput (as its Spec property) and PlanLlmInput (as its ApprovedSpec property) so
/// PlanAgent receives the same structured UserStories/AcceptanceCriteria — with their stable Ids —
/// that the human approved, rather than a re-flattened string that loses that structure.
/// </summary>
public sealed record ApprovedSpec(
	string Title,
	string Summary,
	IReadOnlyList<UserStory> UserStories,
	IReadOnlyList<string> Assumptions,
	IReadOnlyList<string> OutOfScope);
