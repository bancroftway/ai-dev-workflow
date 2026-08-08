using AiDev.Workflow.Application.Common.Behaviors;
using AiDev.Workflow.Application.Diagnostics.Ping;
using Microsoft.Extensions.Logging;
using NSubstitute;

namespace AiDev.Workflow.UnitTests.Behaviors;

public class LoggingBehaviorTests
{
	[Fact]
	public async Task Handle_OnSuccess_LogsStartAndCompletion()
	{
		var logger = Substitute.For<ILogger<LoggingBehavior<PingCommand, PingResult>>>();
		logger.IsEnabled(Arg.Any<LogLevel>()).Returns(true);
		var behavior = new LoggingBehavior<PingCommand, PingResult>(logger);

		var result = await behavior.Handle(
			new PingCommand("hello"),
			_ => Task.FromResult(new PingResult("hello")),
			CancellationToken.None);

		Assert.Equal("hello", result.Echo);
		Assert.Equal(2, CountLogCalls(logger, LogLevel.Information));
	}

	[Fact]
	public async Task Handle_WhenNextThrows_LogsWarningAndRethrows()
	{
		var logger = Substitute.For<ILogger<LoggingBehavior<PingCommand, PingResult>>>();
		logger.IsEnabled(Arg.Any<LogLevel>()).Returns(true);
		var behavior = new LoggingBehavior<PingCommand, PingResult>(logger);

		var act = () => behavior.Handle(
			new PingCommand("hello"),
			_ => throw new InvalidOperationException("boom"),
			CancellationToken.None);

		await Assert.ThrowsAsync<InvalidOperationException>(act);
		Assert.Equal(1, CountLogCalls(logger, LogLevel.Warning));
	}

	/// <summary>
	/// [LoggerMessage] source-generated logging calls ILogger.Log&lt;TState&gt; with a compiler-generated
	/// TState struct, not `object` — so NSubstitute's strongly-typed `Received().Log(...)` (which infers
	/// TState from the Arg.Any&lt;T&gt; matchers) never matches. Inspecting ReceivedCalls' raw arguments
	/// (LogLevel is always the first, non-generic argument) works regardless of the generated TState type.
	/// </summary>
	private static int CountLogCalls(ILogger logger, LogLevel level) =>
		logger.ReceivedCalls().Count(call => string.Equals(call.GetMethodInfo().Name, nameof(ILogger.Log), StringComparison.Ordinal)
			&& call.GetArguments() is [LogLevel callLevel, ..] && callLevel == level);
}
