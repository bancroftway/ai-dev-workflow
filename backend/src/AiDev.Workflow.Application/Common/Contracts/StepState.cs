using AiDev.Workflow.Domain.Enums;

namespace AiDev.Workflow.Application.Common.Contracts;

public sealed record StepState(object? Output, HitlPhase Phase, bool IsStale);
