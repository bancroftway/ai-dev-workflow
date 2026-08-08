using AiDev.Workflow.Application.Common.Contracts;
using AiDev.Workflow.Application.Session;
using AiDev.Workflow.Domain.Enums;

namespace AiDev.Workflow.UnitTests.Session;

public class WorkflowSessionStateProjectorTests
{
	private static readonly ApprovedSpec SampleApprovedSpec = new("Title", "Summary", [], [], []);

	private static readonly SpecLlmOutput SampleSpec = new(
		SampleApprovedSpec, ClarifyingQuestions: [], ReadyForApproval: true, A2ui: []);

	private static readonly PlanLlmOutput SamplePlan = new(
		"Overview", [], [], [], ReadyForApproval: true, []);

	[Fact]
	public void InitialState_SetsSpecInProgressAndPlanNotStarted()
	{
		var state = WorkflowSessionStateProjector.InitialState("raw text");

		Assert.Equal("raw text", state.RawRequirementsText);
		Assert.Equal(HitlPhase.InProgress, state.Spec.Phase);
		Assert.Null(state.Spec.Output);
		Assert.Equal(HitlPhase.NotStarted, state.Plan.Phase);
		Assert.Equal("requirements", state.ActiveTab);
	}

	[Fact]
	public void WithSpecUpdate_SetsOutputPhaseAndActiveTab()
	{
		var state = WorkflowSessionStateProjector.InitialState("raw text");

		var updated = WorkflowSessionStateProjector.WithSpecUpdate(state, SampleSpec, HitlPhase.PendingApproval);

		Assert.Same(SampleSpec, updated.Spec.Output);
		Assert.Equal(HitlPhase.PendingApproval, updated.Spec.Phase);
		Assert.False(updated.Spec.IsStale);
		Assert.Equal("spec", updated.ActiveTab);
	}

	[Fact]
	public void WithPlanUpdate_SetsOutputPhaseAndActiveTab()
	{
		var state = WorkflowSessionStateProjector.InitialState("raw text");

		var updated = WorkflowSessionStateProjector.WithPlanUpdate(state, SamplePlan, HitlPhase.Approved);

		Assert.Same(SamplePlan, updated.Plan.Output);
		Assert.Equal(HitlPhase.Approved, updated.Plan.Phase);
		Assert.Equal("plan", updated.ActiveTab);
	}

	[Fact]
	public void WithRequirementsEdited_MarksExistingSpecAndPlanOutputStale_ButNotEmptyStepStates()
	{
		var state = WorkflowSessionStateProjector.InitialState("original text");
		state = WorkflowSessionStateProjector.WithSpecUpdate(state, SampleSpec, HitlPhase.Approved);
		// Plan is still NotStarted (no output) at this point.

		var edited = WorkflowSessionStateProjector.WithRequirementsEdited(state, "edited text");

		Assert.Equal("edited text", edited.RawRequirementsText);
		Assert.True(edited.Spec.IsStale);
		Assert.Same(SampleSpec, edited.Spec.Output);
		Assert.False(edited.Plan.IsStale);
		Assert.Null(edited.Plan.Output);
		Assert.Equal("requirements", edited.ActiveTab);
	}

	[Fact]
	public void WithRequirementsEdited_MarksBothStale_WhenBothHaveOutput()
	{
		var state = WorkflowSessionStateProjector.InitialState("original text");
		state = WorkflowSessionStateProjector.WithSpecUpdate(state, SampleSpec, HitlPhase.Approved);
		state = WorkflowSessionStateProjector.WithPlanUpdate(state, SamplePlan, HitlPhase.PendingApproval);

		var edited = WorkflowSessionStateProjector.WithRequirementsEdited(state, "edited text");

		Assert.True(edited.Spec.IsStale);
		Assert.True(edited.Plan.IsStale);
	}
}
