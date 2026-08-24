export function isVisualEnvelope(value) {
  return Boolean(value && typeof value === "object" && typeof value.kind === "string" && typeof value.state === "string" && "data" in value && Array.isArray(value.problems || []));
}
