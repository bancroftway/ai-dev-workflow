using AiDev.Workflow.Infrastructure.AzureOpenAI;
using AiDev.Workflow.Infrastructure.Workflow;
using Microsoft.Agents.AI.Hosting;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;

namespace AiDev.Workflow.Infrastructure.DependencyInjection;

public static class AddInfrastructureExtensions
{
	/// <summary>
	/// Registers Azure OpenAI + the SpecAgent/PlanAgent workflow, hosted via the
	/// AddWorkflow(...).AddAsAIAgent(...).WithInMemorySessionStore() pattern rather than the
	/// bare Workflow.AsAIAgent() extension — the hosted-builder path is what actually preserves
	/// conversation state across separate HTTP requests (confirmed live; the bare extension does not).
	/// Returns the IHostedAgentBuilder so Program.cs can pass it to MapAGUIServer after Build().
	/// </summary>
	public static IHostedAgentBuilder AddInfrastructure(this IHostApplicationBuilder builder)
	{
		builder.Services.AddAzureOpenAI(builder.Configuration);
		builder.Services.Configure<WorkflowOptions>(builder.Configuration.GetSection(WorkflowOptions.SectionName));

		return builder
			// Default (Singleton) lifetime. WorkflowSession (internal) keeps its own
			// _inMemoryCheckpointManager + _pendingRequests + LastCheckpoint as instance fields, so
			// resume across HTTP requests requires the same WorkflowSession instance to survive —
			// confirmed working end-to-end via the live HITL integration test.
			.AddWorkflow(WorkflowGraphBuilder.WorkflowName, (sp, _) => WorkflowGraphBuilder.Build(sp))
			.AddAsAIAgent("AiDevWorkflow")
			// withIsolation defaults to true and requires a SessionIsolationKeyProvider (meant for
			// multi-tenant/authenticated scenarios). Auth is explicitly out of scope here (see plan),
			// so isolation is disabled — every session is scoped only by its AG-UI threadId.
			.WithInMemorySessionStore(withIsolation: false);
	}
}
