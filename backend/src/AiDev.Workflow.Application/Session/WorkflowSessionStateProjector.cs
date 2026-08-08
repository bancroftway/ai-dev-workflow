using AiDev.Workflow.Application.Common.Contracts;
using AiDev.Workflow.Domain.Enums;

namespace AiDev.Workflow.Application.Session;

/// <summary>
/// Pure folding logic for WorkflowSessionState — no I/O, no framework dependency, so it's testable
/// in isolation. Not yet wired into the workflow graph: how a projected state actually reaches the
/// client (an AG-UI STATE_SNAPSHOT push from a workflow executor, vs. something computed at the API
/// layer from the agent's own output) is exactly what M5's AG-UI event-type spike needs to answer
/// first — wiring this in prematurely would mean guessing at that shape twice.
/// </summary>
public static class WorkflowSessionStateProjector
{
	public static WorkflowSessionState InitialState(string rawRequirementsText) => new(
		rawRequirementsText,
		Spec: new StepState(Output: null, Phase: HitlPhase.InProgress, IsStale: false),
		Plan: new StepState(Output: null, Phase: HitlPhase.NotStarted, IsStale: false),
		ActiveTab: "requirements");

	public static WorkflowSessionState WithSpecUpdate(WorkflowSessionState state, SpecLlmOutput output, HitlPhase phase) =>
		state with { Spec = new StepState(output, phase, IsStale: false), ActiveTab = "spec" };

	public static WorkflowSessionState WithPlanUpdate(WorkflowSessionState state, PlanLlmOutput output, HitlPhase phase) =>
		state with { Plan = new StepState(output, phase, IsStale: false), ActiveTab = "plan" };

	/// <summary>
	/// The human edited the requirements text after Spec and/or Plan already exist. Marks any
	/// StepState that already has output as stale rather than clearing it, so the UI can show a
	/// "this may be out of date" banner over the last-known content instead of losing it.
	/// </summary>
	public static WorkflowSessionState WithRequirementsEdited(WorkflowSessionState state, string newText) => state with
	{
		RawRequirementsText = newText,
		Spec = state.Spec.Output is null ? state.Spec : state.Spec with { IsStale = true },
		Plan = state.Plan.Output is null ? state.Plan : state.Plan with { IsStale = true },
		ActiveTab = "requirements",
	};
}
