using System.Text.Json.Nodes;

namespace AiDev.Workflow.Application.Common.Contracts;

/// <summary>
/// What the HITL gate asks the human to review. A2ui is already validated (or replaced with the
/// fallback card) by the time this reaches the port — see A2UiSchemaValidator.
/// </summary>
public sealed record GateReviewRequest(
	string GateId,
	string ContentSnapshot,
	IReadOnlyList<ClarifyingQuestion> ClarifyingQuestions,
	IReadOnlyList<JsonObject> A2ui);
