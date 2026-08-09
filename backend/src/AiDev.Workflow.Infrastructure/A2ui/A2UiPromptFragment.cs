namespace AiDev.Workflow.Infrastructure.A2ui;

/// <summary>
/// Shared system-prompt fragment teaching an agent how to author its own `a2ui` field: the real
/// A2UI v0.9.1 envelope shape and Basic Catalog vocabulary (verified against
/// github.com/a2ui-project/a2ui specification/v0_9_1/docs/a2ui_protocol.md), not an invented one.
/// This is protocol instruction, not UI authorship — the agent still decides everything about what
/// gets rendered.
/// </summary>
public static class A2UiPromptFragment
{
	private const string Template =
		"""
		## Authoring the `a2ui` field

		Populate `a2ui` with a list of A2UI v0.9.1 envelope messages that render your output above as a
		UI surface. Every envelope is a JSON object of the form:
		`{"version": "v0.9.1", "<messageType>": {...}}` where `<messageType>` is exactly one of
		`createSurface`, `updateComponents`, `updateDataModel`.

		Always target `surfaceId` = "__SURFACE_ID__". On every turn (including revisions), emit a fresh,
		complete snapshot for this surface: one `createSurface`, then one `updateComponents` containing
		the FULL component tree (not a partial diff), then optionally one `updateDataModel`. A duplicate
		`createSurface` for an existing surface is safely deduped downstream, so always include it.

		`createSurface` properties: `surfaceId`, `catalogId` (always "__CATALOG_ID__").

		`updateComponents` properties: `surfaceId`, `components` (a flat array of component objects —
		relationships are expressed via `id` references, not nesting). Exactly one component in the
		array must have `id` = "root". Each component object has `id` (string, unique within the
		surface), `component` (the type name, must be one of the components listed below), and
		type-specific properties. Container components reference children by id: `Row`/`Column` use a
		`children` array of ids (or a single `child` for one child); `Card`/`Modal`/`Button` use a single
		`child` id.

		Only use these Basic Catalog component types: Text, Image, Icon, Video, AudioPlayer, Row,
		Column, List, Card, Tabs, Divider, Modal, Button, CheckBox, TextField, DateTimeInput,
		ChoicePicker, Slider. Any other component name will fail validation.

		Data binding: a property can be a literal value, or `{"path": "/some/path"}` to bind to the
		surface's data model (JSON Pointer). This surface is a read-only display of your output's
		content — do not include input components (`TextField`, `ChoicePicker`, `CheckBox`, etc.), data
		bindings, or clarifying questions here. Clarifying questions are shown to the human elsewhere,
		answered by editing their requirements text directly, not through this surface.

		Do NOT include Approve/Continue/Submit buttons or any action that finalizes the human's
		decision — that control lives outside this surface and is added by the host application, not
		by you.

		Example envelope pair (a card showing a title, a summary, and a couple of content sections):
		```json
		{"version":"v0.9.1","createSurface":{"surfaceId":"__SURFACE_ID__","catalogId":"__CATALOG_ID__"}}
		{"version":"v0.9.1","updateComponents":{"surfaceId":"__SURFACE_ID__","components":[
		  {"id":"root","component":"Card","child":"body"},
		  {"id":"body","component":"Column","children":["title_text","summary_text","section1_title","section1_text"]},
		  {"id":"title_text","component":"Text","text":"Support Ticket Triage Tool","variant":"h2"},
		  {"id":"summary_text","component":"Text","text":"A tool for..."},
		  {"id":"section1_title","component":"Text","text":"Overview","variant":"h3"},
		  {"id":"section1_text","component":"Text","text":"..."}
		]}}
		```
		""";

	public static string Build(string surfaceId) =>
		Template.Replace("__SURFACE_ID__", surfaceId).Replace("__CATALOG_ID__", BasicCatalogIds.CatalogId);
}
