using AiDev.Workflow.Api;
using AiDev.Workflow.Api.DependencyInjection;
using AiDev.Workflow.Infrastructure.DependencyInjection;
using Microsoft.Agents.AI.Hosting.AGUI.AspNetCore;

var builder = WebApplication.CreateBuilder(args);

var allowedOrigins = builder.Configuration.GetSection("Cors:AllowedOrigins").Get<string[]>() ?? [];
builder.Services.AddCors(options => options.AddDefaultPolicy(policy => policy
	.WithOrigins(allowedOrigins)
	.AllowAnyHeader()
	.AllowAnyMethod()));

builder.Services.AddApplication();
builder.Services.AddAGUIServer();
var agentBuilder = builder.AddInfrastructure();

var app = builder.Build();

app.UseCors();
app.UseCopilotKitToolResultUnwrapping();

app.MapAGUIServer(agentBuilder, "/");

await app.RunAsync().ConfigureAwait(false);
