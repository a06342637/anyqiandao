import { useEffect, useState } from 'react';
import { Clock3, FileText, X } from 'lucide-react';
import { api, formatTime, refreshData, useResource } from './api';
import { Button, EmptyState, ErrorNotice, kindLabel, Pagination, RefreshButton, useConfirm, usePageClamp, usePageSize, useToast } from './ui';
import { useSelection } from './selection';
import HistoryToolbar from './HistoryToolbar';
import RetentionSettings from './RetentionSettings';
import type { HistoryResult, LogEntry, PageData, Selection } from './types';

const CATEGORIES = [
  { id: '', label: '全部' }, { id: 'checkin', label: '签到记录' }, { id: 'invalid', label: '凭证失效' }, { id: 'error', label: '错误' },
  { id: 'extract', label: '提取' }, { id: 'validate', label: '检测' }, { id: 'proxy', label: '代理' }, { id: 'backup', label: '备份' }, { id: 'system', label: '系统' },
];

export default function LogsPage({ timezone, jobId, accountId, onClearFilter }: { timezone: string; jobId: string; accountId: string; onClearFilter: () => void }) {
  const [page, setPage] = useState(1);
  const [limit, setLimit] = usePageSize('logs', 20);
  const [category, setCategory] = useState('');
  const [retention, setRetention] = useState(false);
  const [busy, setBusy] = useState(false);
  const selection = useSelection();
  const confirm = useConfirm();
  const toast = useToast();
  useEffect(() => { setPage(1); selection.clear(); }, [jobId, accountId, category]);
  const { data, error, reload } = useResource<PageData<LogEntry> & { categories: Record<string, number> }>(`/logs?page=${page}&limit=${limit}${category ? '&category=' + category : ''}${jobId ? '&job_id=' + encodeURIComponent(jobId) : ''}${accountId ? '&account_id=' + encodeURIComponent(accountId) : ''}`);
  usePageClamp(page, data?.total, limit, setPage);
  const counts = data?.categories || {};
  const totalAll = Object.values(counts).reduce((sum, value) => sum + value, 0);
  const remove = async (payload: Selection & { before?: number }) => {
    if (!await confirm({ title: payload.before ? '清理当前筛选中截止时间以前的日志？' : `删除所选 ${selection.count(data?.total || 0)} 条日志？`, message: '只清理当前任务、账号和分类筛选中的运行日志。等待/运行任务的日志会保留；账号、凭证、队列任务和签到统计不会删除。此操作无法撤销。', confirmLabel: '确认清理', danger: true })) return;
    setBusy(true);
    try {
      const result = await api<HistoryResult>('/logs/delete', 'POST', { ...payload, category: category || undefined, job_id: jobId || undefined, account_id: accountId || undefined });
      selection.clear(); refreshData(); toast(`已清理 ${result.removed} 条日志${result.protected ? `；保留 ${result.protected} 条活动任务日志` : ''}`, 'success');
    } catch (caught) { toast((caught as Error).message, 'error'); } finally { setBusy(false); }
  };
  return <section className="panel logs-panel"><div className="panel-heading"><div className="heading-icon"><FileText size={19} /></div><div><h2>运行日志</h2><p>支持分类、跨页多选和按截止时间清理</p></div><div className="panel-tools"><Button onClick={() => setRetention(true)}><Clock3 size={15} />日志保留</Button><RefreshButton onClick={reload} label="日志已刷新" /></div></div>
    <div className="filter-bar category-bar">{CATEGORIES.map(item => <button key={item.id} className={`category-chip ${category === item.id ? 'active' : ''} ${item.id === 'invalid' || item.id === 'error' ? 'alert' : ''}`} onClick={() => { setCategory(item.id); setPage(1); }}>{item.label}<span>{item.id ? counts[item.id] || 0 : totalAll}</span></button>)}{(jobId || accountId) && <button className="filter-chip" onClick={() => { onClearFilter(); setPage(1); }}>{jobId ? `当前任务 ${jobId.slice(0, 8)}` : `当前账号 ${accountId.slice(0, 8)}`}<X size={13} /></button>}<span className="form-hint push-right">时区 {timezone}</span></div><ErrorNotice message={error} retry={reload} />
    <HistoryToolbar selection={selection} pageIds={data?.items.map(entry => String(entry.id)) || []} total={data?.total || 0} busy={busy} onRemove={payload => void remove(payload)} />
    {!data?.items.length ? <EmptyState title={category ? '这个分类还没有记录' : '还没有运行记录'} description="任务开始后，执行状态和处理结果会显示在这里。" /> : <div className="log-list">{data.items.map(entry => <article key={entry.id} className={`log-row selectable ${entry.level}`}><input type="checkbox" aria-label={`选择日志 ${entry.id}`} checked={selection.selected(String(entry.id))} onChange={event => selection.toggle(String(entry.id), event.target.checked)} /><time>{formatTime(entry.created, timezone)}</time><span className={`log-level ${entry.level} ${entry.category === 'invalid' ? 'invalid' : ''}`}>{entry.category === 'invalid' ? '失效' : entry.level === 'error' ? '错误' : entry.level === 'warning' ? '提醒' : '信息'}</span><div className="log-content"><div><span className="log-kind">{kindLabel(entry.kind)}</span>{entry.username && <strong>{entry.username}</strong>}</div><p>{entry.message}</p></div></article>)}</div>}
    <Pagination page={page} total={data?.total || 0} limit={limit} onChange={setPage} onLimitChange={setLimit} /><div className="panel-footnote">不会记录密码、Cookie、原始网站响应或错误截图；日志清理不删除账号凭证。</div>{retention && <RetentionSettings onClose={() => setRetention(false)} />}</section>;
}
