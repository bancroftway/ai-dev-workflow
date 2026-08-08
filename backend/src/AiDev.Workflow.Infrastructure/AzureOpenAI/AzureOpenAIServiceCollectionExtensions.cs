using Azure.AI.OpenAI;
using Microsoft.Extensions.AI;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;

namespace AiDev.Workflow.Infrastructure.AzureOpenAI;

public static class AzureOpenAIServiceCollectionExtensions
{
	/// <summary>
	/// Registers an <see cref="AzureOpenAIClient"/> plus one keyed <see cref="IChatClient"/> per
	/// agent deployment ("SpecAgent", "PlanAgent"). Resilience is handled by the Azure SDK's own
	/// pipeline (built-in exponential-backoff retry) rather than a Polly/IHttpClientFactory policy,
	/// since AzureOpenAIClient manages its own request pipeline and isn't registered through
	/// IHttpClientFactory.
	/// </summary>
	public static IServiceCollection AddAzureOpenAI(this IServiceCollection services, IConfiguration configuration)
	{
		services
			.AddOptions<AzureOpenAIOptions>()
			.Bind(configuration.GetSection(AzureOpenAIOptions.SectionName))
			.Validate(o => !string.IsNullOrWhiteSpace(o.Endpoint), $"{AzureOpenAIOptions.SectionName}:Endpoint is required.")
			.Validate(o => !string.IsNullOrWhiteSpace(o.AuthMode), $"{AzureOpenAIOptions.SectionName}:AuthMode is required.")
			.Validate(o => !string.IsNullOrWhiteSpace(o.SpecAgent?.DeploymentName), $"{AzureOpenAIOptions.SectionName}:SpecAgent:DeploymentName is required.")
			.Validate(o => !string.IsNullOrWhiteSpace(o.PlanAgent?.DeploymentName), $"{AzureOpenAIOptions.SectionName}:PlanAgent:DeploymentName is required.")
			.ValidateOnStart();

		services.AddSingleton(sp =>
		{
			var options = sp.GetRequiredService<IOptions<AzureOpenAIOptions>>().Value;

			if (!string.Equals(options.AuthMode, "ApiKey", StringComparison.OrdinalIgnoreCase))
			{
				throw new NotSupportedException(
					$"AzureOpenAI:AuthMode '{options.AuthMode}' is not wired up yet — only 'ApiKey' is supported so far.");
			}

			if (string.IsNullOrWhiteSpace(options.ApiKey))
			{
				throw new InvalidOperationException("AzureOpenAI:ApiKey must be set when AuthMode is 'ApiKey'.");
			}

			var clientOptions = new AzureOpenAIClientOptions();
			return new AzureOpenAIClient(new Uri(options.Endpoint), new System.ClientModel.ApiKeyCredential(options.ApiKey), clientOptions);
		});

		services.AddKeyedSingleton<IChatClient>("SpecAgent", (sp, _) =>
		{
			var options = sp.GetRequiredService<IOptions<AzureOpenAIOptions>>().Value;
			var client = sp.GetRequiredService<AzureOpenAIClient>();
			return client.GetChatClient(options.SpecAgent.DeploymentName).AsIChatClient();
		});

		services.AddKeyedSingleton<IChatClient>("PlanAgent", (sp, _) =>
		{
			var options = sp.GetRequiredService<IOptions<AzureOpenAIOptions>>().Value;
			var client = sp.GetRequiredService<AzureOpenAIClient>();
			return client.GetChatClient(options.PlanAgent.DeploymentName).AsIChatClient();
		});

		return services;
	}
}
