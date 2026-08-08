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
		surface's data model (JSON Pointer). Input components (`TextField`, `ChoicePicker`, `CheckBox`,
		etc.) use this to read/write user input. Bind each clarifying question's answer to
		`/answers/<questionId>` so a human can draft an answer in place before submitting. Use
		`TextField` for an open-ended question; use `ChoicePicker` only when the question has discrete
		choices. `ChoicePicker` REQUIRES both `options` (array of `{"label":..., "value":...}`) and
		`value` — and `value` binds to an ARRAY in the data model (one element for a single-select
		question with `variant: "mutuallyExclusive"`, e.g. `["optionValue"]`), never a bare string.

		Do NOT include Approve/Continue/Submit buttons or any action that finalizes the human's
		decision — that control lives outside this surface, in the review sidebar, and is added by the
		host application, not by you.

		Example envelope pair (a card showing a title, a summary, and two clarifying questions — one
		open-ended with a bound TextField, one multiple-choice with a bound ChoicePicker):
		```json
		{"version":"v0.9.1","createSurface":{"surfaceId":"__SURFACE_ID__","catalogId":"__CATALOG_ID__"}}
		{"version":"v0.9.1","updateComponents":{"surfaceId":"__SURFACE_ID__","components":[
		  {"id":"root","component":"Card","child":"body"},
		  {"id":"body","component":"Column","children":["title_text","summary_text","q1_label","q1_field","q2_label","q2_field"]},
		  {"id":"title_text","component":"Text","text":"Support Ticket Triage Tool","variant":"h2"},
		  {"id":"summary_text","component":"Text","text":"A tool for..."},
		  {"id":"q1_label","component":"Text","text":"What should the tool be called?","variant":"caption"},
		  {"id":"q1_field","component":"TextField","label":"Your answer","value":{"path":"/answers/q1"}},
		  {"id":"q2_label","component":"Text","text":"Should urgency be configurable by admins?","variant":"caption"},
		  {"id":"q2_field","component":"ChoicePicker","variant":"mutuallyExclusive","options":[{"label":"Yes","value":"yes"},{"label":"No","value":"no"}],"value":{"path":"/answers/q2"}}
		]}}
		```
		""";

	public static string Build(string surfaceId) =>
		Template.Replace("__SURFACE_ID__", surfaceId).Replace("__CATALOG_ID__", BasicCatalogIds.CatalogId);
}
