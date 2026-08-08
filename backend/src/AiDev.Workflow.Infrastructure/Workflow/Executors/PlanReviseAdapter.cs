using Microsoft.Agents.AI.Workflows;
using Microsoft.Extensions.AI;

namespace AiDev.Workflow.Infrastructure.Workflow.Executors;

/// <summary>Turns the human's revision note into the next PlanAgent turn (the loop-back edge).
/// See SpecReviseAdapter for why planAgentNodeId (not PlanAgentFactory.Name) is the correct target.</summary>
internal sealed class PlanReviseAdapter(string planAgentNodeId) : Executor(WorkflowExecutorIds.PlanReviseAdapter)
{
	protected override ProtocolBuilder ConfigureProtocol(ProtocolBuilder protocolBuilder) =>
		protocolBuilder
			.SendsMessage<List<ChatMessage>>()
			.SendsMessage<TurnToken>()
			.ConfigureRoutes(routes => routes.AddHandler<string>(HandleAsync, overwrite: false));

	private async ValueTask HandleAsync(string note, IWorkflowContext context, CancellationToken cancellationToken)
	{
		var messages = new List<ChatMessage> { new(ChatRole.User, note) };
		await context.SendMessageAsync(messages, planAgentNodeId, cancellationToken).ConfigureAwait(false);
		await context.SendMessageAsync(new TurnToken(emitEvents: true), planAgentNodeId, cancellationToken).ConfigureAwait(false);
	}
}
