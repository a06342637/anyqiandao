import { useState, type FormEvent } from 'react';
import { Clock3, Save, Trash2 } from 'lucide-react';
import { api, formatTime, refreshData, useResource } from './api';
import { Button, ErrorNotice, Modal, useConfirm, useToast } from './ui';
import type { CleanupResult, SettingsData } from './types';

export const cleanupSummary = (result: CleanupResult) => `已清理 ${result.logs} 条运行/账号日志、${result.jobs} 条队列记录、${result.checkins} 条签到明细`;

export default function RetentionSettings({ onClose }: { onClose: () => void }) {
  const { data, error, reload } = useResource<SettingsData>('/settings', 0);
  return <Modal title="日志保留与自动清理" subtitle="运行日志（含备份日志）、队列历史及仪表盘账号明细共用此清理计划。" onClose={onClose}>
    <ErrorNotice message={error} retry={reload} />
    {data ? <RetentionForm initial={data} onClose={onClose} /> : <p className="muted">正在读取保留设置…</p>}
  </Modal>;
}

function RetentionForm({ initial, onClose }: { initial: SettingsData; onClose: () => void }) {
  const toast = useToast();
  const confirm = useConfirm();
  const [values, setValues] = useState(() => ({
    log_retention_days: initial.settings.log_retention_days,
    queue_retention_days: initial.settings.queue_retention_days,
    stats_retention_days: initial.settings.stats_retention_days,
    log_cleanup_hours: initial.settings.log_cleanup_hours,
    log_cleanup_enabled: initial.settings.log_cleanup_enabled,
  }));
  const [busy, setBusy] = useState(false);
  const save = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try { await api('/settings', 'PUT', values); refreshData(); toast('日志保留和清理间隔已保存', 'success'); onClose(); }
    catch (caught) { toast((caught as Error).message, 'error'); }
    finally { setBusy(false); }
  };
  const cleanup = async () => {
    if (!await confirm({ title: '按已保存策略清理到期记录？', message: '运行/账号日志、已结束队列记录和签到明细将按各自保留天数清理。未保存的修改不参与本次清理；账号、凭证和等待/运行任务不会删除。', confirmLabel: '清理到期记录', danger: true })) return;
    setBusy(true);
    try { const result = await api<CleanupResult>('/logs/cleanup', 'POST'); refreshData(); toast(cleanupSummary(result), 'success'); }
    catch (caught) { toast((caught as Error).message, 'error'); }
    finally { setBusy(false); }
  };
  return <form onSubmit={save}>
    <label className="check-label setting-toggle"><input type="checkbox" checked={values.log_cleanup_enabled} onChange={event => setValues(previous => ({ ...previous, log_cleanup_enabled: event.target.checked }))} /><span><strong>自动清理到期记录</strong><small>关闭自动清理后，仍可手动清理或多选删除。</small></span></label>
    <div className="form-grid">
      {([
        ['log_retention_days', '运行 / 账号日志保留天数', 3650],
        ['queue_retention_days', '已结束队列记录保留天数', 3650],
        ['stats_retention_days', '账号明细 / 签到统计保留天数', 3650],
        ['log_cleanup_hours', '每隔多少小时清理', 168],
      ] as const).map(([key, label, max]) => <label className="field" key={key}>{label}<input type="number" required min={1} max={max} value={values[key]} onChange={event => setValues(previous => ({ ...previous, [key]: Number(event.target.value) }))} /></label>)}
    </div>
    <div className="notice"><Clock3 size={16} /><span>上次清理：{formatTime(initial.last_log_cleanup, initial.settings.timezone)}<br />下次清理：{initial.settings.log_cleanup_enabled ? formatTime(initial.next_log_cleanup, initial.settings.timezone) : '自动清理已关闭'}<br />按已保存设置显示 · {initial.settings.timezone}</span></div>
    <p className="form-hint">队列仅清理已结束的历史；账号凭证、等待/运行任务及其日志保留。签到明细清理后，对应历史统计同步减少，账号最近状态不会重置。</p>
    <div className="modal-actions"><Button disabled={busy} onClick={() => void cleanup()}><Trash2 size={15} />清理到期记录</Button><Button type="submit" variant="primary" busy={busy}><Save size={15} />保存清理设置</Button></div>
  </form>;
}
