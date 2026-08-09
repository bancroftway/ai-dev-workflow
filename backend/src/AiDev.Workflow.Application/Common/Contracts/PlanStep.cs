namespace AiDev.Workflow.Application.Common.Contracts;

/// <summary>
/// A single step in the implementation plan. Id is stable across plan revisions (e.g. "PS-3") for
/// the same reason as UserStory.Id/AcceptanceCriterion.Id — it's what lets PlanAgent recognize "this
/// step is unchanged" across a revision instead of rewriting the whole plan from scratch.
/// </summary>
public sealed record PlanStep(string Id, string Description);
