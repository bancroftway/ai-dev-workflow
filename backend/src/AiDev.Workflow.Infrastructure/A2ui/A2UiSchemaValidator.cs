using System.Text.Json;
using System.Text.Json.Nodes;
using Json.Schema;

namespace AiDev.Workflow.Infrastructure.A2ui;

/// <summary>
/// Validates LLM-authored A2UI v0.9.1 envelope messages against the real, vendored protocol JSON
/// Schemas (see A2UiSpecSchemas) rather than a hand-rolled allow-list — this is a safety net, not a
/// UI generator; see SpecLlmOutput for why the shape guarantee from structured-output JSON Schema
/// stops short of a content guarantee. The only check the real schema can't express is the
/// app-specific "surfaceId must equal the surface this turn is allowed to target" rule, which is
/// still applied here as a supplementary check.
/// </summary>
public static class A2UiSchemaValidator
{
	private static readonly EvaluationOptions Options = new() { OutputFormat = OutputFormat.List };

	public static A2UiValidationResult Validate(IReadOnlyList<JsonObject> envelopes, string expectedSurfaceId)
	{
		var errors = new List<string>();

		for (var i = 0; i < envelopes.Count; i++)
		{
			ValidateEnvelope(envelopes[i], i, expectedSurfaceId, errors);
		}

		return errors.Count == 0 ? A2UiValidationResult.Valid : A2UiValidationResult.Invalid(errors);
	}

	private static void ValidateEnvelope(JsonObject envelope, int index, string expectedSurfaceId, List<string> errors)
	{
		var element = JsonSerializer.SerializeToElement(envelope);
		var result = A2UiSpecSchemas.Envelope.Evaluate(element, Options);
		if (!result.IsValid)
		{
			errors.AddRange(CollectLeafErrors(result).Distinct(StringComparer.Ordinal).Take(10).Select(e => $"[{index}] {e}"));
		}

		var surfaceId = FindSurfaceId(envelope);
		if (surfaceId is not null && !string.Equals(surfaceId, expectedSurfaceId, StringComparison.Ordinal))
		{
			errors.Add($"[{index}] surfaceId was '{surfaceId}', expected '{expectedSurfaceId}'");
		}
	}

	private static string? FindSurfaceId(JsonObject envelope) =>
		envelope.Select(kv => kv.Value).OfType<JsonObject>()
			.Select(payload => payload["surfaceId"] is JsonValue v && v.TryGetValue(out string? s) ? s : null)
			.FirstOrDefault(s => s is not null);

	private static IEnumerable<string> CollectLeafErrors(EvaluationResults results)
	{
		var details = results.Details;
		if ((details is null || details.Count == 0) && results.Errors is { Count: > 0 })
		{
			foreach (var (keyword, message) in results.Errors)
			{
				yield return $"{results.InstanceLocation}: {keyword}: {message}";
			}
		}

		foreach (var child in details ?? [])
		{
			foreach (var error in CollectLeafErrors(child))
			{
				yield return error;
			}
		}
	}

	/// <summary>
	/// A minimal, generic, feature-agnostic fallback surface used when the model's A2ui output
	/// fails validation and one corrective re-prompt also fails. Still A2UI, still schema-valid —
	/// not hand-authored feature UI. Relies on SessionStateProjectionExecutor to suppress a
	/// duplicate createSurface if this surface already exists from a prior turn.
	/// </summary>
	public static IReadOnlyList<JsonObject> BuildFallback(string surfaceId, string message) =>
	[
		new JsonObject
		{
			["version"] = BasicCatalogIds.ProtocolVersion,
			["createSurface"] = new JsonObject
			{
				["surfaceId"] = surfaceId,
				["catalogId"] = BasicCatalogIds.CatalogId,
			},
		},
		new JsonObject
		{
			["version"] = BasicCatalogIds.ProtocolVersion,
			["updateComponents"] = new JsonObject
			{
				["surfaceId"] = surfaceId,
				["components"] = new JsonArray
				{
					new JsonObject { ["id"] = "root", ["component"] = "Card", ["child"] = "fallback_text" },
					new JsonObject { ["id"] = "fallback_text", ["component"] = "Text", ["text"] = message },
				},
			},
		},
	];
}
