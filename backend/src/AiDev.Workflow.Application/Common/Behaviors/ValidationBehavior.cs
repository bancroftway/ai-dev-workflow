using FluentValidation;
using FreeMediator;

namespace AiDev.Workflow.Application.Common.Behaviors;

public sealed class ValidationBehavior<TRequest, TResponse>(IEnumerable<IValidator<TRequest>> validators)
	: IPipelineBehavior<TRequest, TResponse>
	where TRequest : FreeMediator.Internals.IBaseRequest
{
	public async Task<TResponse> Handle(TRequest request, RequestHandlerDelegate<TResponse> next, CancellationToken cancellationToken)
	{
		if (validators.Any())
		{
			var context = new ValidationContext<TRequest>(request);
			var failures = (await Task.WhenAll(validators.Select(v => v.ValidateAsync(context, cancellationToken))).ConfigureAwait(false))
				.SelectMany(result => result.Errors)
				.Where(failure => failure is not null)
				.ToList();

			if (failures.Count != 0)
			{
				throw new ValidationException(failures);
			}
		}

		return await next(cancellationToken).ConfigureAwait(false);
	}
}
