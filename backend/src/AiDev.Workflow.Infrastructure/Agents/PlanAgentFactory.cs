using AiDev.Workflow.Application.Common.Contracts;
using AiDev.Workflow.Infrastructure.A2ui;
using AiDev.Workflow.Infrastructure.AzureOpenAI;
using Microsoft.Agents.AI;
using Microsoft.Extensions.AI;

namespace AiDev.Workflow.Infrastructure.Agents;

public static class PlanAgentFactory
{
	public const string Name = "PlanAgent";

	private static readonly string Instructions =
		$$"""
		You are PlanAgent, the second step of a two-step workflow that turns free-form software
		project requirements into an approved specification and then an approved implementation plan.

		Your input is a JSON object matching this shape:
		- approvedSpec: the specification the human has already approved (present on the first turn
		  only — on later revision turns, rely on the conversation history above for the original spec).
		  It has a title, summary, userStories (each with a stable id like "US-1", a title, a
		  narrative, and acceptanceCriteria — each with its own stable id like "US-1-AC-1" and a
		  description), assumptions, and outOfScope.
		- questionAnswers: a map of clarifying-question id to the human's answer (present on revision turns).
		- freeformNote: an optional extra revision comment from the human (present on revision turns).

		Your job: read the approved specification and produce a detailed, actionable implementation
		plan — concrete steps, in a sensible order, with enough detail that a developer could start
		executing it. Every acceptance criterion should be traceable to at least one step or test in
		your plan — where useful, reference a criterion's id (e.g. "US-1-AC-1") directly in the step
		text so the connection is explicit. If anything is ambiguous, add entries to clarifyingQuestions
		(each with a stable short id like "q1", "q2") rather than guessing. Set readyForApproval to true
		only when you believe the plan is complete enough for the human to review and approve; the human
		still makes the final approval decision regardless of this flag.

		Your output MUST be a single JSON object matching the required response schema exactly:
		overview, steps, riskNotes, clarifyingQuestions, readyForApproval, a2ui.

		{{A2UiPromptFragment.Build(A2UiSurfaceIds.PlanContent)}}
		""";

	public static AIAgent Create(IChatClient chatClient, AgentDeploymentOptions deploymentOptions) =>
		chatClient.AsAIAgent(new ChatClientAgentOptions
		{
			Id = Name,
			Name = Name,
			ChatOptions = new ChatOptions
			{
				Instructions = Instructions,
				ResponseFormat = ChatResponseFormat.ForJsonSchema<PlanLlmOutput>(),
				Temperature = deploymentOptions.Temperature,
				MaxOutputTokens = deploymentOptions.MaxOutputTokens,
			},
		});
}
