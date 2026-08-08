using AiDev.Workflow.Infrastructure.Agents;
using AiDev.Workflow.Infrastructure.AzureOpenAI;
using AiDev.Workflow.Infrastructure.Workflow.Executors;
using AiDev.Workflow.Infrastructure.Workflow.Ports;
using Microsoft.Agents.AI;
using Microsoft.Agents.AI.Workflows;
using Microsoft.Extensions.AI;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;

namespace AiDev.Workflow.Infrastructure.Workflow;

/// <summary>
/// Builds the SpecAgent -> [HITL gate] -> PlanAgent -> [HITL gate] -> output workflow.
/// Hosted (session-aware) via IHostApplicationBuilder.AddWorkflow(...).AddAsAIAgent(...) in
/// AddInfrastructure — NOT the bare Workflow.AsAIAgent() extension, which was confirmed (by
/// reflection + a live cross-request memory test) not to preserve conversation state across
/// separate HTTP requests.
/// </summary>
public static class WorkflowGraphBuilder
{
	/// <summary>Must match the name passed to IHostApplicationBuilder.AddWorkflow(...) in AddInfrastructure.</summary>
	public const string WorkflowName = "aidev-workflow";

	public static Microsoft.Agents.AI.Workflows.Workflow Build(IServiceProvider sp)
	{
		var specChatClient = sp.GetRequiredKeyedService<IChatClient>("SpecAgent");
		var planChatClient = sp.GetRequiredKeyedService<IChatClient>("PlanAgent");
		var options = sp.GetRequiredService<IOptions<WorkflowOptions>>().Value;
		var azureOpenAIOptions = sp.GetRequiredService<IOptions<AzureOpenAIOptions>>().Value;

		var specAgent = SpecAgentFactory.Create(specChatClient, azureOpenAIOptions.SpecAgent);
		var planAgent = PlanAgentFactory.Create(planChatClient, azureOpenAIOptions.PlanAgent);

		// The real graph executor id for an AIAgent-wrapped node is NOT the Name passed to
		// ChatClientAgentOptions — it's derived internally. SendMessageAsync's targetId must match
		// this real id or the message is silently dropped. Capture it via AIAgentBinding.
		var specAgentNodeId = new AIAgentBinding(specAgent, (AIAgentHostOptions?)null).Id;
		var planAgentNodeId = new AIAgentBinding(planAgent, (AIAgentHostOptions?)null).Id;

		var specGate = new SpecGateExecutor(options.MaxGateIterations);
		var specReviseAdapter = new SpecReviseAdapter(specAgentNodeId);
		var specApprovedToPlanAdapter = new SpecApprovedToPlanAdapter(planAgentNodeId);
		var planGate = new PlanGateExecutor(options.MaxGateIterations);
		var planReviseAdapter = new PlanReviseAdapter(planAgentNodeId);
		var terminal = new WorkflowTerminalExecutor();

		return new WorkflowBuilder(specAgent)
			.AddEdge(specAgent, specGate)
			.AddEdge(specGate, WorkflowPorts.SpecGate)
			.AddEdge(WorkflowPorts.SpecGate, specGate)
			.AddEdge(specGate, specReviseAdapter)
			.AddEdge(specGate, specApprovedToPlanAdapter)
			.AddEdge(specReviseAdapter, specAgent)
			.AddEdge(specApprovedToPlanAdapter, planAgent)
			.AddEdge(planAgent, planGate)
			.AddEdge(planGate, WorkflowPorts.PlanGate)
			.AddEdge(WorkflowPorts.PlanGate, planGate)
			.AddEdge(planGate, planReviseAdapter)
			.AddEdge(planGate, terminal)
			.AddEdge(planReviseAdapter, planAgent)
			.WithOutputFrom(terminal)
			.WithName(WorkflowName)
			.Build();
	}
}
