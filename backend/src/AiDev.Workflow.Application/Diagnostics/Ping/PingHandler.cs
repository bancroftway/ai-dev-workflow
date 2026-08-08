using FreeMediator;

namespace AiDev.Workflow.Application.Diagnostics.Ping;

public sealed class PingHandler : IRequestHandler<PingCommand, PingResult>
{
	public Task<PingResult> Handle(PingCommand request, CancellationToken cancellationToken)
	{
		return Task.FromResult(new PingResult(request.Message));
	}
}
