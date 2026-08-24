const FORBIDDEN = /(^|_)(id|ref|token|secret|password|prompt|trace|correlation|schema|abi|provider)(_|$)/i;

export function scanPrivateFields(value, path = "") {
  const findings = [];
  if (Array.isArray(value)) value.forEach((item, index) => findings.push(...scanPrivateFields(item, `${path}[${index}]`)));
  else if (value && typeof value === "object") for (const [key, item] of Object.entries(value)) {
    const next = path ? `${path}.${key}` : key;
    if (FORBIDDEN.test(key)) findings.push(next); else findings.push(...scanPrivateFields(item, next));
  }
  return findings;
}

export function safeText(value, fallback = "") { return typeof value === "string" && value.trim() ? value.trim() : fallback; }
