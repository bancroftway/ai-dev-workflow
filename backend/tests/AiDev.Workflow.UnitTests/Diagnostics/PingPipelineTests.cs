using AiDev.Workflow.Application.Common.Behaviors;
using AiDev.Workflow.Application.Diagnostics.Ping;
using FluentValidation;
using FreeMediator;
using Microsoft.Extensions.DependencyInjection;

namespace AiDev.Workflow.UnitTests.Diagnostics;

public class PingPipelineTests
{
	private static ServiceProvider BuildProvider()
	{
		var services = new ServiceCollection();
		services.AddLogging();
		services.AddMediator(config => config.RegisterServicesFromAssemblyContaining<PingCommand>());
		services.AddValidatorsFromAssemblyContaining<PingValidator>();
		services.AddTransient(typeof(IPipelineBehavior<,>), typeof(ValidationBehavior<,>));
		services.AddTransient(typeof(IPipelineBehavior<,>), typeof(LoggingBehavior<,>));
		services.AddTransient(typeof(IPipelineBehavior<,>), typeof(ExceptionHandlingBehavior<,>));
		return services.BuildServiceProvider();
	}

	[Fact]
	public async Task Send_WithValidMessage_ReturnsEchoedResult()
	{
		using var provider = BuildProvider();
		var sender = provider.GetRequiredService<ISender>();

		var result = await sender.Send(new PingCommand("hello world"), CancellationToken.None);

		Assert.Equal("hello world", result.Echo);
	}

	[Fact]
	public async Task Send_WithEmptyMessage_ShortCircuitsViaValidationBehavior()
	{
		using var provider = BuildProvider();
		var sender = provider.GetRequiredService<ISender>();

		await Assert.ThrowsAsync<ValidationException>(
			() => sender.Send(new PingCommand(""), CancellationToken.None));
	}
}
