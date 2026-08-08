using System.Text.Json;
using AiDev.Workflow.Application.Common.Contracts;
using Microsoft.Agents.AI.Workflows;
using Microsoft.Extensions.AI;

namespace AiDev.Workflow.Infrastructure.Workflow.Executors;

/// <summary>Seeds PlanAgent's first turn with the approved spec, as a serialized PlanLlmInput
/// (structured input — see PlanAgentFactory's instructions). See SpecReviseAdapter for why
/// planAgentNodeId (not PlanAgentFactory.Name) is the correct SendMessageAsync target.</summary>
internal sealed class SpecApprovedToPlanAdapter(string planAgentNodeId) : Executor(WorkflowExecutorIds.SpecApprovedToPlanAdapter)
{
	protected override ProtocolBuilder ConfigureProtocol(ProtocolBuilder protocolBuilder) =>
		protocolBuilder
			.SendsMessage<List<ChatMessage>>()
			.SendsMessage<TurnToken>()
			.ConfigureRoutes(routes => routes.AddHandler<ApprovedSpec>(HandleAsync, overwrite: false));

	private async ValueTask HandleAsync(ApprovedSpec approvedSpec, IWorkflowContext context, CancellationToken cancellationToken)
	{
		var input = new PlanLlmInput(approvedSpec, QuestionAnswers: null, FreeformNote: null);
		var messages = new List<ChatMessage>
		{
			new(ChatRole.User, JsonSerializer.Serialize(input, AIJsonUtilities.DefaultOptions)),
		};
		await context.SendMessageAsync(messages, planAgentNodeId, cancellationToken).ConfigureAwait(false);
		await context.SendMessageAsync(new TurnToken(emitEvents: true), planAgentNodeId, cancellationToken).ConfigureAwait(false);
	}
}
