using FluentValidation;

namespace AiDev.Workflow.Application.Diagnostics.Ping;

public sealed class PingValidator : AbstractValidator<PingCommand>
{
	public PingValidator()
	{
		RuleFor(x => x.Message).NotEmpty();
	}
}
