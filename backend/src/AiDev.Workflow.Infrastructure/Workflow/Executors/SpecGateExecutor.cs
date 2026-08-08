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

/// <summary>
/// Sits on both sides of the spec HITL gate: parses SpecAgent's structured turn output, validates
/// its LLM-authored A2ui payload, and forwards it to the human as a GateReviewRequest; then — once
/// the RequestPort resolves — decides whether to loop back to SpecAgent (revise) or forward the
/// approved spec (structured, not flattened — see ApprovedSpec) to PlanAgent. One executor instance
/// handles both edges since the approved-spec forwarding needs the output captured on the request
/// side, which GateReviewResponse doesn't carry back on its own.
/// Instance fields are safe: executors are long-lived for the life of one workflow run.
/// </summary>
internal sealed class SpecGateExecutor(int maxIterations) : Executor(WorkflowExecutorIds.SpecGateResponse)
{
	private SpecLlmOutput? _lastOutput;
	private int _iterations;

	protected override ProtocolBuilder ConfigureProtocol(ProtocolBuilder protocolBuilder) =>
		protocolBuilder
			.SendsMessage<GateReviewRequest>()
			.SendsMessage<string>()
			.SendsMessage<ApprovedSpec>()
			.ConfigureRoutes(routes => routes
				.AddHandler<List<ChatMessage>>(HandleSpecTurnAsync, overwrite: false)
				.AddHandler<string>(HandleGateResponseAsync, overwrite: false));

	private async ValueTask HandleSpecTurnAsync(List<ChatMessage> messages, IWorkflowContext context, CancellationToken cancellationToken)
	{
		// specAgent forwards both the user's input message and its own assistant response as
		// separate single-message deliveries — confirmed via live diagnostic logging. Only the
		// assistant's own output is the turn's actual result; the user-role echo must be ignored.
		var assistantMessages = messages.Where(m => m.Role == ChatRole.Assistant).ToList();
		if (assistantMessages.Count == 0)
		{
			return;
		}

		var json = string.Join(string.Empty, assistantMessages.Select(m => m.Text));
		var output = JsonSerializer.Deserialize<SpecLlmOutput>(json, AIJsonUtilities.DefaultOptions)
			?? throw new InvalidOperationException("SpecAgent returned an empty or unparseable structured response.");

		_lastOutput = output;

		var validation = A2UiSchemaValidator.Validate(output.A2ui, A2UiSurfaceIds.SpecContent);
		var a2ui = validation.IsValid
			? output.A2ui
			: A2UiSchemaValidator.BuildFallback(A2UiSurfaceIds.SpecContent, "The specification content couldn't be displayed. See the summary below.");

		var request = new GateReviewRequest(WorkflowPorts.SpecGateId, BuildContentSnapshot(output.Spec), output.ClarifyingQuestions, a2ui);
		await context.SendMessageAsync(request, WorkflowPorts.SpecGateId, cancellationToken).ConfigureAwait(false);
	}

	private ValueTask HandleGateResponseAsync(string rawResponse, IWorkflowContext context, CancellationToken cancellationToken)
	{
		var response = JsonSerializer.Deserialize<GateReviewResponse>(rawResponse, AIJsonUtilities.DefaultOptions)
			?? throw new InvalidOperationException("Spec gate received an empty or unparseable review response.");

		_iterations++;
		var forceApprove = _iterations >= maxIterations && response.Decision != GateDecision.Approve;
		var approved = response.Decision == GateDecision.Approve || forceApprove;

		if (approved)
		{
			if (_lastOutput is null)
			{
				throw new InvalidOperationException("Spec gate received an approval before any spec turn was recorded.");
			}

			return context.SendMessageAsync(_lastOutput.Spec, WorkflowExecutorIds.SpecApprovedToPlanAdapter, cancellationToken);
		}

		var input = new SpecLlmInput(RawRequirementsText: null, response.QuestionAnswers, response.FreeformNote);
		return context.SendMessageAsync(JsonSerializer.Serialize(input, AIJsonUtilities.DefaultOptions), WorkflowExecutorIds.SpecReviseAdapter, cancellationToken);
	}

	private static string BuildContentSnapshot(ApprovedSpec spec)
	{
		var sb = new StringBuilder();
		sb.AppendLine(CultureInfo.InvariantCulture, $"# {spec.Title}");
		sb.AppendLine();
		sb.AppendLine(spec.Summary);

		foreach (var story in spec.UserStories)
		{
			sb.AppendLine();
			sb.AppendLine(CultureInfo.InvariantCulture, $"## {story.Id}: {story.Title}");
			sb.AppendLine(story.Narrative);
			foreach (var ac in story.AcceptanceCriteria)
			{
				sb.AppendLine(CultureInfo.InvariantCulture, $"- [{ac.Id}] {ac.Description}");
			}
		}

		if (spec.Assumptions.Count > 0)
		{
			sb.AppendLine();
			sb.AppendLine("## Assumptions");
			foreach (var a in spec.Assumptions)
			{
				sb.AppendLine(CultureInfo.InvariantCulture, $"- {a}");
			}
		}

		if (spec.OutOfScope.Count > 0)
		{
			sb.AppendLine();
			sb.AppendLine("## Out of Scope");
			foreach (var o in spec.OutOfScope)
			{
				sb.AppendLine(CultureInfo.InvariantCulture, $"- {o}");
			}
		}

		return sb.ToString();
	}
}
