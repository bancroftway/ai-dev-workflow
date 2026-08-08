using AiDev.Workflow.Application.Common.Contracts;
using Microsoft.Agents.AI.Workflows;

namespace AiDev.Workflow.Infrastructure.Workflow.Ports;

internal static class WorkflowPorts
{
	public const string SpecGateId = "SpecGate";
	public const string PlanGateId = "PlanGate";

	// TResponse is `string`, not GateReviewResponse: MAF's AG-UI hosting layer (AGUIChatMessageExtensions)
	// wraps an incoming "tool" message's raw content string directly into FunctionResultContent.Result
	// with no deserialization step. WorkflowSession.NormalizeResponseContentForDelivery then requires
	// Result's runtime type to literally match the port's ResponseType (or be a PortableValue instance,
	// which nothing on this path ever constructs) — so a non-string ResponseType can never be satisfied
	// from the wire. Using `string` here makes that type check trivially pass; the executors on the
	// receiving end deserialize the JSON into GateReviewResponse themselves.
	public static readonly RequestPort<GateReviewRequest, string> SpecGate =
		RequestPort.Create<GateReviewRequest, string>(SpecGateId);

	public static readonly RequestPort<GateReviewRequest, string> PlanGate =
		RequestPort.Create<GateReviewRequest, string>(PlanGateId);
}
