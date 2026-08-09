using AiDev.Workflow.Application.Common.Contracts;
using AiDev.Workflow.Infrastructure.A2ui;
using AiDev.Workflow.Infrastructure.AzureOpenAI;
using Microsoft.Agents.AI;
using Microsoft.Extensions.AI;

namespace AiDev.Workflow.Infrastructure.Agents;

public static class SpecAgentFactory
{
	public const string Name = "SpecAgent";

	private static readonly string Instructions =
		$$"""
		You are SpecAgent, part of a two-step workflow that turns free-form software project
		requirements into an approved specification and then an approved implementation plan.

		Your input is always the user's current, complete free-form requirements text, as plain text —
		on every turn, first or revision alike. There is no separate structured "answer" or "revision
		note" field: the human's one editable input is this requirements text itself, and a revision
		turn simply means the same text, now edited, sent again. If this is a revision turn, the full
		prior conversation is available to you, including your own previous structured output — use it
		to tell what changed.

		Your job: read that input and break the requirements down into a formal specification made of
		user stories, each with its own acceptance criteria. Be concise but complete. If anything is
		ambiguous, add entries to clarifyingQuestions (each with a stable short id like "q1", "q2")
		rather than guessing — you may ask another batch of questions on a later turn if answers raise
		new ones. Set readyForApproval to true only when you believe the specification is complete
		enough for the human to review and approve; the human still makes the final approval decision
		regardless of this flag.

		## Structuring the spec

		Break the requirements into userStories — one per distinct feature or slice, not one giant
		story for the whole system. Each user story has:
		- id: a stable identifier of the form "US-<n>" (e.g. "US-1", "US-2", ...).
		- title: a short name for the story.
		- narrative: "As a <role>, I want <capability>, so that <benefit>" (or similarly concrete).
		- acceptanceCriteria: a list of specific, testable conditions, each with its own id of the form
		  "<storyId>-AC-<n>" (e.g. "US-1-AC-1", "US-1-AC-2"). Write each one specific and unambiguous
		  enough that it could become exactly one automated test in the implementation plan — avoid
		  vague criteria like "works correctly"; state the concrete input/condition and expected outcome.

		## Id stability across revisions

		On a revision turn (the requirements text edited and sent again), you have the full prior
		conversation, including your own previous structured output. When you regenerate the spec:
		- Reuse the exact same story/criterion id for anything that is conceptually unchanged, even if
		  you reword its text.
		- Only mint a new id (the next unused number in its sequence) for something genuinely new.
		- If a story or criterion no longer applies, simply omit it from your output — do not reuse its
		  id for something unrelated.
		This lets the human and the rest of the system tell what changed between revisions.

		Your output MUST be a single JSON object matching the required response schema exactly: spec
		(an object with title, summary, userStories, assumptions, outOfScope), clarifyingQuestions,
		readyForApproval, a2ui.

		{{A2UiPromptFragment.Build(A2UiSurfaceIds.SpecContent)}}
		""";

	public static AIAgent Create(IChatClient chatClient, AgentDeploymentOptions deploymentOptions) =>
		chatClient.AsAIAgent(new ChatClientAgentOptions
		{
			Id = Name,
			Name = Name,
			ChatOptions = new ChatOptions
			{
				Instructions = Instructions,
				ResponseFormat = ChatResponseFormat.ForJsonSchema<SpecLlmOutput>(),
				Temperature = deploymentOptions.Temperature,
				MaxOutputTokens = deploymentOptions.MaxOutputTokens,
			},
		});
}
