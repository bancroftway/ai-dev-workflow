using System.Reflection;
using Json.Schema;

namespace AiDev.Workflow.Infrastructure.A2ui;

/// <summary>
/// Loads the real, vendored A2UI v0.9.1 JSON Schemas (server_to_client.json, common_types.json,
/// catalog.json — pinned copies of specification/v0_9_1/{json,catalogs/basic} from
/// github.com/a2ui-project/a2ui, embedded as resources under A2ui/Spec/v0_9_1/) and registers them
/// for $ref resolution, so A2UiSchemaValidator validates against the actual protocol schemas
/// instead of a hand-rolled allow-list. A hand-rolled validator can't feasibly reproduce this: the
/// schemas encode per-component required/forbidden properties via 44 `unevaluatedProperties: false`
/// clauses across 18 component types, plus the envelope's `additionalProperties: false` and
/// `version` enum — the kind of detail that's easy to miss by hand and was in fact missed
/// (BasicCatalogIds' old hand-maintained allow-list caught unknown component names but not
/// hallucinated properties, missing required fields, or a missing/malformed `version`).
/// </summary>
internal static class A2UiSpecSchemas
{
	// server_to_client.json refs "catalog.json#/..." — a RELATIVE ref resolved against its own $id
	// (".../v0_9/server_to_client.json"), giving ".../v0_9/catalog.json". catalog.json's own $id is
	// instead ".../v0_9/catalogs/basic/catalog.json" (it lives in a subdirectory on disk). These two
	// URIs don't match, so without registering this alias the $ref fails to resolve. This is a fixed
	// identifier mandated by the external A2UI protocol schema itself, not a configurable path.
#pragma warning disable S1075
	private const string CatalogRefAliasUri = "https://a2ui.org/specification/v0_9/catalog.json";
#pragma warning restore S1075

	public static readonly JsonSchema Envelope = Load();

	private static JsonSchema Load()
	{
		var assembly = typeof(A2UiSpecSchemas).Assembly;
		var serverToClient = LoadSchema(assembly, "server_to_client.json");
		var commonTypes = LoadSchema(assembly, "common_types.json");
		var catalog = LoadSchema(assembly, "catalog.json");

		SchemaRegistry.Global.Register(serverToClient);
		SchemaRegistry.Global.Register(commonTypes);
		SchemaRegistry.Global.Register(catalog);
		SchemaRegistry.Global.Register(new Uri(CatalogRefAliasUri), catalog);

		return serverToClient;
	}

	private static JsonSchema LoadSchema(Assembly assembly, string fileName)
	{
		var resourceName = $"{assembly.GetName().Name}.A2ui.Spec.v0_9_1.{fileName}";
		using var stream = assembly.GetManifestResourceStream(resourceName)
			?? throw new InvalidOperationException($"Embedded A2UI spec resource '{resourceName}' not found.");
		using var reader = new StreamReader(stream);
		return JsonSchema.FromText(reader.ReadToEnd());
	}
}
