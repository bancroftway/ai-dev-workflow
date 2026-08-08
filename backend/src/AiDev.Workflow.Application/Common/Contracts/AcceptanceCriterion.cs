namespace AiDev.Workflow.Application.Common.Contracts;

/// <summary>
/// One testable condition under a UserStory. Id is stable across spec revisions (e.g. "US-3-AC-2")
/// so PlanAgent can reference it 1:1 when turning it into a test, and so a revision can tell which
/// criteria are unchanged/reworded (same Id) vs. genuinely new (a fresh Id) vs. removed (simply
/// absent from the next turn's output).
/// </summary>
public sealed record AcceptanceCriterion(string Id, string Description);
