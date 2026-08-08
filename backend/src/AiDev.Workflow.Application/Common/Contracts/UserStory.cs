namespace AiDev.Workflow.Application.Common.Contracts;

/// <summary>
/// A single feature/slice, broken out from the raw requirements. Id is stable across spec
/// revisions (e.g. "US-3") for the same reason as AcceptanceCriterion.Id.
/// </summary>
public sealed record UserStory(
	string Id,
	string Title,
	string Narrative,
	IReadOnlyList<AcceptanceCriterion> AcceptanceCriteria);
