// REST client for the local daemon.
//
// The per-install token arrives in the URL fragment (never sent to the server or
// logged). It is removed from the address bar at once and kept for this window only.

const fromHash = new URLSearchParams(location.hash.slice(1)).get("token");
if (fromHash) {
  sessionStorage.setItem("jarvis-token", fromHash);
  history.replaceState(null, "", location.pathname);
}
export const TOKEN = sessionStorage.getItem("jarvis-token") || "";

export async function api<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...options,
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return (res.status === 204 ? null : res.json()) as Promise<T>;
}

export const post = <T = any>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const put = <T = any>(path: string, body: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(body) });

export const del = <T = any>(path: string) => api<T>(path, { method: "DELETE" });

export const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));
