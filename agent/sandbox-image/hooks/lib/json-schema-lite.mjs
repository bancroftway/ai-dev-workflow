// Minimal JSON Schema (draft 2020-12 subset) validator, dependency-free (the sandbox image ships
// no npm packages alongside these hooks -- see require-skills-stop.mjs's own zero-dependency
// precedent). Covers exactly the constructs Pydantic v2's `.model_json_schema()` emits for the
// plain, mostly-flat models this pipeline exports (`object`/`array`/`string`/`integer`/`number`/
// `boolean` types, `required`, `properties`, `items`, `enum`, and `$ref` into a top-level `$defs`
// map) -- NOT a general-purpose validator (no `oneOf`/`allOf`/`anyOf`/`patternProperties`/
// `additionalProperties` handling, no format keywords). If a future model needs one of those,
// extend this file rather than hand-rolling a second, narrower validator elsewhere -- the whole
// point of exporting real JSON Schema (agent/src/schemas.py's `export_hook_schemas`) instead of a
// hand-typed field list is that ONE generic walker here can validate ANY of these schema files,
// so a hook never re-encodes a model's field names as its own magic strings.
//
// Returns a flat list of `{path, message}` errors (empty = valid) rather than throwing, so a
// caller can report every problem in one pass instead of stopping at the first.

function typeOf(value) {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value; // "object"/"string"/"number"/"boolean"/"undefined"
}

function checkType(value, schemaType, path, errors) {
  const actual = typeOf(value);
  if (schemaType === "integer") {
    if (actual !== "number" || !Number.isInteger(value)) {
      errors.push({ path, message: `expected integer, got ${actual}` });
    }
    return;
  }
  if (actual !== schemaType) {
    errors.push({ path, message: `expected ${schemaType}, got ${actual}` });
  }
}

function resolveRef(ref, defs) {
  // Pydantic always emits local refs shaped "#/$defs/<Name>" for this subset -- anything else is
  // a schema shape this validator doesn't understand yet (see module header).
  const match = /^#\/\$defs\/(.+)$/.exec(ref || "");
  if (!match || !defs || !(match[1] in defs)) return null;
  return defs[match[1]];
}

/** Validates `value` against `schema` (a JSON Schema object, or `{$ref: "#/$defs/X"}`), appending
 * `{path, message}` entries to `errors`. `path` starts as `"$"` (jq-style) so a nested error reads
 * as e.g. `$.diagrams[1]`. `defs` is the schema document's own top-level `$defs` map, threaded
 * through every recursive call so a `$ref` anywhere below the root can resolve. */
function validateNode(value, schema, path, defs, errors) {
  if (!schema || typeof schema !== "object") return;

  if (typeof schema.$ref === "string") {
    const resolved = resolveRef(schema.$ref, defs);
    if (resolved === null) {
      errors.push({ path, message: `unresolvable $ref ${JSON.stringify(schema.$ref)}` });
      return;
    }
    validateNode(value, resolved, path, defs, errors);
    return;
  }

  if (typeof schema.type === "string") {
    checkType(value, schema.type, path, errors);
  }

  if (Array.isArray(schema.enum) && !schema.enum.includes(value)) {
    errors.push({ path, message: `must be one of ${JSON.stringify(schema.enum)}, got ${JSON.stringify(value)}` });
  }

  if (schema.type === "object" && value !== null && typeof value === "object" && !Array.isArray(value)) {
    for (const required of schema.required || []) {
      if (!(required in value)) {
        errors.push({ path, message: `missing required field '${required}'` });
      }
    }
    for (const [key, propSchema] of Object.entries(schema.properties || {})) {
      if (key in value) {
        validateNode(value[key], propSchema, `${path}.${key}`, defs, errors);
      }
    }
  }

  if (schema.type === "array" && Array.isArray(value) && schema.items) {
    value.forEach((item, i) => validateNode(item, schema.items, `${path}[${i}]`, defs, errors));
  }
}

/** Top-level entry point: validates `instance` against `schemaDoc` (as written by
 * `export_hook_schemas`/`write_hook_schemas` -- a full JSON Schema document, `$defs` and all).
 * Returns a list of `{path, message}` errors (empty = valid). */
export function validate(instance, schemaDoc) {
  const errors = [];
  validateNode(instance, schemaDoc, "$", schemaDoc.$defs || {}, errors);
  return errors;
}
