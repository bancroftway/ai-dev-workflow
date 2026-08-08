using AiDev.Workflow.Domain.Enums;

namespace AiDev.Workflow.Application.Common.Contracts;

/// <summary>
/// The human's answer to a GateReviewRequest. QuestionAnswers maps ClarifyingQuestion.Id to the
/// human's answer for that question; FreeformNote is an extra revision comment not tied to any
/// specific question. Both feed directly into the next turn's SpecLlmInput/PlanLlmInput.
/// </summary>
public sealed record GateReviewResponse(
	GateDecision Decision,
	IReadOnlyDictionary<string, string>? QuestionAnswers,
	string? FreeformNote);
