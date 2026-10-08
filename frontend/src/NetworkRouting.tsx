import { useState, type FormEvent } from 'react';
import { Network, Save } from 'lucide-react';
import { api, refreshData, useResource } from './api';
import { Button, ErrorNotice, Pagination, useToast } from './ui';
import type { Account, NetworkRoute, OperationRoutes, PageData, ProxyNode, SettingsData } from './types';

export const OPERATIONS = [
  ['extract', '登录 / 提取 Cookie'], ['refresh', '更新 / 自动修复凭证'], ['validate', '检测凭证'],
  ['checkin', '签到默认线路'], ['tokens', 'API 令牌查询'], ['dashboard', '数据看板'],
] as const;

export function RouteSelect({ value, onChange, proxies, inherit = false, disabled = false, label = '网络线路' }: {
  value: NetworkRoute; onChange: (route: NetworkRoute) => void; proxies: ProxyNode[]; inherit?: boolean; disabled?: boolean; label?: string;
}) {
  const selected = value.mode === 'proxy' ? value.proxy_id || '' : value.mode;
  return <select aria-label={label} disabled={disabled} value={selected} onChange={event => onChange(event.target.value === 'direct' || event.target.value === 'inherit' ? { mode: event.target.value } : { mode: 'proxy', proxy_id: event.target.value })}>
    {inherit && <option value="inherit">跟随账号 / 默认设置</option>}<option value="direct">直连（不使用代理）</option>
    {value.mode === 'proxy' && !proxies.some(proxy => proxy.id === value.proxy_id) && <option value={value.proxy_id || ''}>原代理已不存在，请重新选择</option>}
    {proxies.map(proxy => <option key={proxy.id} value={proxy.id} disabled={!proxy.enabled}>{proxy.name} · {proxy.scheme}{!proxy.enabled ? '（已停用）' : ''}</option>)}
  </select>;
}

export function OperationRouting() {
  const { data, error, reload } = useResource<SettingsData>('/settings', 0);
  const nodes = useResource<PageData<ProxyNode>>('/proxies?limit=100', 0);
  const [draft, setDraft] = useState<OperationRoutes | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const save = async (event: FormEvent) => {
    event.preventDefault(); if (!draft) return; setBusy(true);
    try { await api('/settings/routes', 'PUT', draft); setDraft(null); refreshData(); toast('各操作线路已保存，对等待和后续任务生效', 'success'); }
    catch (error) { toast((error as Error).message, 'error'); } finally { setBusy(false); }
  };
  const values = draft || data?.settings.operation_routes;
  return <section className="settings-section"><h3><Network size={16} /> 按操作选择线路</h3><p className="form-hint">添加代理不会自动使用。每个操作默认直连，只有明确选择节点才走代理。网络失败尝试该节点最多 5 次，再转直连；验证码、凭证错误或已提交但结果不明的签到不会反复重发。</p><ErrorNotice message={error || nodes.error} retry={reload} />
    {values && <form onSubmit={save}><div className="form-grid">{OPERATIONS.map(([key, label]) => <label className="field" key={key}>{label}<RouteSelect value={values[key]} proxies={nodes.data?.items || []} disabled={busy} label={label} onChange={route => setDraft({ ...values, [key]: route })} /></label>)}</div>
      <p className="form-hint">登录与 Cookie 提取属于同一浏览器会话；手动粘贴 Session / api_user 只保存数据，不访问外网。签到计划与账号还可在“签到管理”指定线路。</p><Button type="submit" variant="primary" busy={busy} disabled={!draft || busy}><Save size={15} />保存操作线路</Button></form>}
  </section>;
}

export function AccountCheckinRouting() {
  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState('');
  const { data, error, reload } = useResource<PageData<Account>>(`/accounts?page=${page}&limit=10`, 0);
  const nodes = useResource<PageData<ProxyNode>>('/proxies?limit=100', 0);
  const toast = useToast();
  const save = async (account: Account, network_route: NetworkRoute) => {
    setBusy(account.id);
    try { await api('/accounts/routes/checkin', 'PUT', { ids: [account.id], network_route }); refreshData(); toast('该账号的签到线路已保存', 'success'); }
    catch (error) { toast((error as Error).message, 'error'); } finally { setBusy(''); }
  };
  return <section className="panel"><div className="panel-heading"><div className="heading-icon"><Network size={18} /></div><div><h2>账号签到线路</h2><p>手动签到和跟随默认设置的计划使用此线路；计划显式指定线路时以计划为准。</p></div></div><ErrorNotice message={error || nodes.error} retry={reload} />
    <div className="job-list">{data?.items.map(account => <div className="job-row" key={account.id}><div className="job-description"><strong>{account.username}</strong><p>{account.validity === 'invalid' ? '凭证失效时，按“更新凭证”线路修复后继续' : '未指定时使用代理池设置中的签到默认线路'}</p></div><div style={{ width: 250, maxWidth: '55%' }}><RouteSelect inherit value={account.checkin_route || { mode: 'inherit' }} label={`${account.username} 签到线路`} proxies={nodes.data?.items || []} disabled={!!busy} onChange={route => void save(account, route)} /></div></div>)}</div>
    <Pagination page={page} total={data?.total || 0} limit={10} onChange={setPage} />
  </section>;
}
