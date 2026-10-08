import { useEffect, useRef, useState } from 'react';
import { BarChart3, ChevronLeft, ChevronRight, Copy, Eye, EyeOff, KeyRound, RefreshCw } from 'lucide-react';
import { api, copyText, formatTime, refreshData, useResource } from './api';
import { Badge, Button, EmptyState, ErrorNotice, Modal, useToast } from './ui';
import type { Account } from './types';

export type InsightView = 'dashboard' | 'tokens';
interface Profile { username: string; display_name: string; quota: number | null; used_quota: number | null; request_count: number | null; group: string; status: number | null; }
interface Usage { requests: number; tokens: number; cost: number; rpm: number; tpm: number; series: { label: string; cost: number; requests: number; tokens: number }[]; models: { model: string; cost: number; requests: number; tokens: number }[]; }
interface Token { id: string; name: string; key: string | null; status: number | null; used_quota: number | null; remain_quota: number | null; unlimited_quota: boolean; created_time: number | null; expired_time: number | null; accessed_time: number | null; group: string; model_limits_enabled: boolean; model_limits: string[]; allowed_ips: string[]; }
interface InsightData { view: InsightView; profile: Profile; fetched_at: number; range?: string; since?: number; until?: number; usage?: Usage | null; usage_error?: string; items?: Token[]; page?: number; limit?: number; total?: number | null; has_more?: boolean; }
interface InsightResult { status: string; message: string; paused: boolean; data: InsightData | null; }
interface InsightQueryOptions { view: InsightView; range: string; page: number; revision: number; }
interface InsightProps { account: Account; initialView: InsightView; timezone: string; onClose: () => void; }
const money = (value: number | null | undefined) => value == null ? '—' : `$${value.toFixed(4)}`;
const count = (value: number | null | undefined) => value == null ? '—' : value.toLocaleString('zh-CN');

export default function AccountInsights({ initialView, ...props }: InsightProps) {
  const [query, setQuery] = useState<InsightQueryOptions>({ view: initialView, range: 'day', page: 1, revision: 0 });
  return <InsightQuery key={`${props.account.id}:${query.view}:${query.range}:${query.page}:${query.revision}`} {...props} query={query} onChange={updates => setQuery(previous => ({ ...previous, ...updates }))} />;
}

function InsightQuery({ account, timezone, onClose, query, onChange }: Omit<InsightProps, 'initialView'> & { query: InsightQueryOptions; onChange: (updates: Partial<InsightQueryOptions>) => void; }) {
  const { view, range, page, revision } = query;
  const request = useRef<Promise<{ job_id: string }> | null>(null);
  const [jobId, setJobId] = useState('');
  const [starting, setStarting] = useState(true);
  const [requestError, setRequestError] = useState('');
  const [polling, setPolling] = useState(true);
  const resource = useResource<InsightResult>(jobId ? `/accounts/${account.id}/insights/${jobId}` : null, polling ? 2000 : 0);
  useEffect(() => {
    let mounted = true;
    request.current ??= api<{ job_id: string }>(`/accounts/${account.id}/insights`, 'POST', { view, range, page, limit: 10 });
    void request.current.then(result => {
      if (mounted) { setJobId(result.job_id); refreshData(); }
    }).catch(caught => { if (mounted) setRequestError((caught as Error).message); }).finally(() => { if (mounted) setStarting(false); });
    return () => { mounted = false; };
  }, [account.id, view, range, page]);
  const status = resource.data?.status;
  useEffect(() => { if (status && !['pending', 'running'].includes(status)) { setPolling(false); refreshData(); } }, [status]);
  const data = resource.data?.data;
  const pendingJob = !!jobId && (!status || ['pending', 'running'].includes(status));
  const loading = starting || pendingJob;
  const error = requestError || (jobId ? resource.error : '') || (status && !['pending', 'running', 'success'].includes(status) ? resource.data?.message || '查询未完成' : '');
  return <Modal title={`${account.username} · ${view === 'dashboard' ? '数据看板' : 'API 令牌'}`} subtitle="使用该账号已保存的凭证按需读取网站数据，不自动登录、不签到、不创建或修改令牌。" onClose={onClose} wide className="insights-modal">
    <div className="insight-controls"><div className="segmented inline"><button disabled={loading || view === 'dashboard'} className={view === 'dashboard' ? 'selected' : ''} onClick={() => onChange({ view: 'dashboard', page: 1 })}><BarChart3 size={15} />数据看板</button><button disabled={loading || view === 'tokens'} className={view === 'tokens' ? 'selected' : ''} onClick={() => onChange({ view: 'tokens', page: 1 })}><KeyRound size={15} />API 令牌</button></div>
      {view === 'dashboard' && <select aria-label="网站使用统计范围" disabled={loading} value={range} onChange={event => onChange({ range: event.target.value })}><option value="day">近 24 小时</option><option value="week">近 7 天</option><option value="month">近 30 天</option></select>}
      <Button disabled={loading && !resource.error} onClick={() => { if (pendingJob && resource.error) void resource.reload().catch(() => {}); else onChange({ revision: revision + 1 }); }}><RefreshCw size={15} />{pendingJob && resource.error ? '重试读取进度' : '重新加载'}</Button>
    </div>
    <ErrorNotice message={error} />
    {loading && <div className="insight-loading" role="status"><RefreshCw size={22} className="spin" /><strong>{resource.data?.paused && status === 'pending' ? '查询已排队，队列当前暂停' : status === 'running' ? '正在读取网站数据…' : '正在加入执行队列…'}</strong><p>{resource.data?.paused ? '请到执行队列继续任务；关闭此窗口不会取消排队。' : '使用代理池中为该查询指定的线路，与其他任务共用全局串行队列。'}</p></div>}
    {data && !loading && !error && <>
      <div className="insight-profile"><span><strong>{data.profile.display_name || data.profile.username || account.username}</strong><small>{data.profile.group ? `分组 ${data.profile.group}` : '用户默认分组'}</small></span><span>读取于 {formatTime(data.fetched_at, timezone)}</span></div>
      {data.view === 'dashboard' ? <DashboardData data={data} timezone={timezone} /> : <>
        <div className="notice">令牌额度只限制该令牌本身，实际调用仍受账号余额 {money(data.profile.quota)} 限制。仅展示网站已有令牌；密钥不会写入日志或浏览器本地存储。</div>
        {!data.items?.length ? <EmptyState title="此页没有 API 令牌" description="不会自动创建令牌。已有令牌将按网站顺序显示。" /> : <div className="token-list">{data.items.map(token => <TokenCard key={token.id} token={token} timezone={timezone} />)}</div>}
        <div className="token-pagination"><span>第 {data.page} 页 · 本页 {data.items?.length || 0} 个{data.total != null ? ` · 共 ${data.total} 个` : ''}</span><Button disabled={page <= 1} onClick={() => onChange({ page: page - 1 })}><ChevronLeft size={15} />上一页</Button><Button disabled={!data.has_more} onClick={() => onChange({ page: page + 1 })}>下一页<ChevronRight size={15} /></Button></div>
      </>}
    </>}
    <p className="insight-footnote">查询结果在服务器加密暂存 10 分钟，过期自动清除。失效凭证请重新提取；Session 导入账号请手动更新。</p>
  </Modal>;
}

function DashboardData({ data, timezone }: { data: InsightData; timezone: string }) {
  const usage = data.usage;
  const peak = Math.max(0.000001, ...(usage?.series.map(point => point.cost) || []));
  return <>
    <div className="insight-metrics">{[
      ['当前余额', money(data.profile.quota)], ['历史消耗', money(data.profile.used_quota)], ['历史请求次数', count(data.profile.request_count)],
      ['区间请求次数', count(usage?.requests)], ['区间统计额度', money(usage?.cost)], ['区间统计 Tokens', count(usage?.tokens)],
      ['平均 RPM', usage ? usage.rpm.toFixed(3) : '—'], ['平均 TPM', usage ? usage.tpm.toFixed(2) : '—'],
    ].map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
    <p className="form-hint">{formatTime(data.since, timezone)} — {formatTime(data.until, timezone)} · {timezone}；RPM / TPM 为所选区间每分钟平均请求数 / Tokens，不代表速率上限。</p>
    <ErrorNotice message={data.usage_error || ''} />
    {usage && <section className="insight-analysis"><h3>模型数据分析</h3>
      {!usage.series.length ? <EmptyState title="所选区间暂无使用数据" description="这是网站返回的空统计，不影响账号历史消耗及令牌列表。" /> : <>
        <div className="usage-chart" aria-label="使用额度趋势">{usage.series.map(point => <div className="usage-chart-column" key={point.label} title={`${point.label} · ${money(point.cost)} · ${point.requests} 次 · ${point.tokens} Tokens`}><div className="usage-chart-track"><div style={{ height: `${Math.max(0, point.cost) / peak * 100}%` }} /></div><small>{point.label.replace(/^\d{4}-/, '')}</small></div>)}</div>
        <div className="usage-models">{usage.models.map(model => <div key={model.model}><strong>{model.model}</strong><span>{money(model.cost)}</span><small>{count(model.requests)} 次请求 · {count(model.tokens)} Tokens</small></div>)}</div>
      </>}
    </section>}
  </>;
}

function TokenCard({ token, timezone }: { token: Token; timezone: string }) {
  const toast = useToast();
  const [visible, setVisible] = useState(false);
  const label = ({ 1: '已启用', 2: '已禁用', 3: '已过期', 4: '已耗尽' } as Record<number, string>)[token.status || 0] || '未知状态';
  const copy = async () => {
    if (!token.key) return;
    try { await copyText(token.key); toast(`已复制令牌「${token.name}」`, 'success'); } catch (caught) { toast((caught as Error).message, 'error'); }
  };
  return <article className="token-card"><header><strong>{token.name}</strong><Badge status={token.status === 1 ? 'valid' : token.status === 2 || token.status === 3 || token.status === 4 ? 'invalid' : 'unknown'} label={label} /></header>
    <div className="token-key"><code>{token.key ? visible ? token.key : `${token.key.slice(0, 7)}••••••••${token.key.slice(-4)}` : '网站未返回可复制的密钥'}</code><Button disabled={!token.key} aria-label={`${visible ? '隐藏' : '显示'}令牌 ${token.name}`} onClick={() => setVisible(!visible)}>{visible ? <EyeOff size={15} /> : <Eye size={15} />}</Button><Button disabled={!token.key} onClick={() => void copy()}><Copy size={15} />复制</Button></div>
    <dl className="token-details"><div><dt>已用额度</dt><dd>{money(token.used_quota)}</dd></div><div><dt>剩余额度</dt><dd>{token.unlimited_quota ? '无限制（仍受账号余额限制）' : money(token.remain_quota)}</dd></div><div><dt>创建时间</dt><dd>{formatTime(token.created_time, timezone)}</dd></div><div><dt>过期时间</dt><dd>{token.expired_time === -1 ? '永不过期' : formatTime(token.expired_time, timezone)}</dd></div><div><dt>令牌分组</dt><dd>{token.group || '跟随用户分组'}</dd></div><div><dt>最近使用</dt><dd>{formatTime(token.accessed_time, timezone)}</dd></div><div><dt>模型限制</dt><dd>{token.model_limits_enabled ? token.model_limits.join('、') || '已启用，未返回具体模型' : '未限制'}</dd></div><div><dt>IP 白名单</dt><dd>{token.allowed_ips.join('、') || '未限制'}</dd></div></dl>
  </article>;
}
