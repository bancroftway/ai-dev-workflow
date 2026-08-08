namespace AiDev.Workflow.Application.Common.Contracts;

/// <summary>
/// Structured input handed to SpecAgent on every turn — serialized to JSON and sent as the turn's
/// user message content, never as raw prose. Exactly one of RawRequirementsText (first turn) or
/// QuestionAnswers/FreeformNote (revision turns) is populated.
/// </summary>
public sealed record SpecLlmInput(
	string? RawRequirementsText,
	IReadOnlyDictionary<string, string>? QuestionAnswers,
	string? FreeformNote);
