import assert from 'node:assert/strict';
import test from 'node:test';
import { clearUpdateTracking, isUpdateRunning, readUpdateSnapshot, rememberUpdateView, restoreUpdateView, saveUpdateSnapshot, selectUpdateOperation, updateTrackingKey } from '../frontend/src/updateState.ts';

const previous = { id: 'old', stage: 'complete', started_at: 100, progress: 100 };
const queued = { id: 'new', stage: 'queued', started_at: 200, progress: 0 };
const state = operation => ({ current_version: '0.7.4', repository: 'owner/assistant', updater_available: true, check: {}, operation });

test('new queued task stays visible when server briefly returns the previous result', () => {
  assert.equal(selectUpdateOperation(queued, previous), queued);
  assert.equal(isUpdateRunning(selectUpdateOperation(queued, previous)), true);
});

test('executor progress and terminal result replace the queued state for the same task', () => {
  for (const stage of ['building', 'complete', 'failed', 'rolled_back']) {
    const live = { ...queued, stage, progress: stage === 'building' ? 25 : 100 };
    assert.equal(selectUpdateOperation(queued, live), live);
    assert.equal(isUpdateRunning(live), stage === 'building');
  }
});

test('missing live operation preserves running and terminal cached results', () => {
  for (const operation of [queued, previous]) assert.equal(selectUpdateOperation(operation, {}), operation);
});

test('newer server task supersedes a saved result and older server task cannot overwrite it', () => {
  const newest = { ...queued, id: 'newest', started_at: 300 };
  assert.equal(selectUpdateOperation(previous, newest), newest);
  assert.equal(selectUpdateOperation(newest, previous), newest);
});

test('refresh restores progress, all steps and view, while logout clears saved state', () => {
  const storage = () => {
    const values = new Map();
    return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
  };
  globalThis.sessionStorage = storage();
  globalThis.localStorage = storage();
  const saved = state({ ...queued, stage: 'building', progress: 25, history: Array.from({ length: 9 }, (_, at) => ({ at, stage: 'checking', message: `step ${at}` })) });
  saveUpdateSnapshot(saved);
  rememberUpdateView(true);
  assert.deepEqual(readUpdateSnapshot(), saved);
  assert.equal(restoreUpdateView(), true);
  rememberUpdateView(false);
  assert.equal(restoreUpdateView(), false);
  localStorage.setItem(updateTrackingKey, queued.id);
  assert.equal(restoreUpdateView(), true);
  clearUpdateTracking();
  assert.equal(readUpdateSnapshot(), null);
  assert.equal(restoreUpdateView(), false);
});

test('unavailable browser storage does not prevent rendering', () => {
  delete globalThis.sessionStorage;
  delete globalThis.localStorage;
  assert.equal(readUpdateSnapshot(), null);
  assert.equal(restoreUpdateView(), false);
  assert.doesNotThrow(() => { saveUpdateSnapshot(state(queued)); rememberUpdateView(true); clearUpdateTracking(); });
});
