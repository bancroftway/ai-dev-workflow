namespace AiDev.Workflow.Application.Common.Contracts;

public sealed record ClarifyingQuestion(string Id, string Question, IReadOnlyList<string>? Choices);
