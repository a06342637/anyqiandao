import { useState } from 'react';
import type { Account, PageData, Selection } from './types';
import { useResource } from './api';
import { Badge, Button, EmptyState, ErrorNotice, Pagination, usePageClamp, usePageSize } from './ui';

export function useSelection(filter: Pick<Selection, 'validity' | 'only_extracted'> = {}) {
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
  const [limit, setLimit] = usePageSize('schedule-account-picker', 10);
  const resource = useResource<PageData<Account>>(`/accounts?page=${page}&limit=${limit}&only_extracted=true`);
  usePageClamp(page, resource.data?.total, limit, setPage);
  const selection = useSelection({ only_extracted: true });
  const toggle = (id: string, checked: boolean) => {
    selection.toggle(id, checked);
    const next = { ...selection.payload };
    if (selection.all) next.exclude_ids = checked ? (next.exclude_ids || []).filter(value => value !== id) : [...(next.exclude_ids || []), id];
    else next.ids = checked ? [...(next.ids || []), id] : (next.ids || []).filter(value => value !== id);
    onChange(next);
  };
  return <div className="account-picker"><div className="picker-heading"><strong>选择签到账号</strong><span>已选 {selection.count(resource.data?.total || 0)} 个</span><Button variant="ghost" onClick={() => { selection.selectAll(); onChange({ all_matching: true, only_extracted: true }); }}>全选已提取账号</Button><Button variant="ghost" onClick={() => { selection.clear(); onChange({ ids: [] }); }}>清空</Button></div><ErrorNotice message={resource.error} retry={resource.reload} />
    {!resource.data?.items.length ? <EmptyState title="还没有可选账号" description="请先提取凭证，再创建签到计划。" /> : <div className="picker-list">{resource.data.items.map(account => <label className="picker-row" key={account.id}><input type="checkbox" checked={selection.selected(account.id)} onChange={event => toggle(account.id, event.target.checked)} /><span className="ellipsis">{account.username}</span><Badge status={account.validity} /></label>)}</div>}
    <Pagination page={page} total={resource.data?.total || 0} limit={limit} onChange={setPage} onLimitChange={setLimit} /><p className="form-hint">选择跨页保留。失效凭证会按设置尝试自动重新提取；重提失败或未保存密码时停止该账号本轮签到，并记录原因。</p></div>;
}
