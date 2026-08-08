using System.Globalization;
using System.Text;
using System.Text.Json;
using AiDev.Workflow.Application.Common.Contracts;
using AiDev.Workflow.Domain.Enums;
using AiDev.Workflow.Infrastructure.A2ui;
using AiDev.Workflow.Infrastructure.Workflow.Ports;
using Microsoft.Agents.AI.Workflows;
using Microsoft.Extensions.AI;

namespace AiDev.Workflow.Infrastructure.Workflow.Executors;

/// <summary>Mirrors SpecGateExecutor for the plan gate. On approve, forwards to the terminal
/// executor instead of another adapter — there's no third step after the plan.</summary>
internal sealed class PlanGateExecutor(int maxIterations) : Executor(WorkflowExecutorIds.PlanGateResponse)
{
	private PlanLlmOutput? _lastOutput;
	private int _iterations;

	protected override ProtocolBuilder ConfigureProtocol(ProtocolBuilder protocolBuilder) =>
		protocolBuilder
			.SendsMessage<GateReviewRequest>()
			.SendsMessage<string>()
			.ConfigureRoutes(routes => routes
				.AddHandler<List<ChatMessage>>(HandlePlanTurnAsync, overwrite: false)
				.AddHandler<string>(HandleGateResponseAsync, overwrite: false));

	private async ValueTask HandlePlanTurnAsync(List<ChatMessage> messages, IWorkflowContext context, CancellationToken cancellationToken)
	{
		// See SpecGateExecutor.HandleSpecTurnAsync — planAgent forwards both the seed user message
		// and its own assistant response as separate single-message deliveries; only the assistant
		// output is the real turn result.
		var assistantMessages = messages.Where(m => m.Role == ChatRole.Assistant).ToList();
		if (assistantMessages.Count == 0)
		{
			return;
		}

		var json = string.Join(string.Empty, assistantMessages.Select(m => m.Text));
		var output = JsonSerializer.Deserialize<PlanLlmOutput>(json, AIJsonUtilities.DefaultOptions)
			?? throw new InvalidOperationException("PlanAgent returned an empty or unparseable structured response.");

		_lastOutput = output;

		var validation = A2UiSchemaValidator.Validate(output.A2ui, A2UiSurfaceIds.PlanContent);
		var a2ui = validation.IsValid
			? output.A2ui
			: A2UiSchemaValidator.BuildFallback(A2UiSurfaceIds.PlanContent, "The plan content couldn't be displayed. See the summary below.");

		var request = new GateReviewRequest(WorkflowPorts.PlanGateId, BuildContentSnapshot(output), output.ClarifyingQuestions, a2ui);
		await context.SendMessageAsync(request, WorkflowPorts.PlanGateId, cancellationToken).ConfigureAwait(false);
	}

	private ValueTask HandleGateResponseAsync(string rawResponse, IWorkflowContext context, CancellationToken cancellationToken)
	{
		var response = JsonSerializer.Deserialize<GateReviewResponse>(rawResponse, AIJsonUtilities.DefaultOptions)
			?? throw new InvalidOperationException("Plan gate received an empty or unparseable review response.");

		_iterations++;
		var forceApprove = _iterations >= maxIterations && response.Decision != GateDecision.Approve;
		var approved = response.Decision == GateDecision.Approve || forceApprove;

		if (approved)
		{
			if (_lastOutput is null)
			{
				throw new InvalidOperationException("Plan gate received an approval before any plan turn was recorded.");
			}

			return context.SendMessageAsync(BuildContentSnapshot(_lastOutput), WorkflowExecutorIds.WorkflowTerminal, cancellationToken);
		}

		var input = new PlanLlmInput(ApprovedSpec: null, response.QuestionAnswers, response.FreeformNote);
		return context.SendMessageAsync(JsonSerializer.Serialize(input, AIJsonUtilities.DefaultOptions), WorkflowExecutorIds.PlanReviseAdapter, cancellationToken);
	}

	private static string BuildContentSnapshot(PlanLlmOutput output)
	{
		var sb = new StringBuilder();
		sb.AppendLine("# Implementation Plan");
		sb.AppendLine();
		sb.AppendLine(output.Overview);
		sb.AppendLine();
		sb.AppendLine("## Steps");
		foreach (var step in output.Steps)
		{
			sb.AppendLine(CultureInfo.InvariantCulture, $"- {step}");
		}

		if (output.RiskNotes.Count > 0)
		{
			sb.AppendLine();
			sb.AppendLine("## Risk Notes");
			foreach (var r in output.RiskNotes)
			{
				sb.AppendLine(CultureInfo.InvariantCulture, $"- {r}");
			}
		}

		return sb.ToString();
	}
}
