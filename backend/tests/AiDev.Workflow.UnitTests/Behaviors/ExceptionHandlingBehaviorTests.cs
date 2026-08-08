using AiDev.Workflow.Application.Common.Behaviors;
using AiDev.Workflow.Application.Common.Exceptions;
using AiDev.Workflow.Application.Diagnostics.Ping;
using FluentValidation;
using Microsoft.Extensions.Logging;
using NSubstitute;

namespace AiDev.Workflow.UnitTests.Behaviors;

public class ExceptionHandlingBehaviorTests
{
	[Fact]
	public async Task Handle_WhenNextThrowsGenericException_WrapsInAppException()
	{
		var logger = Substitute.For<ILogger<ExceptionHandlingBehavior<PingCommand, PingResult>>>();
		var behavior = new ExceptionHandlingBehavior<PingCommand, PingResult>(logger);

		var act = () => behavior.Handle(
			new PingCommand("hello"),
			_ => throw new InvalidOperationException("boom"),
			CancellationToken.None);

		var thrown = await Assert.ThrowsAsync<AppException>(act);
		Assert.IsType<InvalidOperationException>(thrown.InnerException);
	}

	[Fact]
	public Task Handle_WhenNextThrowsValidationException_PassesThroughUnwrapped()
	{
		var logger = Substitute.For<ILogger<ExceptionHandlingBehavior<PingCommand, PingResult>>>();
		var behavior = new ExceptionHandlingBehavior<PingCommand, PingResult>(logger);

		var act = () => behavior.Handle(
			new PingCommand("hello"),
			_ => throw new ValidationException("invalid"),
			CancellationToken.None);

		return Assert.ThrowsAsync<ValidationException>(act);
	}

	[Fact]
	public async Task Handle_OnSuccess_ReturnsResponseUnchanged()
	{
		var logger = Substitute.For<ILogger<ExceptionHandlingBehavior<PingCommand, PingResult>>>();
		var behavior = new ExceptionHandlingBehavior<PingCommand, PingResult>(logger);

		var result = await behavior.Handle(
			new PingCommand("hello"),
			_ => Task.FromResult(new PingResult("hello")),
			CancellationToken.None);

		Assert.Equal("hello", result.Echo);
	}
}
