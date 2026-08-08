using Microsoft.Agents.AI.Workflows;
using Microsoft.Extensions.AI;

namespace AiDev.Workflow.Infrastructure.Workflow.Executors;

/// <summary>
/// Turns the human's revision note into the next SpecAgent turn (the loop-back edge).
/// Targets specAgentNodeId — the REAL graph executor id (captured via `new AIAgentBinding(agent).Id`
/// at build time), not the agent's display Name. SendMessageAsync's targetId is matched against
/// the executor node id, which for an AIAgent-wrapped node is derived internally (roughly
/// "{Name}_{Id}") and is NOT the same string passed to ChatClientAgentOptions.Name — confirmed via
/// live debugging (routing to the bare Name silently dropped the message with no exception). A
/// TurnToken must also be sent — the agent host executor buffers incoming messages and only runs
/// the agent's turn once a TurnToken arrives.
/// </summary>
internal sealed class SpecReviseAdapter(string specAgentNodeId) : Executor(WorkflowExecutorIds.SpecReviseAdapter)
{
	protected override ProtocolBuilder ConfigureProtocol(ProtocolBuilder protocolBuilder) =>
		protocolBuilder
			.SendsMessage<List<ChatMessage>>()
			.SendsMessage<TurnToken>()
			.ConfigureRoutes(routes => routes.AddHandler<string>(HandleAsync, overwrite: false));

	private async ValueTask HandleAsync(string note, IWorkflowContext context, CancellationToken cancellationToken)
	{
		var messages = new List<ChatMessage> { new(ChatRole.User, note) };
		await context.SendMessageAsync(messages, specAgentNodeId, cancellationToken).ConfigureAwait(false);
		await context.SendMessageAsync(new TurnToken(emitEvents: true), specAgentNodeId, cancellationToken).ConfigureAwait(false);
	}
}
