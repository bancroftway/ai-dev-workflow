using AiDev.Workflow.Application.Common.Behaviors;
using AiDev.Workflow.Application.Diagnostics.Ping;
using FluentValidation;
using FreeMediator;

namespace AiDev.Workflow.Api.DependencyInjection;

public static class AddApplicationExtensions
{
	public static IServiceCollection AddApplication(this IServiceCollection services)
	{
		services.AddMediator(config =>
		{
			config.RegisterServicesFromAssemblyContaining<PingCommand>();
		});

		services.AddValidatorsFromAssemblyContaining<PingValidator>();

		services.AddTransient(typeof(IPipelineBehavior<,>), typeof(ValidationBehavior<,>));
		services.AddTransient(typeof(IPipelineBehavior<,>), typeof(LoggingBehavior<,>));
		services.AddTransient(typeof(IPipelineBehavior<,>), typeof(ExceptionHandlingBehavior<,>));

		return services;
	}
}
