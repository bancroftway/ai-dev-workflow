using System.Text.Json;
using AiDev.Workflow.Application.Common.Contracts;
using AiDev.Workflow.Domain.Enums;
using AiDev.Workflow.Infrastructure.DependencyInjection;
using Microsoft.Agents.AI;
using Microsoft.Extensions.AI;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Xunit.Abstractions;

namespace AiDev.Workflow.IntegrationTests;

/// <summary>
/// Live, in-process harness for the HITL round-trip — matches the plan's M4 verification bar
/// ("console harness drives RunStreamingAsync, manually resolves requests") using the SDK's own
/// FunctionResultContent type directly rather than hand-rolled AG-UI wire JSON, which sidesteps
/// an internal serialization contract not meant for external construction. Needs real Azure
/// OpenAI credentials in user-secrets/environment — run manually, not part of the default suite.
/// </summary>
[Trait("Category", "Live")]
public class WorkflowHitlLiveTests(ITestOutputHelper output)
{
	private static readonly JsonSerializerOptions LogSerializerOptions = new() { WriteIndented = true };

	[Fact]
	public async Task FullRun_ApproveSpecAndPlan_ProducesFinalOutput()
	{
		var host = Host.CreateApplicationBuilder();
		host.Configuration.AddUserSecrets("1e4c3808-d173-4dc7-ace2-d32c8e868716");
		host.Logging.ClearProviders();
		host.Logging.AddProvider(new TestOutputLoggerProvider(output));
		host.Logging.SetMinimumLevel(LogLevel.Trace);
		host.AddInfrastructure();
		using var app = host.Build();

		var agent = app.Services.GetRequiredKeyedService<AIAgent>("AiDevWorkflow");
		var session = await agent.CreateSessionAsync();

		var messages = new List<ChatMessage>
		{
			new(ChatRole.User, "Build a tool that lets support agents triage tickets by urgency."),
		};

		// --- Turn 1: expect a pending SpecGate request ---
		var specCall = await RunStreamingAndFindCallAsync(agent, messages, session, "SpecGate")
			?? throw new InvalidOperationException("Expected a pending SpecGate function call.");
		LogGateRequestArgs("Spec", specCall.Arguments);

		// --- Turn 2: approve the spec ---
		var approveSpec = new ChatMessage(ChatRole.Tool,
		[
			new FunctionResultContent(specCall.CallId, JsonSerializer.Serialize(new GateReviewResponse(GateDecision.Approve, QuestionAnswers: null, FreeformNote: null), AIJsonUtilities.DefaultOptions)),
		]);
		var planCall = await RunStreamingAndFindCallAsync(agent, [approveSpec], session, "PlanGate")
			?? throw new InvalidOperationException("Expected a pending PlanGate function call.");
		LogGateRequestArgs("Plan", planCall.Arguments);

		// --- Turn 3: approve the plan -> workflow should complete ---
		var approvePlan = new ChatMessage(ChatRole.Tool,
		[
			new FunctionResultContent(planCall.CallId, JsonSerializer.Serialize(new GateReviewResponse(GateDecision.Approve, QuestionAnswers: null, FreeformNote: null), AIJsonUtilities.DefaultOptions)),
		]);
		var finalText = await RunStreamingAndCollectTextAsync(agent, [approvePlan], session);
		output.WriteLine($"Final response text: {finalText}");
		Assert.False(string.IsNullOrWhiteSpace(finalText));
	}

	private void LogGateRequestArgs(string label, IDictionary<string, object?>? arguments)
	{
		var json = JsonSerializer.Serialize(arguments, LogSerializerOptions);
		output.WriteLine($"{label} gate request args:\n{json}");
	}

	private async Task<FunctionCallContent?> RunStreamingAndFindCallAsync(
		AIAgent agent, List<ChatMessage> messages, AgentSession session, string toolName)
	{
		FunctionCallContent? found = null;
		await foreach (var update in agent.RunStreamingAsync(messages, session).ConfigureAwait(false))
		{
			foreach (var content in update.Contents)
			{
				var detail = content is ErrorContent ec
					? $"[{ec.ErrorCode}] {ec.Message}\n{ec.Details}\nRawRepresentation: {ec.RawRepresentation}"
					: content.ToString();
				output.WriteLine($"[stream] {update.AuthorName ?? "(port)"}: {content.GetType().Name} {detail}");
				if (content is FunctionCallContent fc && string.Equals(fc.Name, toolName, StringComparison.Ordinal))
				{
					found = fc;
				}
			}
		}

		return found;
	}

	private async Task<string> RunStreamingAndCollectTextAsync(AIAgent agent, List<ChatMessage> messages, AgentSession session)
	{
		var text = new System.Text.StringBuilder();
		await foreach (var update in agent.RunStreamingAsync(messages, session).ConfigureAwait(false))
		{
			foreach (var content in update.Contents)
			{
				var detail = content is ErrorContent ec
					? $"[{ec.ErrorCode}] {ec.Message}\n{ec.Details}\nRawRepresentation: {ec.RawRepresentation}"
					: content.ToString();
				output.WriteLine($"[stream] {update.AuthorName ?? "(port)"}: {content.GetType().Name} {detail}");
			}

			if (!string.IsNullOrEmpty(update.Text))
			{
				text.Append(update.Text);
			}
		}

		return text.ToString();
	}
}
