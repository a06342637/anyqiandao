import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent, type PointerEvent } from 'react';
import { ArrowDownToLine, BarChart3, CheckCheck, Copy, FileText, Filter, GripVertical, KeyRound, Pencil, Play, Plus, RefreshCw, ShieldCheck, Trash2, Users } from 'lucide-react';
import { api, copyText, downloadBlob, fetchApi, formatTime, refreshData, useResource } from './api';
import { Badge, Button, EmptyState, ErrorNotice, LoadingState, Modal, Pagination, RefreshButton, useConfirm, usePageClamp, usePageSize, useToast, useTrack } from './ui';
import CredentialImport from './CredentialImport';
import AccountInsights, { type InsightView } from './AccountInsights';
import { useSelection } from './selection';
import type { Account, ActionResult, PageData } from './types';

type AccountAction = 'validate' | 'checkin' | 'extract' | 'extract_invalid';

export default function AccountsPage({ timezone, onExtract, onLogs }: { timezone: string; onExtract: () => void; onLogs: (accountId: string) => void }) {
  const toast = useToast();
  const confirm = useConfirm();
  const track = useTrack();
  const [page, setPage] = useState(1);
  const [limit, setLimit] = usePageSize('accounts', 10);
  const [validity, setValidity] = useState('');
  const [onlyExtracted, setOnlyExtracted] = useState(false);
  const [repairInvalid, setRepairInvalid] = useState(true);
  const [busy, setBusy] = useState(false);
  const [extracting, setExtracting] = useState<Account | null>(null);
  const [editing, setEditing] = useState<Account | null>(null);
  const [credentialAccount, setCredentialAccount] = useState<Account | null>(null);
  const [insights, setInsights] = useState<{ account: Account; view: InsightView } | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const [order, setOrder] = useState<string[] | null>(null);
  const dragAccount = useRef<string | null>(null);
  const dragOrder = useRef<string[] | null>(null);
  const table = useRef<HTMLTableElement | null>(null);
  const pointer = useRef<{ id: number; x: number; y: number; moved: boolean } | null>(null);
  const selection = useSelection({ validity: validity || undefined, only_extracted: onlyExtracted });
  const { data, error, reload } = useResource<PageData<Account>>(`/accounts?page=${page}&limit=${limit}&validity=${validity}&only_extracted=${onlyExtracted}`);
  const total = data?.total || 0, count = selection.count(total);
  usePageClamp(page, data?.total, limit, setPage);
  useEffect(() => { setOrder(null); setDragging(null); dragAccount.current = null; dragOrder.current = null; pointer.current = null; }, [page, limit, validity, onlyExtracted]);
  const items = order ? order.map(id => data?.items.find(item => item.id === id)).filter((item): item is Account => !!item) : data?.items || [];
  const checkedPage = !!items.length && items.every(account => selection.selected(account.id));
  const canSort = !validity && !onlyExtracted && !busy;
  const run = async (operation: () => Promise<void>) => { setBusy(true); try { await operation(); refreshData(); } catch (caught) { toast((caught as Error).message, 'error'); } finally { setBusy(false); } };
  const actionLabel = (kind: AccountAction) => ({ validate: repairInvalid ? '检测并修复' : '检测', checkin: '签到', extract: '重新提取', extract_invalid: '仅重提失效' })[kind];
  const action = async (kind: AccountAction, account?: Account) => {
    const target = account ? `账号「${account.username}」` : `选中的 ${count} 个账号`;
    if (kind === 'checkin' && !await confirm({ title: `为${target}签到一次？`, message: '先验证凭证，再提交签到；有账密的账号可自动重新提取。Session 导入账号只签到，失效后保留并等待手动更新，不执行自动登录。', confirmLabel: '开始签到' })) return;
    if (kind === 'extract' && !account && count > 1 && !await confirm({ title: `重新提取所选 ${count} 个账号？`, message: '这会重新登录全部所选账号，包括凭证仍有效的账号。如果只需修复已失效 Cookie，请取消后选择“仅重提失效”。', confirmLabel: '全部重新提取' })) return;
    void run(async () => {
      const result = await api<ActionResult>('/accounts/actions', 'POST', { ...(account ? { ids: [account.id] } : selection.payload), action: kind, ...(kind === 'validate' ? { repair_invalid: repairInvalid } : {}) });
      if (result.needs_password_count && !result.job_ids?.length) throw new Error(`${result.needs_password_count} 个账号未保存密码，未执行登录；Session 账号请点“更新凭证”，其他账号可编辑补充账密`);
      if (!result.job_ids?.length) {
        toast(result.filtered_count ? `所选账号中没有标记为失效的凭证，已跳过 ${result.filtered_count} 个；可先点击“检测并修复”` : result.skipped ? `跳过 ${result.skipped} 个账号：尚未提取凭证` : '没有可执行的账号');
        return;
      }
      track(result.job_ids, { label: `${actionLabel(kind)} ${account ? account.username : `${result.job_ids.length} 个账号`}` });
      const skipped = [result.filtered_count ? `${result.filtered_count} 个非失效账号未操作` : '', result.skipped ? `${result.skipped} 个未重复入队或缺少凭证` : '', result.needs_password_count ? `${result.needs_password_count} 个未保存密码` : ''].filter(Boolean);
      if (skipped.length) toast(`新加入 ${result.queued} 个；${skipped.join('；')}`);
    });
  };
  const copyAccount = (account: Account, field: 'session' | 'api_user' | 'config') => void run(async () => {
    const credentials = await api<{ session: string; api_user: string }>(`/accounts/${account.id}/credentials`);
    const value = field === 'config' ? JSON.stringify([{ cookies: { session: credentials.session }, api_user: credentials.api_user }]) : credentials[field];
    await copyText(value); toast(`已复制 ${field === 'config' ? 'GitHub 配置（单行 JSON 数组）' : field}`, 'success');
  });
  const copyLogin = (account: Account, field: 'username' | 'password' | 'both') => void run(async () => {
    if (field === 'username') { await copyText(account.username); toast('已复制账号', 'success'); return; }
    const login = await api<{ username: string; password: string | null }>(`/accounts/${account.id}/login`);
    if (!login.password) throw new Error('该账号没有保存密码，请先编辑填写');
    await copyText(field === 'password' ? login.password : `${login.username}----${login.password}`);
    toast(field === 'password' ? '已复制密码' : '已复制 账号----密码', 'success');
  });
  const copyLogins = () => void run(async () => {
    const response = await fetchApi('/accounts/export-logins', 'POST', selection.payload);
    const value = await response.text();
    const lines = value.split('\n').filter(Boolean).length;
    if (lines > 500) { downloadBlob(new Blob([value], { type: 'text/plain;charset=utf-8' }), '账号密码.txt'); toast(`选择较大，已改为下载 ${lines} 行账号----密码`, 'success'); return; }
    await copyText(value.trimEnd()); toast(`已复制 ${lines} 行，每行 账号----密码，可直接粘贴回批量导入`, 'success');
  });
  const download = async () => {
    const result = await api<{ url: string; count: number }>('/accounts/export-link', 'POST', selection.payload);
    const anchor = document.createElement('a'); anchor.href = result.url; anchor.download = 'ANYROUTER_ACCOUNTS.json'; document.body.appendChild(anchor); anchor.click(); anchor.remove();
    toast(`开始下载 ${result.count} 个成功结果；未提取和已失效项不会混入。请妥善保管配置文件。`, 'success');
  };
  const copySelection = () => void run(async () => {
    if (count > 500) { toast('选择较大，已改用完整文件下载，不截断复制内容'); await download(); return; }
    try {
      const response = await fetchApi('/accounts/export', 'POST', selection.payload);
      const value = await response.text();
      if (new TextEncoder().encode(value).length > 1048576) { await download(); return; }
      const parsed = JSON.parse(value) as unknown[];
      await copyText(value); toast(`已复制 ${parsed.length} 个成功结果的 GitHub 配置（逗号分隔的单行 JSON 数组）；不含密码和已失效项`, 'success');
    } catch (caught) { if ((caught as { status?: number }).status === 413) await download(); else throw caught; }
  });
  const deleteSelected = async () => {
    if (!await confirm({ title: `永久删除 ${count} 个账号？`, message: '账号、保存的密码和凭证都会被删除，签到计划中的对应账号一并移除。此操作无法撤销。', confirmLabel: '删除', danger: true })) return;
    void run(async () => { const result = await api<{ removed: number }>('/accounts/delete', 'POST', selection.payload); selection.clear(); toast(`已删除 ${result.removed} 个账号`, 'success'); });
  };
  const canCopy = (account: Account) => !busy && account.has_result && account.validity !== 'invalid';
  const startDrag = (id: string) => (event: PointerEvent<HTMLButtonElement>) => {
    if (!canSort || event.button !== 0 || pointer.current) return;
    event.preventDefault();
    event.currentTarget.focus({ preventScroll: true });
    dragAccount.current = id; dragOrder.current = items.map(item => item.id);
    pointer.current = { id: event.pointerId, x: event.clientX, y: event.clientY, moved: false };
    table.current?.setPointerCapture(event.pointerId);
  };
  const moveDrag = (event: PointerEvent<HTMLTableElement>) => {
    const start = pointer.current;
    const active = dragAccount.current, currentOrder = dragOrder.current;
    if (!start || start.id !== event.pointerId || !active || !currentOrder) return;
    if (!start.moved && Math.hypot(event.clientX - start.x, event.clientY - start.y) < 6) return;
    start.moved = true;
    setDragging(active);
    event.preventDefault();
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLTableRowElement>('tr[data-account-id]');
    if (!target || !table.current?.contains(target)) return;
    const id = target.dataset.accountId || '';
    if (id === active) return;
    const from = currentOrder.indexOf(active), to = currentOrder.indexOf(id);
    if (from < 0 || to < 0) return;
    const middle = target.getBoundingClientRect().top + target.getBoundingClientRect().height / 2;
    if (from < to && event.clientY < middle || from > to && event.clientY > middle) return;
    const next = [...currentOrder]; next.splice(from, 1); next.splice(to, 0, active); dragOrder.current = next; setOrder(next);
  };
  const saveOrder = (next: string[] | null) => {
    if (!next || !data || next.join() === data.items.map(item => item.id).join()) { setOrder(null); return; }
    setOrder(next);
    void run(async () => { await api('/accounts/reorder', 'POST', { ids: next }); await reload(); toast('排序已保存', 'success'); }).finally(() => setOrder(null));
  };
  const finishDrag = (commit = false) => {
    const start = pointer.current, next = dragOrder.current;
    pointer.current = null; dragAccount.current = null; dragOrder.current = null; setDragging(null);
    if (start && table.current?.hasPointerCapture(start.id)) table.current.releasePointerCapture(start.id);
    if (commit && start?.moved) saveOrder(next); else setOrder(null);
  };
  const dropDrag = (event: PointerEvent<HTMLTableElement>) => {
    if (pointer.current?.id !== event.pointerId) return;
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('tr[data-account-id]');
    finishDrag(!!target && !!table.current?.contains(target));
  };
  const keyboardSort = (id: string) => (event: KeyboardEvent) => {
    if (event.key === 'Escape' && pointer.current) { event.preventDefault(); finishDrag(); return; }
    if (!canSort || !event.altKey || !['ArrowUp', 'ArrowDown'].includes(event.key)) return;
    event.preventDefault();
    const next = items.map(item => item.id), index = next.indexOf(id), target = index + (event.key === 'ArrowUp' ? -1 : 1);
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]]; saveOrder(next);
  };

  return <section className="panel accounts-panel"><div className="panel-heading"><div className="heading-icon"><Users size={19} /></div><div><h2>账号列表 <span className="count-pill">{data ? total : '—'}</span></h2><p>账号、密码与凭证均加密保存，直到你主动删除{canSort ? '；拖动左侧把手可排序' : ''}</p></div><div className="panel-tools"><RefreshButton onClick={reload} label="账号列表已刷新" /><Button onClick={onExtract}><Plus size={16} />添加账号</Button></div></div>
    <div className="filter-bar"><Filter size={16} /><select aria-label="筛选账号状态" value={validity} onChange={event => { setValidity(event.target.value); setPage(1); selection.clear(); }}><option value="">全部状态</option><option value="valid">有效</option><option value="invalid">已失效</option><option value="unknown">未检测</option><option value="blocked">需人工处理</option><option value="network_error">网络异常</option></select><label className="check-label"><input type="checkbox" checked={onlyExtracted} onChange={event => { setOnlyExtracted(event.target.checked); setPage(1); selection.clear(); }} />只看已提取</label><Button variant="ghost" className="push-right" onClick={() => { setValidity('valid'); setOnlyExtracted(true); setPage(1); selection.selectAll(); toast('已选择全部有效账号', 'success'); }}><CheckCheck size={16} />全选有效结果</Button></div>
    <div className={`selection-bar account-selection ${count ? 'has-selection' : ''}`}>
      <div className="selection-summary"><label className="check-label account-mobile-select"><input type="checkbox" aria-label="选择本页账号（卡片）" checked={checkedPage} onChange={event => items.forEach(account => selection.toggle(account.id, event.target.checked))} />本页</label><span>已选 <strong>{count}</strong> 个{selection.all ? ' · 跨页全选' : ' · 跨页保留选择'}</span><button className="text-button" onClick={selection.selectAll}>全选当前筛选</button>{count > 0 && <button className="text-button" onClick={selection.clear}>清空选择</button>}</div>
      <label className="check-label repair-option" title="只对保存了登录密码的账号自动重提；Session 导入账号失效时等待人工更新。检测不会顺带签到。"><input type="checkbox" checked={repairInvalid} onChange={event => setRepairInvalid(event.target.checked)} />账密账号失效时自动重提</label>
      <div className="selection-actions">
        <Button disabled={!count || busy} onClick={() => void action('validate')}><ShieldCheck size={15} />{repairInvalid ? '检测并修复' : '检测有效性'}</Button>
        <Button disabled={!count || busy} onClick={() => void action('extract_invalid')} title="只处理所选账号中已标记失效的 Cookie，其他账号保持不变"><RefreshCw size={15} />仅重提失效</Button>
        <Button disabled={!count || busy} onClick={() => void action('extract')} title="重新登录全部所选账号，包括凭证有效的账号">全部重提</Button>
        <Button disabled={!count || busy} onClick={() => void action('checkin')}><Play size={15} />签到一次</Button>
        <span className="selection-divider" aria-hidden="true" />
        <Button disabled={!count || busy} onClick={copyLogins} title="每行 账号----密码"><Copy size={15} />复制账密</Button><Button disabled={!count || busy} onClick={copySelection} title="GitHub 配置：单行 JSON 数组"><Copy size={15} />复制配置</Button><Button disabled={!count || busy} onClick={() => void run(download)} aria-label="下载所选账号配置" title="下载完整 JSON 配置"><ArrowDownToLine size={16} /></Button><Button disabled={!count || busy} variant="ghost" onClick={() => void deleteSelected()} aria-label="删除所选账号"><Trash2 size={16} /></Button>
      </div>
    </div>
    <ErrorNotice message={error} retry={reload} />
    {!data ? !error && <LoadingState label="正在读取账号列表" /> : !items.length ? <EmptyState title={validity || onlyExtracted ? '没有符合条件的账号' : '还没有账号'} description="可用账密自动提取，也可直接导入 Session 凭证后签到。" action={!validity && !onlyExtracted && <Button variant="primary" onClick={onExtract}><Plus size={16} />添加账号</Button>} /> : <div className="table-container account-layout-container"><table className="account-table account-layout" ref={table} onPointerMove={moveDrag} onPointerUp={dropDrag} onPointerCancel={() => finishDrag()} onLostPointerCapture={() => { if (pointer.current) finishDrag(); }}>
      <thead><tr><th className="check-cell serial-cell"><span>序号</span><input type="checkbox" aria-label="选择本页账号" checked={checkedPage} onChange={event => items.forEach(account => selection.toggle(account.id, event.target.checked))} /></th><th>账号</th><th>凭证与时间</th><th>余额</th><th>账号操作</th></tr></thead>
      <tbody>{items.map((account, index) => <tr key={account.id} data-account-id={account.id} className={`${selection.selected(account.id) ? 'selected-row' : ''} ${dragging === account.id ? 'dragging' : ''}`}>
        <td className="check-cell serial-cell"><span className="account-number" aria-label={`序号 ${(page - 1) * limit + index + 1}`}>{(page - 1) * limit + index + 1}</span><div className="row-selection-controls"><input type="checkbox" aria-label={`选择 ${account.username}`} checked={selection.selected(account.id)} onChange={event => selection.toggle(account.id, event.target.checked)} /><button type="button" className="drag-handle" aria-label={`排序 ${account.username}`} title="拖动排序，也可聚焦后按 Alt + ↑ / ↓；Esc 取消" disabled={!canSort} onPointerDown={startDrag(account.id)} onKeyDown={keyboardSort(account.id)}><GripVertical size={16} /></button></div></td>
        <td className="account-identity"><strong>{account.username}</strong><span className={`credential-source ${account.has_password ? 'automatic' : 'manual'}`}>{account.has_password ? '账密账号 · 自动续签' : account.has_result ? 'Session 导入 · 仅签到' : '待补充凭证'}</span>{account.note && <span className="account-note" title={account.note}>{account.note}</span>}
          <details className="account-copy-menu"><summary><Copy size={13} />复制账号与凭证</summary><div className="account-copy-tools">
            <div className="account-copy-group" aria-label={`${account.username} 的登录信息`}><span>账密</span><button className="mini-copy" disabled={busy} onClick={() => copyLogin(account, 'username')} title="复制账号">账号</button><button className="mini-copy" disabled={busy || !account.has_password} onClick={() => copyLogin(account, 'password')} title="复制密码">密码</button><button className="mini-copy" disabled={busy || !account.has_password} onClick={() => copyLogin(account, 'both')} title="复制为 账号----密码">账号----密码</button></div>
            <div className="account-copy-group" aria-label={`${account.username} 的 Cookie 凭证`}><span>凭证</span><button className="mini-copy" disabled={!canCopy(account)} onClick={() => copyAccount(account, 'session')} title="复制 session Cookie">session</button><button className="mini-copy" disabled={!canCopy(account)} onClick={() => copyAccount(account, 'api_user')} title="复制 api_user">api_user</button><button className="mini-copy" disabled={!canCopy(account)} onClick={() => copyAccount(account, 'config')} title="复制 GitHub 签到配置（单行 JSON 数组）">GitHub 配置</button></div>
          </div></details>
        </td>
        <td data-label="凭证与时间" className="account-status-cell"><Badge status={account.validity} /><small className="account-message" title={account.message}>{account.message || '等待提取或检测'}</small><div className="account-time-list"><small>更新<span>{formatTime(account.last_extracted, timezone)}</span></small><small>检测<span>{formatTime(account.last_validated, timezone)}</span></small></div></td>
        <td data-label="余额" className="account-balance"><div className="quota-line"><span className="quota-value">{account.quota !== null ? `$${account.quota.toFixed(4)}` : '待读取'}</span><button className="icon-button tiny" title="刷新余额（重新检测）" aria-label={`刷新 ${account.username} 的余额`} disabled={busy || !account.has_result} onClick={() => void action('validate', account)}><RefreshCw size={14} /></button></div><small className="date-cell" title="最近一次签到任务时间（北京时间）；具体结果请查看明细日志">上次签到 {formatTime(account.last_checkin, 'Asia/Shanghai')}</small></td>
        <td className="account-operations-cell"><div className="account-action-grid">
          <Button aria-label={`检测 ${account.username}`} disabled={busy || (!account.has_result && !(repairInvalid && account.has_password))} onClick={() => void action('validate', account)}><ShieldCheck size={13} />检测</Button>
          <Button aria-label={`签到 ${account.username}`} disabled={busy || (!account.has_result && !account.has_password)} onClick={() => void action('checkin', account)}><Play size={13} />签到</Button>
          <Button aria-label={`${account.has_password || !account.has_result ? '重新提取' : '更新凭证'} ${account.username}`} disabled={busy} onClick={() => account.has_password || !account.has_result ? setExtracting(account) : setCredentialAccount(account)}><RefreshCw size={13} />{account.has_password || !account.has_result ? '重新提取' : '更新凭证'}</Button>
          <Button aria-label={`编辑 ${account.username}`} disabled={busy} onClick={() => setEditing(account)}><Pencil size={13} />编辑</Button>
          <Button aria-label={`数据看板 ${account.username}`} disabled={busy || !account.has_result} onClick={() => setInsights({ account, view: 'dashboard' })}><BarChart3 size={13} />数据看板</Button>
          <Button aria-label={`API 令牌 ${account.username}`} disabled={busy || !account.has_result} onClick={() => setInsights({ account, view: 'tokens' })}><KeyRound size={13} />API 令牌</Button>
          <button className="text-button account-log-action" onClick={() => onLogs(account.id)} aria-label={`查看 ${account.username} 的日志`}><FileText size={13} />明细日志</button>
        </div></td>
      </tr>)}</tbody>
    </table></div>}
    {data && <Pagination page={page} total={total} limit={limit} onChange={setPage} onLimitChange={setLimit} />}<div className="panel-footnote"><ShieldCheck size={14} />账密账号可自动修复失效凭证；Session 导入账号只签到，失效后等待手动更新。看板与令牌均按需只读加载，不会触发签到。</div>
    {extracting && <Reextract account={extracting} onClose={() => setExtracting(null)} />}
    {editing && <EditAccount account={editing} onClose={() => setEditing(null)} />}
    {credentialAccount && <Modal title="更新 Session 凭证" subtitle="仅更新当前账号的 session 与 api_user，不需要登录密码。" onClose={() => setCredentialAccount(null)}><CredentialImport account={credentialAccount} onDone={() => setCredentialAccount(null)} /></Modal>}
    {insights && <AccountInsights account={insights.account} initialView={insights.view} timezone={timezone} onClose={() => setInsights(null)} />}
  </section>;
}

function Reextract({ account, onClose }: { account: Account; onClose: () => void }) {
  const toast = useToast();
  const track = useTrack();
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try {
      const result = await api<ActionResult>('/accounts/actions', 'POST', { ids: [account.id], action: 'extract', ...(password ? { password } : {}) });
      setPassword(''); refreshData();
      if (result.needs_password_count) throw new Error('该账号没有保存密码，请输入密码后再提取');
      if (result.queued) track(result.job_ids || [], { label: `重新提取 ${account.username}` }); else toast('该账号已有等待或运行中的提取任务', 'error');
      onClose();
    } catch (error) { toast((error as Error).message, 'error'); } finally { setBusy(false); }
  };
  return <Modal title="重新提取凭证" subtitle={account.has_password ? '默认使用已保存的密码；如密码已更改，在下方填写新密码。' : '该账号没有保存密码，请输入当前登录密码。'} onClose={onClose}><form onSubmit={submit} autoComplete="off"><label className="field">账号<input value={account.username} readOnly /></label><label className="field">{account.has_password ? '新密码（可选）' : '当前登录密码'}<input type="password" value={password} onChange={event => setPassword(event.target.value)} required={!account.has_password} maxLength={4096} autoComplete="new-password" placeholder={account.has_password ? '留空则使用已保存的密码' : '请输入该账号的密码'} /></label><div className="notice">新密码会加密保存并覆盖旧密码。已有凭证会保留到新凭证验证成功为止。</div><div className="modal-actions"><Button onClick={onClose}>取消</Button><Button type="submit" variant="primary" busy={busy}><RefreshCw size={16} />开始重新提取</Button></div></form></Modal>;
}

function EditAccount({ account, onClose }: { account: Account; onClose: () => void }) {
  const toast = useToast();
  const [username, setUsername] = useState(account.username);
  const [password, setPassword] = useState('');
  const [note, setNote] = useState(account.note || '');
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try {
      const result = await api<{ changed: string[] }>(`/accounts/${account.id}`, 'PUT', { username: username.trim() || undefined, password: password || undefined, note });
      refreshData();
      toast(result.changed.length ? `已保存：${result.changed.map(key => ({ username: '账号', password: '密码', note: '备注' })[key]).join('、')}` : '没有改动', 'success');
      onClose();
    } catch (error) { toast((error as Error).message, 'error'); } finally { setBusy(false); }
  };
  return <Modal title="编辑账号" subtitle="修改账号会作废旧凭证，需重新提取；只修改密码或备注不会影响现有凭证。" onClose={onClose}><form onSubmit={submit} autoComplete="off"><label className="field">账号<input value={username} onChange={event => setUsername(event.target.value)} required maxLength={512} autoComplete="off" /></label><label className="field">密码<input type="password" value={password} onChange={event => setPassword(event.target.value)} maxLength={4096} autoComplete="new-password" placeholder={account.has_password ? '留空保持不变' : '尚未保存密码，可在此填写'} /></label><label className="field">备注<input value={note} onChange={event => setNote(event.target.value)} maxLength={500} placeholder="例如：主账号、同事的、测试用" /></label><div className="modal-actions"><Button onClick={onClose}>取消</Button><Button type="submit" variant="primary" busy={busy}><Pencil size={15} />保存</Button></div></form></Modal>;
}
