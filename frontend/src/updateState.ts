export interface Release { version: string; release_url: string; notes: string; published_at: string | null; }
export interface VersionCheck { repository?: string; checked_at?: number; release?: Release; update_available?: boolean; error?: string; }
export interface UpdateOperation { id?: string; stage?: string; message?: string; progress?: number; version?: string; started_at?: number; updated_at?: number; finished_at?: number; backup_path?: string; history?: { at: number; stage: string; message: string }[]; }
export interface UpdateState { current_version: string; repository: string; updater_available: boolean; check: VersionCheck; operation: UpdateOperation; }

export const updateTrackingKey = 'any-update-operation';
const viewKey = 'any-update-view';
const snapshotKey = 'any-update-snapshot';
export const isUpdateRunning = (operation?: UpdateOperation) => !!operation?.id && !['complete', 'failed', 'rolled_back'].includes(operation.stage || '');

export function selectUpdateOperation(saved?: UpdateOperation | null, live?: UpdateOperation): UpdateOperation | undefined {
  if (!live?.id) return saved || live;
  if (saved?.id && saved.id !== live.id && (saved.started_at || 0) > (live.started_at || 0)) return saved;
  return live;
}

export function restoreUpdateView() {
  try { return sessionStorage.getItem(viewKey) === 'updates' || !!localStorage.getItem(updateTrackingKey); } catch { return false; }
}
export function rememberUpdateView(open: boolean) {
  try { if (open) sessionStorage.setItem(viewKey, 'updates'); else sessionStorage.removeItem(viewKey); } catch {}
}
export function readUpdateSnapshot(): UpdateState | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(snapshotKey) || 'null');
    if (value && typeof value.current_version === 'string' && typeof value.repository === 'string' && value.check && typeof value.check === 'object' && value.operation && typeof value.operation === 'object') return value;
  } catch {}
  return null;
}
export function saveUpdateSnapshot(value: UpdateState) {
  try { sessionStorage.setItem(snapshotKey, JSON.stringify(value)); } catch {}
}
export function clearUpdateTracking() {
  try { sessionStorage.removeItem(viewKey); sessionStorage.removeItem(snapshotKey); localStorage.removeItem(updateTrackingKey); } catch {}
}
