export async function api<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api${path}`, {method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? undefined : {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body), signal});
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(typeof payload.detail === 'string' ? payload.detail : `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}
export async function upload(file: File) {
  const response = await fetch(`/api/circuits?name=${encodeURIComponent(file.name)}`, {method: 'POST', body: file, headers: {'Content-Type': 'text/plain'}});
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Upload failed');
  return data;
}
export function seconds(value: number): string {
  if (value < .001) return `${(value * 1e6).toFixed(1)} µs`;
  if (value < 1) return `${(value * 1000).toFixed(1)} ms`;
  if (value < 60) return `${value.toFixed(3)} s`;
  return `${(value / 60).toFixed(2)} min`;
}
