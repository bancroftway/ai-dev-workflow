namespace AiDev.Workflow.Infrastructure.Workflow.Executors;

internal static class WorkflowExecutorIds
{
	public const string SpecCapture = "SpecCapture";
	public const string SpecGateResponse = "SpecGateResponse";
	public const string SpecReviseAdapter = "SpecReviseAdapter";
	public const string SpecApprovedToPlanAdapter = "SpecApprovedToPlanAdapter";

	public const string PlanCapture = "PlanCapture";
	public const string PlanGateResponse = "PlanGateResponse";
	public const string PlanReviseAdapter = "PlanReviseAdapter";

	public const string WorkflowTerminal = "WorkflowTerminal";
}
