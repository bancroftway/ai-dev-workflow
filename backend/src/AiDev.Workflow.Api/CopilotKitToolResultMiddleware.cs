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
/// 2. History trimming. CopilotKit always resends the full message history (every user/assistant
///    message plus every "tool" role resolution ever sent, including ones from *earlier* Continue
///    rounds already consumed by a prior request) on every turn, since its client is designed to be
///    stateless. Our backend is not stateless — WithInMemorySessionStore already persists the full
///    ChatHistoryProvider server-side per threadId. Two failure modes if the resend isn't trimmed to
///    exactly the one new resolution:
///    - Replaying old user/assistant messages alongside a tool response makes WorkflowSession treat
///      them as a second batch of "regular" input (via TrySendMessageAsync) delivered in the same
///      call that resumes the paused RequestPort (via SendResponseAsync) — confirmed live: the run
///      silently completes (RUN_STARTED immediately followed by RUN_FINISHED, no new agent turn)
///      instead of resuming, with no exception anywhere.
///    - Replaying an *already-resolved* earlier "tool" message (e.g. round 1's Continue, still present
///      in history once round 2 also resolves and round 3 is submitted) hits the same failure mode:
///      its pending request was already removed from the port's tracking after being consumed, so it
///      no longer matches anything and falls into the same "regular input" bucket as above — confirmed
///      live via a multi-round Continue sequence silently stalling on the third round. Keeping only the
///      single most recent "tool" message is what a stateful server, resumed exactly once per request,
///      actually needs.
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

		if (!TryParseToolMessages(body, out var root, out var messages))
		{
			return;
		}

		JsonObject? lastToolMessage = null;
		foreach (var message in messages.OfType<JsonObject>())
		{
			if (message["role"] is not JsonValue roleValue || roleValue.GetValueKind() != System.Text.Json.JsonValueKind.String
				|| !string.Equals(roleValue.GetValue<string>(), "tool", StringComparison.Ordinal))
			{
				continue;
			}

			lastToolMessage = message;
		}

		if (lastToolMessage is null)
		{
			return;
		}

		if (lastToolMessage["content"] is JsonValue lastContentValue
			&& lastContentValue.TryGetValue<string>(out var lastContent)
			&& TryUnwrapCopilotKitToolResult(lastContent, out var lastUnwrapped))
		{
			lastToolMessage["content"] = lastUnwrapped;
		}

		root["messages"] = new JsonArray(lastToolMessage.DeepClone());

		var newBodyBytes = Encoding.UTF8.GetBytes(root.ToJsonString());
		request.Body = new MemoryStream(newBodyBytes);
		request.ContentLength = newBodyBytes.Length;
	}

	private static bool TryParseToolMessages(
		string body,
		[System.Diagnostics.CodeAnalysis.NotNullWhen(true)] out JsonObject? root,
		[System.Diagnostics.CodeAnalysis.NotNullWhen(true)] out JsonArray? messages)
	{
		root = null;
		messages = null;

		if (!body.Contains("\"role\":\"tool\"", StringComparison.Ordinal))
		{
			return false;
		}

		JsonObject? parsedRoot;
		try
		{
			parsedRoot = JsonNode.Parse(body) as JsonObject;
		}
		catch (System.Text.Json.JsonException)
		{
			return false;
		}

		if (parsedRoot?["messages"] is not JsonArray parsedMessages)
		{
			return false;
		}

		root = parsedRoot;
		messages = parsedMessages;
		return true;
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
