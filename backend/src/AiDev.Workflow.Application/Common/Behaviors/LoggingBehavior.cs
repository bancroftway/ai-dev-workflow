using System.Diagnostics;
using FreeMediator;
using Microsoft.Extensions.Logging;

namespace AiDev.Workflow.Application.Common.Behaviors;

public sealed partial class LoggingBehavior<TRequest, TResponse>(ILogger<LoggingBehavior<TRequest, TResponse>> logger)
	: IPipelineBehavior<TRequest, TResponse>
	where TRequest : FreeMediator.Internals.IBaseRequest
{
	public async Task<TResponse> Handle(TRequest request, RequestHandlerDelegate<TResponse> next, CancellationToken cancellationToken)
	{
		var requestName = typeof(TRequest).Name;
		var stopwatch = Stopwatch.StartNew();

		LogHandling(requestName);
		try
		{
			var response = await next(cancellationToken).ConfigureAwait(false);
			LogHandled(requestName, stopwatch.ElapsedMilliseconds);
			return response;
		}
// Intentional log-then-bare-rethrow: a timing signal distinct from
		// ExceptionHandlingBehavior's error-normalization log; must not wrap the exception so that
		// behavior's type-check further up the pipeline still sees the original exception type.
#pragma warning disable S2139
		catch (Exception ex)
		{
			LogFailed(ex, requestName, stopwatch.ElapsedMilliseconds);
			throw;
		}
#pragma warning restore S2139
	}

	[LoggerMessage(Level = LogLevel.Information, Message = "Handling {RequestName}")]
	private partial void LogHandling(string requestName);

	[LoggerMessage(Level = LogLevel.Information, Message = "Handled {RequestName} in {ElapsedMs}ms")]
	private partial void LogHandled(string requestName, long elapsedMs);

	[LoggerMessage(Level = LogLevel.Warning, Message = "Failed {RequestName} after {ElapsedMs}ms")]
	private partial void LogFailed(Exception exception, string requestName, long elapsedMs);
}
