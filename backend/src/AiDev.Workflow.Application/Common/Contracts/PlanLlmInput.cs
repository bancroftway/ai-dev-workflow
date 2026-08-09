namespace AiDev.Workflow.Application.Common.Contracts;

/// <summary>
/// Structured input handed to PlanAgent every time it runs. PlanAgent only ever receives this one
/// shape — sent fresh each time SpecGate resolves to Approve, cascading a (possibly revised)
/// ApprovedSpec down. PlanAgent has no separate revision-turn input shape: incremental updates come
/// from its own conversation history on the same thread (see the id-stability guidance in
/// PlanAgentFactory), not from a structured "what changed" field. Carries the same ApprovedSpec type
/// SpecLlmOutput produces, not a re-flattened string, so PlanAgent has direct access to each
/// UserStory's AcceptanceCriteria and their stable Ids.
/// </summary>
public sealed record PlanLlmInput(ApprovedSpec ApprovedSpec);
