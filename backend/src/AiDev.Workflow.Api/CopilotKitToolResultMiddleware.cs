using System.Text;
using System.Text.Json.Nodes;

namespace AiDev.Workflow.Api;

/// <summary>
/// Fixes up CopilotKit's AG-UI requests to match what MAF's AG-UI hosting layer (WorkflowSession +
/// AGUIChatMessageExtensions) actually expects for a RequestPort gate-response resume, in two ways:
///
/// 1. Envelope unwrapping. CopilotKit's HumanInTheLoopToolCall.respond(value) wraps whatever value is
///    passed in its own `{ toolCallId, toolName, result: value }` envelope before JSON-stringifying it
///    as the "tool" role message's `content` field. MAF threads `content` straight through as a raw
///    string into FunctionResultContent.Result with no deserialization step (confirmed by decompiling
///    AGUIChatMessageExtensions.AsChatMessages), so it needs to see the plain GateReviewResponse JSON,
///    not CopilotKit's wrapper (the RequestPort's response type is `string` server-side for exactly
///    this reason — see WorkflowPorts.cs).
///
/// 2. History trimming. CopilotKit always resends the full message history (the original user message,
///    the assistant's tool-call message, then the new tool-response message) on every turn, since its
///    client is designed to be stateless. Our backend is not stateless — WithInMemorySessionStore
///    already persists the full ChatHistoryProvider server-side per threadId. Replaying the old
///    user/assistant messages alongside the new tool response makes WorkflowSession treat them as a
///    second batch of "regular" input (via TrySendMessageAsync) delivered in the very same call that
///    resumes the paused RequestPort (via SendResponseAsync) — confirmed live: the run silently
///    completes (RUN_STARTED immediately followed by RUN_FINISHED, no new agent turn) instead of
///    resuming the workflow, with no exception anywhere. Trimming the resend down to just the tool
///    response message(s) is what a stateful server actually needs.
/// </summary>
internal static class CopilotKitToolResultMiddleware
{
	public static IApplicationBuilder UseCopilotKitToolResultUnwrapping(this IApplicationBuilder app) =>
		app.Use(async (context, next) =>
		{
			if (HttpMethods.IsPost(context.Request.Method))
			{
				await RewriteToolMessagesAsync(context.Request).ConfigureAwait(false);
			}

			await next().ConfigureAwait(false);
		});

	private static async Task RewriteToolMessagesAsync(HttpRequest request)
	{
		request.EnableBuffering();
		string body;
		using (var reader = new StreamReader(request.Body, Encoding.UTF8, leaveOpen: true))
		{
			body = await reader.ReadToEndAsync().ConfigureAwait(false);
		}

		request.Body.Position = 0;

		if (!body.Contains("\"role\":\"tool\"", StringComparison.Ordinal))
		{
			return;
		}

		JsonObject? root;
		try
		{
			root = JsonNode.Parse(body) as JsonObject;
		}
		catch (System.Text.Json.JsonException)
		{
			return;
		}

		if (root?["messages"] is not JsonArray messages)
		{
			return;
		}

		var toolMessages = new JsonArray();
		foreach (var message in messages.OfType<JsonObject>())
		{
			if (message["role"] is not JsonValue roleValue || roleValue.GetValueKind() != System.Text.Json.JsonValueKind.String
				|| !string.Equals(roleValue.GetValue<string>(), "tool", StringComparison.Ordinal))
			{
				continue;
			}

			if (message["content"] is JsonValue contentValue
				&& contentValue.TryGetValue<string>(out var content)
				&& TryUnwrapCopilotKitToolResult(content, out var unwrapped))
			{
				message["content"] = unwrapped;
			}

			toolMessages.Add(message.DeepClone());
		}

		if (toolMessages.Count == 0)
		{
			return;
		}

		root["messages"] = toolMessages;

		var newBodyBytes = Encoding.UTF8.GetBytes(root.ToJsonString());
		request.Body = new MemoryStream(newBodyBytes);
		request.ContentLength = newBodyBytes.Length;
	}

	private static bool TryUnwrapCopilotKitToolResult(string content, out string unwrapped)
	{
		unwrapped = content;

		JsonNode? parsed;
		try
		{
			parsed = JsonNode.Parse(content);
		}
		catch (System.Text.Json.JsonException)
		{
			return false;
		}

		// CopilotKit's own envelope: { toolCallId, toolName, result }. Only unwrap when it's
		// unambiguously that shape, not just any object that happens to have a "result" key.
		if (parsed is not JsonObject obj || obj["result"] is not { } result
			|| obj["toolCallId"] is null || obj["toolName"] is null)
		{
			return false;
		}

		unwrapped = result.ToJsonString();
		return true;
	}
}
