using System.Text.Json.Nodes;

namespace AiDev.Workflow.Application.Common.Contracts;

/// <summary>
/// Structured output SpecAgent must produce every turn, enforced via
/// ChatResponseFormat.ForJsonSchema&lt;SpecLlmOutput&gt;(). Spec is the actual spec content (see
/// ApprovedSpec); ClarifyingQuestions/ReadyForApproval/A2ui are turn-scoped, not part of the spec
/// itself. A2ui is authored by the model itself as a list of A2UI v0.9.1 envelope messages (each
/// one of createSurface/updateComponents/updateDataModel/deleteSurface) — see
/// AiDev.Workflow.Infrastructure.A2ui.A2UiSchemaValidator for the structural checks applied after
/// generation. JSON Schema can only guarantee A2ui is "a list of objects", not that each object is
/// a well-formed envelope — that's a content guarantee, not a shape guarantee.
/// </summary>
public sealed record SpecLlmOutput(
	ApprovedSpec Spec,
	IReadOnlyList<ClarifyingQuestion> ClarifyingQuestions,
	bool ReadyForApproval,
	IReadOnlyList<JsonObject> A2ui);
