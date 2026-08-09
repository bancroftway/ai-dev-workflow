using Microsoft.Agents.AI.Workflows;
using Microsoft.Extensions.AI;

namespace AiDev.Workflow.Infrastructure.Workflow.Executors;

/// <summary>
/// Turns the human's updated evergreen requirements text into the next SpecAgent turn (the loop-back
/// edge). Shared by both gates: SpecGate's own Continue AND PlanGate's Continue both target this
/// same adapter, since the evergreen requirements text is the single source of truth for both spec
/// and plan — any edit restarts at SpecAgent, which cascades to PlanAgent again once (re-)approved.
/// The input here is always the plain requirements text (never a serialized DTO); SpecAgent's own
/// conversation history (same thread, carried across turns) is what lets it recognize which parts
/// are unchanged and keep their ids/wording stable — see SpecAgentFactory's "Id stability across
/// revisions" instructions.
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

	private async ValueTask HandleAsync(string updatedRawRequirementsText, IWorkflowContext context, CancellationToken cancellationToken)
	{
		var messages = new List<ChatMessage> { new(ChatRole.User, updatedRawRequirementsText) };
		await context.SendMessageAsync(messages, specAgentNodeId, cancellationToken).ConfigureAwait(false);
		await context.SendMessageAsync(new TurnToken(emitEvents: true), specAgentNodeId, cancellationToken).ConfigureAwait(false);
	}
}
