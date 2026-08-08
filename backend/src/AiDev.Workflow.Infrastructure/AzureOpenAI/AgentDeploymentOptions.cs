namespace AiDev.Workflow.Infrastructure.AzureOpenAI;

public sealed class AgentDeploymentOptions
{
	public required string DeploymentName { get; init; }

	public float Temperature { get; init; } = 0.7f;

	/// <summary>
	/// Explicit output-token headroom. The structured SpecLlmOutput/PlanLlmOutput schemas are large
	/// (nested user stories/acceptance criteria plus a full A2ui component tree), and an unbounded
	/// or too-small provider default risks a response being cut off mid-JSON, which fails
	/// deserialization outright rather than degrading gracefully. 8192 was chosen as generous
	/// headroom observed against real output sizes, not a measured exact requirement.
	/// </summary>
	public int MaxOutputTokens { get; init; } = 8192;
}
