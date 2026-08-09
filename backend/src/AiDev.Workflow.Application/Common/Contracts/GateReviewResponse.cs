using AiDev.Workflow.Domain.Enums;

namespace AiDev.Workflow.Application.Common.Contracts;

/// <summary>
/// The human's answer to a GateReviewRequest. OutputJson is GateReviewRequest.OutputJson echoed
/// back unchanged — the client never parses or constructs it, just holds and resends it, so
/// Approve resolution doesn't depend on the gate executor's own instance state. On Continue,
/// UpdatedRawRequirementsText carries the current (possibly edited) evergreen requirements text;
/// there is no more separate structured Q&amp;A/revision-note payload — the human's only editable
/// input is the requirements text itself.
/// </summary>
public sealed record GateReviewResponse(
	GateDecision Decision,
	string OutputJson,
	string? UpdatedRawRequirementsText);
