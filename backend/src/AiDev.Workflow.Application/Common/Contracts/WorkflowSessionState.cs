namespace AiDev.Workflow.Application.Common.Contracts;

/// <summary>
/// The full session shape projected after every turn and synced to the client as shared AG-UI
/// state. See SessionStateProjectionExecutor for how each turn's structured output folds into this.
/// </summary>
public sealed record WorkflowSessionState(
	string RawRequirementsText,
	StepState Spec,
	StepState Plan,
	string ActiveTab);
