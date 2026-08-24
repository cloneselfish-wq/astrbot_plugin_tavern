const UTC8_OFFSET_MS = 8 * 60 * 60 * 1000;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ISO_DATE_TIME = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$/i;

const pad = (value) => String(value).padStart(2, "0");

function parseTimestamp(value) {
  if (value instanceof Date) return Number.isNaN(value.valueOf()) ? null : value;
  if (typeof value === "number") {
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? null : date;
  }
  const text = String(value ?? "").trim();
  if (!ISO_DATE.test(text) && !ISO_DATE_TIME.test(text)) return null;
  let normalized = text.replace(" ", "T");
  if (ISO_DATE_TIME.test(text) && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized)) normalized += "Z";
  const date = new Date(normalized);
  return Number.isNaN(date.valueOf()) ? null : date;
}

function utc8Date(value) {
  const parsed = parseTimestamp(value);
  return parsed ? new Date(parsed.valueOf() + UTC8_OFFSET_MS) : null;
}

export function formatUtc8Minute(value, { fallback = "" } = {}) {
  const shifted = utc8Date(value);
  if (!shifted) return fallback || String(value ?? "");
  return `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())} ${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())} UTC+8`;
}

export function formatUtc8Day(value, { fallback = "" } = {}) {
  const shifted = utc8Date(value);
  if (!shifted) return fallback || String(value ?? "");
  return `${shifted.getUTCFullYear()}年${shifted.getUTCMonth() + 1}月${shifted.getUTCDate()}日`;
}

export function formatTimeField(name, value) {
  return /(?:_at|_time|timestamp|time_label)$/i.test(String(name || ""))
    ? formatUtc8Minute(value)
    : value;
}
