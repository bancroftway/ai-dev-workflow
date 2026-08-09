using System.Text.Json.Nodes;

namespace AiDev.Workflow.Application.Common.Contracts;

/// <summary>
/// Structured output PlanAgent must produce every turn. See SpecLlmOutput for the A2ui field's
/// shape-vs-content guarantee note — it applies identically here.
/// </summary>
public sealed record PlanLlmOutput(
	string Overview,
	IReadOnlyList<PlanStep> Steps,
	IReadOnlyList<string> RiskNotes,
	IReadOnlyList<ClarifyingQuestion> ClarifyingQuestions,
	bool ReadyForApproval,
	IReadOnlyList<JsonObject> A2ui);
