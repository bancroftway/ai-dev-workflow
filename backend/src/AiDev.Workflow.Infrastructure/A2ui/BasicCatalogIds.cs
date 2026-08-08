namespace AiDev.Workflow.Infrastructure.A2ui;

/// <summary>
/// Constants for the A2UI v0.9.1 Basic Catalog. The component/envelope allow-lists that used to
/// live here were removed — they were a hand-maintained duplicate of what
/// A2ui/Spec/v0_9_1/catalog.json and server_to_client.json already encode authoritatively, and
/// A2UiSchemaValidator now validates directly against those real schemas (see A2UiSpecSchemas)
/// instead of a parallel hand-rolled list that can drift out of sync with the spec, which is
/// exactly how the CatalogId bug below happened in the first place.
/// </summary>
public static class BasicCatalogIds
{
	// The v0.9.1 catalog.json's own "$id"/"catalogId" fields both use "v0_9" in the path, NOT
	// "v0_9_1" — the v0.9.1 patch release deliberately reuses the v0.9 catalog identity since the
	// catalog itself didn't change. Verified directly against
	// specification/v0_9_1/catalogs/basic/catalog.json in the repo, not the (inconsistent) prose
	// example in a2ui_protocol.md, which uses v0_9_1 in this URL.
	public const string CatalogId = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json";
	public const string ProtocolVersion = "v0.9.1";
}
