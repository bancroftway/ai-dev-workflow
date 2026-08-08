using Microsoft.Agents.AI.Workflows;

namespace AiDev.Workflow.Infrastructure.Workflow.Executors;

/// <summary>Yields the approved plan text as the workflow's final output.</summary>
internal sealed class WorkflowTerminalExecutor() : Executor(WorkflowExecutorIds.WorkflowTerminal)
{
	protected override ProtocolBuilder ConfigureProtocol(ProtocolBuilder protocolBuilder) =>
		protocolBuilder
			.YieldsOutput<string>()
			.ConfigureRoutes(routes => routes.AddHandler<string>(HandleAsync, overwrite: false));

	private static ValueTask HandleAsync(string approvedPlanText, IWorkflowContext context, CancellationToken cancellationToken) =>
		context.YieldOutputAsync(approvedPlanText, cancellationToken);
}
