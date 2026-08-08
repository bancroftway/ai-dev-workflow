namespace AiDev.Workflow.Infrastructure.A2ui;

public sealed record A2UiValidationResult(bool IsValid, IReadOnlyList<string> Errors)
{
	public static A2UiValidationResult Valid { get; } = new(true, []);

	public static A2UiValidationResult Invalid(IReadOnlyList<string> errors) => new(false, errors);
}
