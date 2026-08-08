namespace AiDev.Workflow.Infrastructure.AzureOpenAI;

public sealed class AzureOpenAIOptions
{
	public const string SectionName = "AzureOpenAI";

	public required string Endpoint { get; init; }

	/// <summary>
	/// Reserved for future use. Azure.AI.OpenAI 2.x pins the API version via the
	/// <see cref="Azure.AI.OpenAI.AzureOpenAIClientOptions.ServiceVersion"/> enum rather than a
	/// free-text string, so this value isn't wired to the client yet — the SDK's default version
	/// is used instead.
	/// </summary>
	public string? ApiVersion { get; init; }

	/// <summary>"ApiKey" or "EntraId". Only "ApiKey" is wired up so far.</summary>
	public required string AuthMode { get; init; }

	public string? ApiKey { get; init; }

	public required AgentDeploymentOptions SpecAgent { get; init; }

	public required AgentDeploymentOptions PlanAgent { get; init; }
}
