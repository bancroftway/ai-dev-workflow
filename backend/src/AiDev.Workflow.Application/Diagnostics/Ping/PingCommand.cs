using FreeMediator;

namespace AiDev.Workflow.Application.Diagnostics.Ping;

public sealed record PingCommand(string Message) : IRequest<PingResult>;
