using System.Text.Json.Nodes;

namespace AiDev.Workflow.Application.Common.Contracts;

/// <summary>
/// What the HITL gate asks the human to review. A2ui is already validated (or replaced with the
/// fallback card) by the time this reaches the port — see A2UiSchemaValidator. OutputJson is the
/// exact structured turn output (SpecLlmOutput/PlanLlmOutput, serialized) that produced this
/// request — the client echoes it back verbatim in GateReviewResponse on Approve, so the gate
/// executor never has to rely on its own (singleton-shared, unscoped) instance state to recover
/// "what am I approving".
/// ReadyForApproval is deliberately the *last* property: this DTO is large (OutputJson duplicates
/// the entire structured turn output) and streams to the client as AG-UI tool-call argument deltas,
/// which the client renders incrementally from a partial JSON parse. Property order here is JSON
/// property order on the wire, so keeping the field the UI gates rendering decisions on last means
/// it can only ever appear once every other field — including the large OutputJson — has already
/// fully arrived, ruling out a "looks complete enough to validate, but OutputJson is still streaming
/// mid-string" render.
/// </summary>
public sealed record GateReviewRequest(
	string GateId,
	string ContentSnapshot,
	IReadOnlyList<ClarifyingQuestion> ClarifyingQuestions,
	IReadOnlyList<JsonObject> A2ui,
	string OutputJson,
	bool ReadyForApproval);
