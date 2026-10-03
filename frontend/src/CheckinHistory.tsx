import { useState } from 'react';
import { api, formatTime, refreshData, useResource } from './api';
import { Badge, EmptyState, ErrorNotice, Modal, Pagination, RefreshButton, useConfirm, usePageClamp, usePageSize, useToast } from './ui';
import { useSelection } from './selection';
import BalanceDetail from './BalanceDetail';
import HistoryToolbar from './HistoryToolbar';
import type { CheckinEntry, HistoryResult, PageData, Selection } from './types';

export default function CheckinHistory({ account, timezone, onClose, onLogs }: {
  account?: { id: string; username: string }; timezone: string; onClose: () => void; onLogs: (jobId: string) => void;
}) {
  const toast = useToast();
  const confirm = useConfirm();
  const [page, setPage] = useState(1);
  const [limit, setLimit] = usePageSize('checkin-history', 10);
  const [busy, setBusy] = useState(false);
  const selection = useSelection();
  const { data, error, reload } = useResource<PageData<CheckinEntry>>(`/checkins?page=${page}&limit=${limit}${account ? '&account_id=' + encodeURIComponent(account.id) : ''}`);
  usePageClamp(page, data?.total, limit, setPage);
  const remove = async (payload: Selection & { before?: number }) => {
    if (!await confirm({ title: payload.before ? '清理截止时间以前的签到明细？' : `删除所选 ${selection.count(data?.total || 0)} 条签到明细？`, message: `${account ? `仅处理「${account.username}」` : '处理所有账号'}的签到历史，对应仪表盘统计会同步减少。账号凭证、签到计划和等待/运行任务不受影响；此操作无法撤销。`, confirmLabel: '确认清理', danger: true })) return;
    setBusy(true);
    try {
      const result = await api<HistoryResult>('/checkins/delete', 'POST', { ...payload, account_id: account?.id });
      selection.clear(); refreshData(); toast(`已清理 ${result.removed} 条签到明细${result.protected ? `；保留 ${result.protected} 条活动任务记录` : ''}`, 'success');
    } catch (caught) { toast((caught as Error).message, 'error'); } finally { setBusy(false); }
  };
  return <Modal title={account ? `${account.username} · 签到明细` : '全部账号 · 签到明细'} subtitle="可按记录多选删除，也可按截止时间清理。" onClose={onClose} wide className="history-modal">
    <div className="history-heading"><span className="form-hint">时区 {timezone} · 保留天数在「日志保留」设置</span><RefreshButton onClick={reload} /></div>
    <ErrorNotice message={error} retry={reload} />
    <HistoryToolbar selection={selection} pageIds={data?.items.map(entry => entry.id) || []} total={data?.total || 0} busy={busy} onRemove={payload => void remove(payload)} />
    {!data?.items.length ? <EmptyState title="暂无签到明细" description="这里只显示仍在保留期内的签到记录，清理不会删除账号。" /> : <div className="checkin-history-list">{data.items.map(entry => <article key={entry.id} className="checkin-history-row">
      <input type="checkbox" aria-label={`选择签到明细 ${entry.id}`} checked={selection.selected(entry.id)} onChange={event => selection.toggle(entry.id, event.target.checked)} />
      <div className="checkin-history-main"><div><strong>{entry.username || '已删除账号'}</strong><Badge status={entry.code} /><time>{formatTime(entry.created, timezone)}</time></div><BalanceDetail balance={entry} />{entry.used_before !== null && entry.used_after !== null && <small className="muted">期间消耗：${Math.max(0, entry.used_after - entry.used_before).toFixed(4)}</small>}</div>
      {entry.job_id && <button className="text-button" onClick={() => { onClose(); onLogs(entry.job_id!); }}>执行日志</button>}
    </article>)}</div>}
    <Pagination page={page} total={data?.total || 0} limit={limit} onChange={setPage} onLimitChange={setLimit} />
  </Modal>;
}
