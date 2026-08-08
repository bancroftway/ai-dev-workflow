using AiDev.Workflow.Application.Common.Exceptions;
using FluentValidation;
using FreeMediator;
using Microsoft.Extensions.Logging;

namespace AiDev.Workflow.Application.Common.Behaviors;

public sealed partial class ExceptionHandlingBehavior<TRequest, TResponse>(ILogger<ExceptionHandlingBehavior<TRequest, TResponse>> logger)
	: IPipelineBehavior<TRequest, TResponse>
	where TRequest : FreeMediator.Internals.IBaseRequest
{
	public async Task<TResponse> Handle(TRequest request, RequestHandlerDelegate<TResponse> next, CancellationToken cancellationToken)
	{
		try
		{
			return await next(cancellationToken).ConfigureAwait(false);
		}
		catch (ValidationException)
		{
			// Validation failures are already a well-shaped, expected exception type — let them pass through.
			throw;
		}
		catch (Exception ex)
		{
			var requestName = typeof(TRequest).Name;
			LogUnhandledException(ex, requestName);
			throw new AppException($"Unhandled exception while handling {requestName}.", ex);
		}
	}

	[LoggerMessage(Level = LogLevel.Error, Message = "Unhandled exception while handling {RequestName}")]
	private partial void LogUnhandledException(Exception exception, string requestName);
}
