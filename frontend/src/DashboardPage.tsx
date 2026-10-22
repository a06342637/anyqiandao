import { useState } from 'react';
import { CircleHelp, Coins, Gauge, ShieldCheck, TrendingUp } from 'lucide-react';
import { useResource } from './api';
import { ErrorNotice, RefreshButton } from './ui';
import type { Provider, Stats } from './types';

const RANGES = [{ id: 'day', label: '今天' }, { id: 'week', label: '近 7 天' }, { id: 'month', label: '近 30 天' }] as const;
const money = (value: number | null | undefined, digits = 2) => value === null || value === undefined ? '—' : `$${value.toFixed(digits)}`;

export default function DashboardPage() {
  return <div className="provider-statistics"><ProviderStatistics provider="agentrouter" /><ProviderStatistics provider="anyrouter" /></div>;
}

function ProviderStatistics({ provider }: { provider: Provider }) {
  const [range, setRange] = useState<'day' | 'week' | 'month'>('day');
  const { data, error, reload } = useResource<Stats>(`/stats?range=${range}&provider=${provider}`, 15000);
  const platform = provider === 'agentrouter' ? 'AgentRouter' : 'AnyRouter';
  const label = RANGES.find(item => item.id === range)!.label;
  const peak = Math.max(0.0001, ...(data?.series.map(point => point.earned) || [1]));
  const successRate = data && data.attempts ? Math.round((data.signed / data.attempts) * 100) : null;

  return <section className="panel dashboard-panel" data-provider={provider} aria-label={`${platform} 签到统计`}>
      <div className="panel-heading"><div className="heading-icon"><Gauge size={19} /></div><div><h2>{platform} 签到统计</h2><p>{label} · 北京时间（UTC+8）</p></div>
        <div className="panel-tools"><div className="segmented inline" role="group" aria-label={`${platform} 统计范围`}>{RANGES.map(item => <button type="button" key={item.id} aria-pressed={range === item.id} className={range === item.id ? 'selected' : ''} onClick={() => setRange(item.id)}>{item.label}</button>)}</div><RefreshButton onClick={reload} label={`${platform} 统计已刷新`} /></div></div>
      <ErrorNotice message={error} retry={reload} />
      <div className="metric-grid">
        <Metric icon={<Coins size={17} />} label={`${label}签到新增`} value={money(data?.earned, 4)} caption="仅累计签到前后核实的新增金额" tone="success-text" />
        <Metric icon={<TrendingUp size={17} />} label={`${label}签到次数`} value={data ? `${data.signed}` : '—'} caption={data ? `成功 ${data.signed} · 未到账 ${data.no_credit} · 失败 ${data.failed} · 人工确认 ${data.manual}` : '含自动与手动签到'} />
        <Metric icon={<ShieldCheck size={17} />} label="到账成功率" value={successRate === null ? '—' : `${successRate}%`} caption={data ? `${data.attempts} 次尝试${data.uncertain ? ` · ${data.uncertain} 次待确认` : ''}` : '按尝试次数计算'} tone={successRate !== null && successRate < 80 ? 'warning-text' : ''} />
        <Metric icon={<CircleHelp size={17} />} label="待确认次数" value={data ? `${data.uncertain + data.unmeasured}` : '—'} caption="金额未核实，暂不计入签到新增" />
      </div>
      {!!data?.legacy_records && <p className="stats-note">本区间含 {data.legacy_records} 条旧版记录，其签到前余额无法核实，保留历史和次数，金额不计入签到新增。</p>}
      {!!data?.unmeasured && <p className="stats-note">有 {data.unmeasured} 次签到成功但余额未完整读取，金额暂不计入，不会重复发起签到。</p>}
      <p className="stats-note">统计按北京时间划日，领取资格由网站决定。只有本轮余额变化加回期间消耗后核实到账，才计为成功；历史到账和人工确认不代替本轮结果。</p>
      {!!data?.no_credit && <p className="stats-note warning-text">有 {data.no_credit} 次请求已完成但没有到账，未计入成功；可以在账号列表筛选并重新核验。</p>}
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
    </section>;
}

function Metric({ icon, label, value, caption, tone = '' }: { icon: React.ReactNode; label: string; value: string; caption: string; tone?: string }) {
  return <div className="metric"><div className="stat-label">{label}{icon}</div><div className={`metric-value ${tone}`}>{value}</div><span className="stat-caption">{caption}</span></div>;
}
