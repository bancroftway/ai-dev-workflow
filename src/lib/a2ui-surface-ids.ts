// Plain constants (no React) so both the client catalog and the server route
// handler can import them without pulling client-only code into the server bundle.
// Must match agent/src/a2ui_tools.py's constants exactly.
export const SPECIFICATION_SURFACE_ID = "specification";
export const PLAN_SURFACE_ID = "plan";
export const CATALOG_ID = "urn:ai-dev-workflow:catalog";
