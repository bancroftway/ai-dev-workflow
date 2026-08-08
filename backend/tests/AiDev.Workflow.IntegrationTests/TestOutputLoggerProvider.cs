using Microsoft.Extensions.Logging;
using Xunit.Abstractions;

namespace AiDev.Workflow.IntegrationTests;

internal sealed class TestOutputLoggerProvider(ITestOutputHelper output) : ILoggerProvider
{
	public ILogger CreateLogger(string categoryName) => new TestOutputLogger(output, categoryName);

	public void Dispose()
	{
	}

	private sealed class TestOutputLogger(ITestOutputHelper output, string category) : ILogger
	{
		public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;

		public bool IsEnabled(LogLevel logLevel) => true;

		public void Log<TState>(LogLevel logLevel, EventId eventId, TState state, Exception? exception, Func<TState, Exception?, string> formatter)
		{
			try
			{
				output.WriteLine($"[{logLevel}] {category}: {formatter(state, exception)}");
				if (exception != null)
				{
					output.WriteLine($"    EXCEPTION: {exception}");
				}
			}
			catch
			{
				// xunit output may be unavailable after test completion
			}
		}
	}
}
