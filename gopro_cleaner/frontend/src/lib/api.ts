const JSON_HEADERS = { "Content-Type": "application/json" };
export const host = "http://127.0.0.1:8765/";

export async function api<T = any>(url: string, options: RequestInit = {}): Promise<T> {
  const isForm = options.body instanceof FormData;
  const response = await fetch(host + url, {
    ...options,
    headers: isForm ? (options.headers ?? {}) : { ...JSON_HEADERS, ...(options.headers || {}) },
  });
  const text = await response.text();
  let payload: any = {};
  let parsed = true;
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    parsed = false;
    payload = {};
  }
  if (!parsed) {
    throw new Error(`Unexpected response from ${url} (${response.status})`);
  }
  if (!response.ok) {
    throw new Error(payload?.error || `Request failed (${response.status})`);
  }
  return payload as T;
}

/** Fetch a binary attachment and trigger a browser download. */
export async function downloadApi(
  url: string,
  options: RequestInit = {},
  fallbackName = "download.bin",
): Promise<void> {
  const isForm = options.body instanceof FormData;
  const response = await fetch(host + url, {
    ...options,
    headers: isForm ? (options.headers ?? {}) : { ...JSON_HEADERS, ...(options.headers || {}) },
  });
  if (!response.ok) {
    const text = await response.text();
    let message = `Download failed (${response.status})`;
    try {
      const payload = text ? JSON.parse(text) : {};
      if (payload?.error) message = payload.error;
    } catch {
      /* ignore */
    }
    throw new Error(message);
  }
  const blob = await response.blob();
  let filename = fallbackName;
  const cd = response.headers.get("Content-Disposition") || "";
  const match = /filename\*?=(?:UTF-8''|")?([^\";]+)/i.exec(cd);
  if (match?.[1]) {
    try {
      filename = decodeURIComponent(match[1].replace(/"/g, "").trim());
    } catch {
      filename = match[1].replace(/"/g, "").trim() || fallbackName;
    }
  }
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objectUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(objectUrl);
}

export function formatBytes(bytes?: number | null) {
  if (bytes === null || bytes === undefined) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function formatClock(seconds?: number | null) {
  if (!seconds && seconds !== 0) return "00:00:00";
  const s = Math.max(0, Math.floor(seconds));
  const h = String(Math.floor(s / 3600)).padStart(2, "0");
  const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const sec = String(s % 60).padStart(2, "0");
  return `${h}:${m}:${sec}`;
}
