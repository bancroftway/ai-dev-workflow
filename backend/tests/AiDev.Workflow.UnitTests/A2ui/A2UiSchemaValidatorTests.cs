using System.Text.Json.Nodes;
using AiDev.Workflow.Infrastructure.A2ui;

namespace AiDev.Workflow.UnitTests.A2ui;

public class A2UiSchemaValidatorTests
{
	private const string SurfaceId = "spec-content";

	[Fact]
	public void Validate_WellFormedEnvelopes_PassesUnchanged()
	{
		var envelopes = Envelopes(
			"""{"version":"v0.9.1","createSurface":{"surfaceId":"__SURFACE__","catalogId":"__CATALOG__"}}"""
				.Replace("__SURFACE__", SurfaceId).Replace("__CATALOG__", BasicCatalogIds.CatalogId),
			"""
			{"version":"v0.9.1","updateComponents":{"surfaceId":"__SURFACE__","components":[
			  {"id":"root","component":"Card","child":"text"},
			  {"id":"text","component":"Text","text":"hello"}
			]}}
			""".Replace("__SURFACE__", SurfaceId));

		var result = A2UiSchemaValidator.Validate(envelopes, SurfaceId);

		Assert.True(result.IsValid);
		Assert.Empty(result.Errors);
	}

	[Fact]
	public void Validate_UnknownComponentType_FailsValidation()
	{
		var envelopes = Envelopes(
			"""
			{"version":"v0.9.1","updateComponents":{"surfaceId":"spec-content","components":[
			  {"id":"root","component":"NotARealComponent"}
			]}}
			""");

		var result = A2UiSchemaValidator.Validate(envelopes, SurfaceId);

		Assert.False(result.IsValid);
	}

	[Fact]
	public void Validate_WrongSurfaceId_FailsValidation()
	{
		var envelopes = Envelopes(
			"""{"version":"v0.9.1","createSurface":{"surfaceId":"some-other-surface","catalogId":"__CATALOG__"}}"""
				.Replace("__CATALOG__", BasicCatalogIds.CatalogId));

		var result = A2UiSchemaValidator.Validate(envelopes, SurfaceId);

		Assert.False(result.IsValid);
		Assert.Contains(result.Errors, e => e.Contains("some-other-surface", StringComparison.Ordinal));
	}

	[Fact]
	public void Validate_EnvelopeWithNoRecognizedMessageKey_FailsValidation()
	{
		var envelopes = Envelopes("""{"version":"v0.9.1","notARealMessageType":{}}""");

		var result = A2UiSchemaValidator.Validate(envelopes, SurfaceId);

		Assert.False(result.IsValid);
	}

	[Fact]
	public void Validate_MissingComponentId_FailsValidation()
	{
		var envelopes = Envelopes(
			"""
			{"version":"v0.9.1","updateComponents":{"surfaceId":"spec-content","components":[
			  {"component":"Text","text":"no id here"}
			]}}
			""");

		var result = A2UiSchemaValidator.Validate(envelopes, SurfaceId);

		Assert.False(result.IsValid);
	}

	[Fact]
	public void Validate_MissingVersionField_FailsValidation()
	{
		var envelopes = Envelopes(
			"""{"createSurface":{"surfaceId":"__SURFACE__","catalogId":"__CATALOG__"}}"""
				.Replace("__SURFACE__", SurfaceId).Replace("__CATALOG__", BasicCatalogIds.CatalogId));

		var result = A2UiSchemaValidator.Validate(envelopes, SurfaceId);

		Assert.False(result.IsValid);
	}

	[Fact]
	public void Validate_HallucinatedComponentProperty_FailsValidation()
	{
		// "txet" is a typo/hallucination of "text" — real spec has additionalProperties:false /
		// unevaluatedProperties:false throughout, so this must be rejected, not silently accepted.
		var envelopes = Envelopes(
			"""
			{"version":"v0.9.1","updateComponents":{"surfaceId":"spec-content","components":[
			  {"id":"root","component":"Text","text":"hi","txet":"oops"}
			]}}
			""");

		var result = A2UiSchemaValidator.Validate(envelopes, SurfaceId);

		Assert.False(result.IsValid);
	}

	[Fact]
	public void Validate_CardMissingRequiredChild_FailsValidation()
	{
		var envelopes = Envelopes(
			"""
			{"version":"v0.9.1","updateComponents":{"surfaceId":"spec-content","components":[
			  {"id":"root","component":"Card"}
			]}}
			""");

		var result = A2UiSchemaValidator.Validate(envelopes, SurfaceId);

		Assert.False(result.IsValid);
	}

	[Fact]
	public void Validate_EmptyComponentsArray_FailsValidation()
	{
		var envelopes = Envelopes(
			"""{"version":"v0.9.1","updateComponents":{"surfaceId":"spec-content","components":[]}}""");

		var result = A2UiSchemaValidator.Validate(envelopes, SurfaceId);

		Assert.False(result.IsValid);
	}

	[Fact]
	public void BuildFallback_ProducesValidEnvelopesForTheSameSurface()
	{
		var fallback = A2UiSchemaValidator.BuildFallback(SurfaceId, "Something went wrong.");

		var result = A2UiSchemaValidator.Validate(fallback, SurfaceId);

		Assert.True(result.IsValid);
	}

	private static List<JsonObject> Envelopes(params string[] json) =>
		json.Select(j => JsonNode.Parse(j)?.AsObject() ?? throw new InvalidOperationException($"Test fixture JSON did not parse: {j}")).ToList();
}
