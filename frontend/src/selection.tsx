import { useState } from 'react';
import type { Account, PageData, Provider, Selection } from './types';
import { useResource } from './api';
import { Badge, Button, EmptyState, ErrorNotice, LoadingState, Pagination, usePageClamp, usePageSize } from './ui';

export function useSelection(filter: Pick<Selection, 'validity' | 'only_extracted' | 'needs_confirmation' | 'provider'> = {}) {
  const [ids, setIds] = useState<Set<string>>(new Set());
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [all, setAll] = useState(false);
  const selected = (id: string) => all ? !excluded.has(id) : ids.has(id);
  const toggle = (id: string, checked: boolean) => {
    if (all) setExcluded(previous => { const next = new Set(previous); if (checked) next.delete(id); else next.add(id); return next; });
    else setIds(previous => { const next = new Set(previous); if (checked) next.add(id); else next.delete(id); return next; });
  };
  const clear = () => { setAll(false); setIds(new Set()); setExcluded(new Set()); };
  const selectAll = () => { setAll(true); setIds(new Set()); setExcluded(new Set()); };
  const payload: Selection = { ...filter, all_matching: all, ids: all ? [] : [...ids], exclude_ids: all ? [...excluded] : [] };
  return { selected, toggle, clear, selectAll, all, payload, count: (total: number) => Math.max(0, all ? total - excluded.size : ids.size) };
}

export function AccountPicker({ onChange }: { onChange: (selection: Selection) => void }) {
  const [page, setPage] = useState(1);
  const [provider, setProvider] = useState<Provider | ''>('');
  const [limit, setLimit] = usePageSize('schedule-account-picker', 10);
  const resource = useResource<PageData<Account>>(`/accounts?page=${page}&limit=${limit}&only_extracted=true&provider=${provider}`);
  usePageClamp(page, resource.data?.total, limit, setPage);
  const filter = { only_extracted: true, provider: provider || undefined };
  const selection = useSelection(filter);
  const total = resource.data?.total || 0;
  const ready = !!resource.data && !resource.error;
  const category = provider === 'agentrouter' ? 'AgentRouter' : provider === 'anyrouter' ? 'AnyRouter' : '全部分类';
  const changeProvider = (value: Provider | '') => {
    setProvider(value); setPage(1); selection.clear();
    onChange({ ids: [], only_extracted: true, provider: value || undefined });
  };
  const toggle = (id: string, checked: boolean) => {
    selection.toggle(id, checked);
    const next = { ...selection.payload };
    if (selection.all) next.exclude_ids = checked ? (next.exclude_ids || []).filter(value => value !== id) : [...(next.exclude_ids || []), id];
    else next.ids = checked ? [...(next.ids || []), id] : (next.ids || []).filter(value => value !== id);
    onChange(next);
  };
  return <div className="account-picker"><div className="picker-heading"><strong>选择签到账号</strong><span aria-live="polite">已选 {selection.count(total)} 个</span></div>
    <div className="picker-toolbar"><label className="picker-platform">账号分类<select aria-label="筛选计划账号分类" value={provider} onChange={event => changeProvider(event.target.value as Provider | '')}><option value="">全部分类</option><option value="anyrouter">AnyRouter</option><option value="agentrouter">AgentRouter</option></select></label><div className="picker-actions"><Button disabled={!ready || !total} onClick={() => { selection.selectAll(); onChange({ ...filter, all_matching: true, ids: [], exclude_ids: [] }); }}>全选当前分类</Button><Button variant="ghost" onClick={() => { selection.clear(); onChange({ ...filter, ids: [] }); }}>清空</Button></div></div>
    <p className="picker-scope" aria-live="polite">{category} · {resource.data ? `${total} 个已提取账号` : '正在加载'}{selection.all ? ' · 已跨页全选' : ''}</p>
    <ErrorNotice message={resource.error} retry={resource.reload} />
    {!resource.data ? !resource.error && <LoadingState label="正在读取分类账号" /> : !resource.data.items.length ? <EmptyState title="当前分类没有可选账号" description="请先添加该平台账号并完成凭证提取。" /> : <div className="picker-list" key={`${provider}:${page}:${limit}`}>{resource.data.items.map(account => <label className="picker-row" key={account.id}><input type="checkbox" aria-label={`选择计划账号 ${account.provider === 'agentrouter' ? 'AgentRouter' : 'AnyRouter'} ${account.username}`} disabled={!ready} checked={selection.selected(account.id)} onChange={event => toggle(account.id, event.target.checked)} /><span className="picker-identity"><span className="ellipsis" title={account.username}>{account.username}</span><small>{account.provider === 'agentrouter' ? 'AgentRouter' : 'AnyRouter'}</small></span><Badge status={account.validity} /></label>)}</div>}
    {resource.data && <Pagination page={page} total={total} limit={limit} onChange={setPage} onLimitChange={setLimit} />}<p className="form-hint">全选覆盖当前分类的所有分页；切换分类会清空选择。账号按各自平台规则签到。</p></div>;
}
