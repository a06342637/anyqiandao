import { useState } from 'react';
import { CalendarDays, Clock3, Coins, FileText, Gauge, ShieldCheck, TrendingUp, Trophy } from 'lucide-react';
import { formatTime, useResource } from './api';
import { Badge, Button, EmptyState, ErrorNotice, LoadingState, Pagination, RefreshButton, usePageClamp, usePageSize } from './ui';
import type { Stats } from './types';
import BalanceDetail from './BalanceDetail';
import CheckinHistory from './CheckinHistory';
import RetentionSettings from './RetentionSettings';

const RANGES = [{ id: 'day', label: '今天' }, { id: 'week', label: '近 7 天' }, { id: 'month', label: '近 30 天' }] as const;
const money = (value: number | null | undefined, digits = 2) => value === null || value === undefined ? '—' : `$${value.toFixed(digits)}`;

export default function DashboardPage({ timezone, onLogs }: { timezone: string; onLogs: (jobId: string) => void }) {
  const [range, setRange] = useState<'day' | 'week' | 'month'>('day');
  const [page, setPage] = useState(1);
  const [limit, setLimit] = usePageSize('dashboard-accounts', 10);
  const [history, setHistory] = useState<{ id: string; username: string } | 'all' | null>(null);
  const [retention, setRetention] = useState(false);
  const { data, error, reload } = useResource<Stats>(`/stats?range=${range}`, 15000);
  const label = RANGES.find(item => item.id === range)!.label;
  const peak = Math.max(0.0001, ...(data?.series.map(point => point.earned) || [1]));
  const accounts = data?.accounts || [];
  usePageClamp(page, data?.accounts.length, limit, setPage);
  const pageItems = accounts.slice((page - 1) * limit, page * limit);
  const successRate = data && data.attempts ? Math.round(((data.signed + data.already) / data.attempts) * 100) : null;

  return <>
    <section className="panel dashboard-panel">
      <div className="panel-heading"><div className="heading-icon"><Gauge size={19} /></div><div><h2>签到统计</h2><p>{label} · 北京时间（UTC+8）</p></div>
        <div className="panel-tools"><div className="segmented inline">{RANGES.map(item => <button key={item.id} className={range === item.id ? 'selected' : ''} onClick={() => { setRange(item.id); setPage(1); }}>{item.label}</button>)}</div><RefreshButton onClick={reload} label="统计已刷新" /></div></div>
      <ErrorNotice message={error} retry={reload} />
      <div className="metric-grid">
        <Metric icon={<Coins size={17} />} label={`${label}新增余额`} value={money(data?.earned, 4)} caption="新签到按固定 $25 计入，已签过不重复累计" tone="success-text" />
        <Metric icon={<TrendingUp size={17} />} label={`${label}签到次数`} value={data ? `${data.signed + data.already}` : '—'} caption={data ? `成功 ${data.signed} · 已签过 ${data.already} · 失败 ${data.failed}` : '含自动与手动签到'} />
        <Metric icon={<ShieldCheck size={17} />} label="确认成功率" value={successRate === null ? '—' : `${successRate}%`} caption={data ? `${data.attempts} 次尝试${data.uncertain ? ` · ${data.uncertain} 次待确认` : ''}` : '按尝试次数计算'} tone={successRate !== null && successRate < 80 ? 'warning-text' : ''} />
        <Metric icon={<Trophy size={17} />} label="账号总余额" value={money(data?.balance_total)} caption={data ? `${data.balance_accounts} / ${data.accounts_total} 个账号有余额数据` : '以最近一次成功读取为准'} />
      </div>
      {!!data?.legacy_records && <p className="stats-note">本区间含 {data.legacy_records} 条旧版记录，其余额差来自历史缓存，保留次数但不混入实时确认的新增余额。</p>}
      {!!data?.unmeasured && <p className="stats-note">有 {data.unmeasured} 次签到成功但余额未完整读取，金额暂不计入，不会重复发起签到。</p>}
      <p className="stats-note">每日签到按北京时间 00:00 划日，固定奖励 $25；余额消费不影响当日已签到依据，其他金额划转不直接算签到。</p>
      {!!data?.uncertain && <p className="stats-note warning-text">有 {data.uncertain} 次签到因到账依据不足、中断或超时而结果待确认，单独统计，不冒充失败或成功，也不会自动重复领取。</p>}
      {data && data.series.length > 1 && <div className={`chart${data.series.length > 14 ? ' chart-dense' : ''}`}>
        <div className="chart-bars">{data.series.map(point => <div key={point.day} className="chart-col" title={`${point.day}：新增 ${money(point.earned, 4)}，成功 ${point.signed}，已签 ${point.already}，失败 ${point.failed}，待确认 ${point.uncertain}`}>
          <div className="chart-bar-wrap"><div className="chart-bar" style={{ height: `${(point.earned / peak) * 100}%` }} /></div>
          <span className="chart-value">{point.earned ? point.earned < 0.01 ? point.earned.toFixed(4) : point.earned.toFixed(2) : point.signed + point.already ? `${point.signed + point.already}次` : ''}</span>
          <span className="chart-issues">{[point.failed > 0 ? `×${point.failed}` : '', point.uncertain > 0 ? `?${point.uncertain}` : ''].filter(Boolean).join(' · ')}</span>
          <small>{point.day}</small>
        </div>)}</div>
        <div className="chart-legend"><span><i className="swatch" />每日新增余额（$）</span><span>× 失败次数 · ? 结果待确认；悬停查看明细</span></div>
      </div>}
    </section>
    <section className="panel">
      <div className="panel-heading"><div className="heading-icon"><CalendarDays size={19} /></div><div><h2>账号明细 <span className="count-pill">{data ? accounts.length : '—'}</span></h2><p>按{label}新增余额排序；余额为该账号最近一次成功读取的值</p></div><div className="panel-tools"><Button onClick={() => setHistory('all')}><FileText size={15} />管理明细日志</Button><Button onClick={() => setRetention(true)}><Clock3 size={15} />日志保留</Button></div></div>
      {!data ? !error && <LoadingState label="正在读取签到统计" /> : !pageItems.length ? <EmptyState title="还没有账号" description="添加账号后，可在签到管理中执行或安排签到；这里会展示每个账号的余额与统计。" /> : <div className="table-container stats-container"><table className="stats-table"><thead><tr><th>账号</th><th>{label}新增</th><th>签到 / 已签 / 失败</th><th>当前余额 / 最近签到变化</th><th>最近签到 / 日志</th></tr></thead><tbody>{pageItems.map(row => <tr key={row.account_id}><td><strong>{row.username}</strong>{row.note && <small>{row.note}</small>}</td><td className={row.earned > 0 ? 'success-text' : 'muted'}>{row.earned > 0 ? `+${money(row.earned, 4)}` : '—'}</td><td className="tabular">{row.signed} / {row.already} / <span className={row.failed ? 'warning-text' : ''}>{row.failed}</span>{row.uncertain > 0 && <small className="warning-text">{row.uncertain} 次待确认</small>}</td><td className="tabular">{money(row.quota, 4)}<BalanceDetail balance={row.last_balance} /></td><td>{row.last_code ? <Badge status={row.last_code} /> : <span className="muted">尚未签到</span>}<small>{formatTime(row.last, timezone)}</small><button className="text-button" onClick={() => setHistory({ id: row.account_id, username: row.username })}><FileText size={13} />明细日志</button></td></tr>)}</tbody></table></div>}
      {data && <Pagination page={page} total={accounts.length} limit={limit} onChange={setPage} onLimitChange={setLimit} />}
      <div className="panel-footnote"><ShieldCheck size={14} />签到统计保留 {data?.stats_retention_days ?? 90} 天，可在设置中调整；清理明细仅影响历史统计，不清除每日签到依据和账号凭证。</div>
    </section>
    {history && <CheckinHistory account={history === 'all' ? undefined : history} timezone={timezone} onClose={() => setHistory(null)} onLogs={onLogs} />}
    {retention && <RetentionSettings onClose={() => setRetention(false)} />}
  </>;
}

function Metric({ icon, label, value, caption, tone = '' }: { icon: React.ReactNode; label: string; value: string; caption: string; tone?: string }) {
  return <div className="metric"><div className="stat-label">{label}{icon}</div><div className={`metric-value ${tone}`}>{value}</div><span className="stat-caption">{caption}</span></div>;
}
