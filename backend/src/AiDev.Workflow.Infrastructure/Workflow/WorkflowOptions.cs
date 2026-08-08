namespace AiDev.Workflow.Infrastructure.Workflow;

public sealed class WorkflowOptions
{
	public const string SectionName = "Workflow";

	/// <summary>Loop-guard cap per gate: after this many Continue iterations without an Approve,
	/// the gate auto-approves rather than looping forever.</summary>
	public int MaxGateIterations { get; init; } = 5;
}
