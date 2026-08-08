using AiDev.Workflow.Application.Common.Behaviors;
using AiDev.Workflow.Application.Diagnostics.Ping;
using FluentValidation;
using FluentValidation.Results;
using NSubstitute;

namespace AiDev.Workflow.UnitTests.Behaviors;

public class ValidationBehaviorTests
{
	[Fact]
	public async Task Handle_WithFailingValidator_ThrowsValidationExceptionAndDoesNotCallNext()
	{
		var validator = Substitute.For<IValidator<PingCommand>>();
		validator
			.ValidateAsync(Arg.Any<ValidationContext<PingCommand>>(), Arg.Any<CancellationToken>())
			.Returns(new ValidationResult([new ValidationFailure("Message", "Message is required")]));

		var behavior = new ValidationBehavior<PingCommand, PingResult>([validator]);
		var nextCalled = false;

		var act = () => behavior.Handle(
			new PingCommand(""),
			_ =>
			{
				nextCalled = true;
				return Task.FromResult(new PingResult(""));
			},
			CancellationToken.None);

		await Assert.ThrowsAsync<ValidationException>(act);
		Assert.False(nextCalled);
	}

	[Fact]
	public async Task Handle_WithPassingValidator_CallsNext()
	{
		var validator = Substitute.For<IValidator<PingCommand>>();
		validator
			.ValidateAsync(Arg.Any<ValidationContext<PingCommand>>(), Arg.Any<CancellationToken>())
			.Returns(new ValidationResult());

		var behavior = new ValidationBehavior<PingCommand, PingResult>([validator]);

		var result = await behavior.Handle(
			new PingCommand("hello"),
			_ => Task.FromResult(new PingResult("hello")),
			CancellationToken.None);

		Assert.Equal("hello", result.Echo);
	}

	[Fact]
	public async Task Handle_WithNoValidators_CallsNext()
	{
		var behavior = new ValidationBehavior<PingCommand, PingResult>([]);

		var result = await behavior.Handle(
			new PingCommand("hello"),
			_ => Task.FromResult(new PingResult("hello")),
			CancellationToken.None);

		Assert.Equal("hello", result.Echo);
	}
}
