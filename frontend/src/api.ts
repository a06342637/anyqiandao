import { useCallback, useEffect, useRef, useState } from 'react';

let csrfToken = '';
export function setCsrfToken(value: string) { csrfToken = value; }
export class ApiError extends Error { constructor(message: string, public status: number) { super(message); } }
export async function fetchApi(path: string, method = 'GET', body?: unknown, signal?: AbortSignal) {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET' && csrfToken) headers['X-CSRF-Token'] = csrfToken;
  const response = await fetch(`/api/v1${path}`, { method, headers, credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body), signal });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401 && path !== '/auth/login') window.dispatchEvent(new Event('any-auth-expired'));
    throw new ApiError(typeof payload.detail === 'string' ? payload.detail : `请求失败（${response.status}）`, response.status);
  }
  return response;
}
export async function api<Value>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<Value> {
  return (await fetchApi(path, method, body, signal)).json();
}
export function refreshData() { window.dispatchEvent(new Event('any-refresh')); }
export function newId() { return Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, '0')).join(''); }
export function useResource<Value>(path: string | null, interval = 3000) {
  const [data, setData] = useState<Value | null>(null);
  const [loadedPath, setLoadedPath] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [errorPath, setErrorPath] = useState<string | null>(null);
  const loader = useRef<(() => Promise<boolean>) | null>(null);
  const reload = useCallback(async () => loader.current ? await loader.current() : false, []);
  useEffect(() => {
    if (!path) return;
    setError('');
    setErrorPath(null);
    let disposed = false;
    let pending: Promise<boolean> | null = null;
    let controller: AbortController | null = null;
    const load = (fresh = false): Promise<boolean> => {
      if (pending) return fresh ? pending.catch(() => false).then(() => disposed ? false : load()) : pending;
      controller = new AbortController();
      pending = api<Value>(path, 'GET', undefined, controller.signal).then(result => {
        if (!disposed) { setData(result); setLoadedPath(path); setError(''); }
        return !disposed;
      }).catch(caught => {
        if (disposed || caught instanceof DOMException && caught.name === 'AbortError') return false;
        setError(caught instanceof Error ? caught.message : '网络暂时不可用');
        setErrorPath(path);
        throw caught;
      }).finally(() => { pending = null; });
      return pending;
    };
    const backgroundLoad = () => { void load().catch(() => {}); };
    const refresh = () => { void load(true).catch(() => {}); };
    loader.current = () => load(true);
    backgroundLoad();
    const timer = interval ? window.setInterval(() => { if (!document.hidden) backgroundLoad(); }, interval) : undefined;
    window.addEventListener('any-refresh', refresh);
    return () => { disposed = true; loader.current = null; controller?.abort(); if (timer) window.clearInterval(timer); window.removeEventListener('any-refresh', refresh); };
  }, [path, interval]);
  return { data: loadedPath === path ? data : null, error: errorPath === path ? error : '', reload };
}
export function formatTime(timestamp: number | null | undefined, timezone?: string) {
  if (!timestamp) return '—';
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: timezone }).format(new Date(timestamp * 1000));
}
export async function copyText(value: string) {
  if (navigator.clipboard) { await navigator.clipboard.writeText(value); return; }
  const previous = document.activeElement as HTMLElement | null;
  const field = document.createElement('textarea');
  field.value = value; field.style.position = 'fixed'; field.style.opacity = '0'; field.setAttribute('readonly', '');
  document.body.append(field); field.select();
  try { if (!document.execCommand('copy')) throw new Error('浏览器未允许复制，请使用 HTTPS 或下载配置文件'); }
  finally { field.remove(); previous?.focus(); }
}
export function downloadBlob(blob: Blob, name: string) {
  const address = URL.createObjectURL(blob);
  const link = document.createElement('a'); link.href = address; link.download = name; link.click();
  window.setTimeout(() => URL.revokeObjectURL(address), 1000);
}
