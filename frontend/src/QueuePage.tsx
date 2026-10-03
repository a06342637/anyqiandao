import { useEffect, useState, type FormEvent } from 'react';
import { CalendarClock, Check, Clock3, Layers3, ListOrdered, Pause, Play, Save, Trash2, TriangleAlert, X } from 'lucide-react';
import { api, formatTime, refreshData, useResource } from './api';
import { Badge, Button, EmptyState, ErrorNotice, kindLabel, Pagination, RefreshButton, useConfirm, usePageClamp, usePageSize, useToast } from './ui';
import type { Job, QueueData, QueueSettings } from './types';
import { useSelection } from './selection';
import RetentionSettings from './RetentionSettings';
import type { HistoryResult } from './types';

const symbolOf = (status: string) => {
  if (status === 'running') return { tone: 'working', icon: <span className="loading-dot" /> };
  if (status === 'pending') return { tone: 'waiting', icon: <Clock3 size={15} /> };
  if (['success', 'signed', 'already_signed'].includes(status)) return { tone: 'done', icon: <Check size={16} /> };
  if (status === 'cancelled') return { tone: '', icon: <X size={15} /> };
  return { tone: 'failed', icon: <TriangleAlert size={15} /> };
};

export default function QueuePage({ onLogs, timezone }: { onLogs: (jobId: string) => void; timezone: string }) {
  const toast = useToast();
  const confirm = useConfirm();
  const [page, setPage] = useState(1);
  const [limit, setLimit] = usePageSize('queue', 10);
  const [lane, setLane] = useState('');
  const [state, setState] = useState('');
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState('');
  const [retention, setRetention] = useState(false);
  const selection = useSelection();
  const { data, error, reload } = useResource<QueueData>(`/jobs?page=${page}&limit=${limit}&lane=${lane}&state=${state}`);
  usePageClamp(page, data?.total, limit, setPage);
  const control = async (action: string) => {
    if (action === 'cancel-pending' && !await confirm({ title: '取消全部等待任务？', message: '取消普通队列和签到队列中所有尚未开始的任务。正在执行的任务、账号和签到计划不受影响。', confirmLabel: '取消全部等待', danger: true })) return;
    setBusy(true);
    try {
      await api(`/queue/${action}`, 'POST');
      refreshData();
      toast(action === 'pause' ? '队列已暂停，正在执行的任务会继续完成' : action === 'resume' ? '队列已继续，按已保存的并发数量执行' : '全部等待任务已取消', 'success');
    } catch (caught) { toast((caught as Error).message, 'error'); } finally { setBusy(false); }
  };
  const remove = async (job: Job) => {
    const running = job.status === 'running';
    if (!await confirm({ title: running ? '停止并删除正在执行的任务？' : '从执行队列删除任务？', message: running && job.kind === 'checkin' ? '会停止任务并等待清理。签到请求若已发送，结果将标记为待确认，不会自动重发。账号、执行日志和签到统计保留。' : '等待中的任务不会再执行，正在执行的任务会先停止。只移除这条队列记录，账号、执行日志和签到计划保留。', confirmLabel: running ? '停止并删除' : '删除任务', danger: true })) return;
    setRemoving(job.id);
    try {
      const result = await api<{ status: string }>(`/jobs/${job.id}`, 'DELETE');
      refreshData();
      toast(result.status === 'uncertain' ? '任务已删除；签到结果待确认，请查看日志' : '任务已从执行队列删除，日志保留', 'success');
    } catch (caught) { toast((caught as Error).message, 'error'); } finally { setRemoving(''); }
  };
  const counts = data?.counts;
  const removeMany = async (finishedOnly = false) => {
    if (!await confirm({ title: finishedOnly ? '清理当前队列的已结束记录？' : `删除所选 ${selection.count(data?.total || 0)} 条队列记录？`, message: finishedOnly ? '只移除已结束的队列历史，等待和运行中的任务不会停止或删除。账号、执行日志和签到统计保留。' : '所选等待任务会取消，运行任务会先安全停止再移除；已发出的签到请求保持待确认，不自动重发。账号、日志和签到统计保留。', confirmLabel: finishedOnly ? '清理已结束' : '停止并删除所选', danger: true })) return;
    setBusy(true);
    try {
      const result = await api<HistoryResult>('/jobs/delete', 'POST', { ...(finishedOnly ? { all_matching: true } : selection.payload), lane, state: finishedOnly ? 'finished' : state });
      selection.clear(); refreshData(); toast(`已移除 ${result.removed} 条队列记录${result.protected ? `；${result.protected} 条未安全结束，请稍后重试` : ''}`, 'success');
    } catch (caught) { toast((caught as Error).message, 'error'); } finally { setBusy(false); }
  };
  return <div className="queue-page">
    <div className="queue-overview">
      <div className="queue-lane-summary"><span className="heading-icon"><Layers3 size={19} /></span><div><strong>普通队列</strong><p>提取 · 检测 · 数据查询 · 代理测试</p></div><span className="queue-lane-count"><b>{counts?.queue_running ?? '—'}</b> 执行中<small>{counts?.queue_pending ?? '—'} 个等待 · 并发 {data?.max_concurrency ?? '—'}</small></span></div>
      <div className="queue-lane-summary"><span className="heading-icon"><CalendarClock size={19} /></span><div><strong>签到队列</strong><p>手动签到与所有定时计划统一排队</p></div><span className="queue-lane-count"><b>{counts?.checkin_running ?? '—'}</b> 执行中<small>{counts?.checkin_pending ?? '—'} 个等待 · 并发 {data?.checkin_concurrency ?? '—'}</small></span></div>
    </div>
    {data && <ConcurrencyForm settings={data} />}
    <section className="panel queue-panel">
      <div className="panel-heading"><div className="heading-icon"><ListOrdered size={19} /></div><div><h2>执行任务</h2><p>自动刷新进度 · 同一账号始终串行</p></div><div className="panel-tools"><RefreshButton onClick={async () => { const loaded = await reload(); if (loaded) refreshData(); return loaded; }} /><Button disabled={!data} busy={busy} onClick={() => void control(data?.paused ? 'resume' : 'pause')}>{data?.paused ? <Play size={15} /> : <Pause size={15} />}{data?.paused ? '继续队列' : '暂停队列'}</Button></div></div>
      <div className="queue-meta"><span className={`live-dot ${data?.paused ? 'paused' : ''}`} /><span>{data?.paused ? '队列已暂停' : '按入队顺序领取任务'}</span><button disabled={busy || !data || !counts || counts.queue_pending + counts.checkin_pending === 0} className="text-button push-right" onClick={() => void control('cancel-pending')}>取消全部等待任务</button></div>
      <ErrorNotice message={error} retry={reload} />
      {data?.paused && <div className="notice warning">{data.pause_reason || '已暂停新任务，点击「继续队列」恢复执行。'}</div>}
      <div className="queue-filters"><label>任务类型<select aria-label="筛选队列" value={lane} onChange={event => { setLane(event.target.value); setPage(1); selection.clear(); }}><option value="">全部队列</option><option value="queue">普通队列</option><option value="checkin">签到队列</option></select></label><label>任务状态<select aria-label="筛选任务状态" value={state} onChange={event => { setState(event.target.value); setPage(1); selection.clear(); }}><option value="">全部状态</option><option value="active">执行中与等待中</option><option value="finished">已结束</option></select></label><span>{data ? `共 ${data.total} 个任务` : '读取中…'}</span><Button onClick={() => setRetention(true)}><Clock3 size={15} />日志保留</Button></div>
      <div className="queue-bulkbar"><label className="check-label"><input type="checkbox" aria-label="选择本页任务" checked={!!data?.items.length && data.items.every(job => selection.selected(job.id))} onChange={event => data?.items.forEach(job => selection.toggle(job.id, event.target.checked))} />本页</label><span>已选 <strong>{selection.count(data?.total || 0)}</strong> 条{selection.all ? ' · 跨页全选' : ''}</span><button className="text-button" onClick={selection.selectAll}>全选当前筛选</button><button className="text-button" onClick={selection.clear}>清空选择</button><Button variant="danger" disabled={busy || !!removing || !selection.count(data?.total || 0)} onClick={() => void removeMany()}><Trash2 size={14} />删除所选</Button><Button disabled={busy || !!removing} onClick={() => void removeMany(true)}>清理已结束</Button></div>
      {!data ? <p className="queue-loading">正在读取执行队列…</p> : !data.items.length ? <EmptyState title={lane || state ? '没有符合条件的任务' : '队列为空'} description="添加账号、检测凭证或运行签到计划后，任务会显示在这里。" /> : <div className="job-list">{data.items.map(job => {
        const symbol = symbolOf(job.status);
        return <article className="job-row selectable" key={job.id}><input type="checkbox" aria-label={`选择任务 ${job.username || job.id.slice(0, 8)}`} checked={selection.selected(job.id)} onChange={event => selection.toggle(job.id, event.target.checked)} /><div className={`job-symbol ${symbol.tone}`}>{symbol.icon}</div><div className="job-description"><div className="job-title"><strong>{kindLabel(job.kind)}</strong><Badge status={job.status} /><span className="job-source">{job.source.startsWith('schedule:') ? '定时计划' : job.source.startsWith('auto:') ? '自动任务' : job.source === 'import' ? '账号导入' : '手动任务'}</span></div><p>{job.username || (job.account_id ? `账号 ${job.account_id.slice(0, 8)}` : '代理节点')} · {job.message || '等待执行'}</p><small>{formatTime(job.created, timezone)}<button className="text-button" onClick={() => onLogs(job.id)}>查看日志</button></small></div><Button className="job-delete" variant="ghost" busy={removing === job.id} disabled={!!removing || busy} aria-label={`删除任务 ${job.username || job.id.slice(0, 8)}`} onClick={() => void remove(job)}><Trash2 size={16} /><span>删除</span></Button></article>;
      })}</div>}
      <Pagination page={page} total={data?.total || 0} limit={limit} onChange={setPage} onLimitChange={setLimit} />
      <div className="panel-footnote">多选支持跨页；清理已结束不会影响等待/运行任务。队列、运行日志和账号明细可分别设置保留天数。</div>
    </section>
    {retention && <RetentionSettings onClose={() => setRetention(false)} />}
  </div>;
}

function ConcurrencyForm({ settings }: { settings: QueueSettings }) {
  const toast = useToast();
  const [draft, setDraft] = useState<QueueSettings | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const values = draft || settings;
  useEffect(() => { if (!dirty && !saving && draft?.max_concurrency === settings.max_concurrency && draft.checkin_concurrency === settings.checkin_concurrency) setDraft(null); }, [settings.max_concurrency, settings.checkin_concurrency, dirty, saving, draft]);
  const save = async (event: FormEvent) => {
    event.preventDefault(); setSaving(true);
    try {
      const result = await api<QueueSettings>('/queue/settings', 'PUT', values);
      setDraft(result); setDirty(false); refreshData();
      toast(`已生效：普通队列并发 ${result.max_concurrency}，签到并发 ${result.checkin_concurrency}；暂停状态保持不变`, 'success');
    } catch (caught) { toast((caught as Error).message, 'error'); } finally { setSaving(false); }
  };
  return <form className="panel concurrency-panel" onSubmit={save}><div className="concurrency-heading"><h2>并发设置</h2><p>两条队列分别限流，保存后立即生效</p></div><div className="concurrency-fields">{([{ key: 'max_concurrency', label: '队列并发' }, { key: 'checkin_concurrency', label: '签到并发' }] as const).map(item => <label className="field" key={item.key}>{item.label}<select disabled={saving} value={values[item.key]} onChange={event => { setDirty(true); setDraft({ max_concurrency: values.max_concurrency, checkin_concurrency: values.checkin_concurrency, [item.key]: Number(event.target.value) }); }}>{[1, 2, 3, 4, 5].map(value => <option value={value} key={value}>{value === 1 ? '1 个 · 依次执行' : `${value} 个 · 同时执行`}</option>)}</select></label>)}<Button variant="primary" type="submit" busy={saving}><Save size={16} />保存并发</Button></div><p className="concurrency-hint">签到默认 1 个，即使多个计划同时到点也会排队。提高并发会补充执行名额；降低并发不会强行中断已有任务。两类任务最多合计 {values.max_concurrency + values.checkin_concurrency} 个，启动间隔仍按运行设置执行。</p></form>;
}
