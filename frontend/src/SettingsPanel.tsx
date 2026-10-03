import { useState, type FormEvent } from 'react';
import { ArrowLeft, Check, Clock3, Code2, DatabaseBackup, LockKeyhole, Network, Palette, Pencil, Play, Plus, Save, RefreshCw, Settings2, Sparkles, Trash2, Upload } from 'lucide-react';
import UpdatePanel from './UpdatePanel';
import RemoteBackupPanel from './RemoteBackupPanel';
import AppearancePanel from './AppearancePanel';
import { cleanupSummary } from './RetentionSettings';
import { parseProxyLine } from './proxyParse';
import { api, downloadBlob, fetchApi, formatTime, refreshData, useResource } from './api';
import { Badge, Button, EmptyState, ErrorNotice, Modal, Pagination, SealMark, useBranding, useConfirm, useToast, useTrack } from './ui';
import type { CleanupResult, PageData, ProxyNode, RuntimeSettings, SettingsData } from './types';

const settingsTabs = [
  { id: 'runtime', label: '运行与日志', icon: Settings2, description: '网络连接、自动续签与日志保留策略。' },
  { id: 'appearance', label: '站点外观', icon: Palette, description: '让这个工作空间拥有你的名字。' },
  { id: 'proxies', label: '代理池', icon: Network, description: '管理登录与签到使用的网络出口。' },
  { id: 'backup', label: '备份与恢复', icon: DatabaseBackup, description: '安全保留账号、凭证、计划和历史数据。' },
  { id: 'remote-backup', label: '远程自动备份', icon: DatabaseBackup, description: '定时备份到阿里云 OSS 或 SSH / SFTP 服务器。' },
  { id: 'updates', label: '版本更新', icon: RefreshCw, description: '检测新版，备份后安全更新。' },
  { id: 'about', label: '关于', icon: Code2, description: '属于你自己的签到工作空间。' },
] as const;

export default function SettingsPanel({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<typeof settingsTabs[number]['id']>('runtime');
  const branding = useBranding();
  const { data, error, reload } = useResource<SettingsData>('/settings', 0);
  const current = settingsTabs.find(item => item.id === tab)!;
  return <Modal title="设置" subtitle="管理你的工作空间" onClose={onClose} wide className="settings-modal">
    <div className="settings-layout">
      <nav className="settings-nav" aria-label="设置分类">{settingsTabs.map(item => <button key={item.id} className={tab === item.id ? 'active' : ''} aria-current={tab === item.id ? 'page' : undefined} onClick={() => setTab(item.id)}><item.icon size={17} /><span>{item.label}</span></button>)}<span className="settings-nav-version">{branding.site_name}<br />v{data?.version || __APP_VERSION__}</span></nav>
      <div className="settings-content" key={tab}>
        <div className="settings-content-heading"><h3>{current.label}</h3><p>{current.description}</p></div>
        <ErrorNotice message={error} retry={reload} />
        {tab === 'runtime' && (data ? <RuntimeForm initial={data} /> : <p className="muted">正在读取设置…</p>)}
        {tab === 'appearance' && (data ? <AppearancePanel initial={data.settings} /> : <p className="muted">正在读取外观设置…</p>)}
        {tab === 'proxies' && <ProxyManager />}
        {tab === 'backup' && <BackupPanel />}
        {tab === 'remote-backup' && <RemoteBackupPanel />}
        {tab === 'updates' && <UpdatePanel />}
        {tab === 'about' && <div className="about-panel"><SealMark size="large" /><h3>{branding.site_name} <span>v{data?.version || __APP_VERSION__}</span></h3><p>私有部署的 AnyRouter 凭证提取、账号检测与定时签到工作空间。</p><div className="about-features"><span><LockKeyhole size={17} />AES-256-GCM 字段加密</span><span><Network size={17} />HTTP / SOCKS5 / SSH 代理</span><span><Clock3 size={17} />普通与签到独立并发</span><span><Sparkles size={17} />失效凭证自动续签</span></div><div className="about-changelog"><h4>v{__APP_VERSION__} · 执行队列与三端布局</h4><p>独立队列页面支持暂停、删除任务、实时刷新及分别设置并发。签到默认依次执行，所有计划共享签到队列。</p><p>每次提取与签到在日志中记录直连或所用代理；Cookie 失效后自动重新提取并继续本次签到。</p></div></div>}
      </div>
    </div>
  </Modal>;
}

function RuntimeForm({ initial }: { initial: SettingsData }) {
  const toast = useToast();
  const confirm = useConfirm();
  const [settings, setSettings] = useState<RuntimeSettings>(initial.settings);
  const [busy, setBusy] = useState(false);
  const update = <Key extends keyof RuntimeSettings>(key: Key, value: RuntimeSettings[Key]) => setSettings(previous => ({ ...previous, [key]: value }));
  const save = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try { await api('/settings', 'PUT', settings); refreshData(); toast('设置已保存，对后续任务生效；暂停的队列请手动继续', 'success'); }
    catch (error) { toast((error as Error).message, 'error'); } finally { setBusy(false); }
  };
  const cleanup = async () => {
    if (!await confirm({ title: '立即清理到期日志？', message: '按当前已保存的策略删除到期日志、已完成的任务记录和到期的签到统计。账号、密码与凭证不受影响。', confirmLabel: '清理' })) return;
    setBusy(true);
    try { const result = await api<CleanupResult>('/logs/cleanup', 'POST'); refreshData(); toast(cleanupSummary(result), 'success'); }
    catch (error) { toast((error as Error).message, 'error'); } finally { setBusy(false); }
  };
  return <form className="runtime-form" onSubmit={save}>
    <section className="settings-section"><h3>网络连接</h3>
      <label className="field">运行模式<select value={settings.proxy_mode} onChange={event => { const value = event.target.value as 'pool' | 'direct'; if (value !== 'direct') { update('proxy_mode', value); return; } void confirm({ title: '启用直连模式？', message: '登录、检测和签到会直接使用服务器自己的网络出口，不经过任何代理。', confirmLabel: '使用直连' }).then(ok => { if (ok) update('proxy_mode', 'direct'); }); }}><option value="pool">代理池模式（不自动降级直连）</option><option value="direct">直连模式（需明确选择）</option></select></label>
      <div className="form-grid three"><NumberField label="连接超时（秒）" value={settings.connect_timeout} min={3} max={120} onChange={value => update('connect_timeout', value)} /><NumberField label="登录总时限（秒）" value={settings.login_timeout} min={15} max={300} onChange={value => update('login_timeout', value)} /><NumberField label="任务启动间隔（秒）" value={settings.account_gap} min={0} max={60} onChange={value => update('account_gap', value)} /></div>
      <p className="form-hint">普通任务与签到的并发数量请在「执行队列」设置。代理全部不可用时暂停队列，不会自动改为直连。</p>
      <label className="field">显示时区<select value={settings.timezone} onChange={event => update('timezone', event.target.value)}>{['Asia/Seoul', 'Asia/Shanghai', 'Asia/Tokyo', 'UTC', 'America/Los_Angeles'].map(zone => <option key={zone}>{zone}</option>)}</select></label>
    </section>
    <section className="settings-section"><h3><Sparkles size={15} /> 自动处理</h3>
      <label className="check-label setting-toggle"><input type="checkbox" checked={settings.auto_checkin} onChange={event => update('auto_checkin', event.target.checked)} /><span><strong>提取后自动签到</strong><small>提取成功后签到一次，并加入“自动签到（默认）”计划。</small></span></label>
      <label className="check-label setting-toggle"><input type="checkbox" checked={settings.auto_reextract} onChange={event => update('auto_reextract', event.target.checked)} /><span><strong>Cookie 失效后自动重新提取</strong><small>使用已保存的密码重新登录，成功后继续本次签到；遇到人工验证会停止并记录日志。</small></span></label>
      <p className="form-hint">签到时间与间隔统一在「签到管理」设置，保存这里的参数不会改动已有计划。</p>
    </section>
    <section className="settings-section"><h3>日志保留</h3>
      <label className="check-label"><input type="checkbox" checked={settings.log_cleanup_enabled} onChange={event => update('log_cleanup_enabled', event.target.checked)} />自动清理到期日志</label>
      <div className="form-grid"><NumberField label="运行 / 账号日志保留天数" value={settings.log_retention_days} min={1} max={3650} onChange={value => update('log_retention_days', value)} /><NumberField label="已结束队列记录保留天数" value={settings.queue_retention_days} min={1} max={3650} onChange={value => update('queue_retention_days', value)} /><NumberField label="每隔多少小时清理" value={settings.log_cleanup_hours} min={1} max={168} onChange={value => update('log_cleanup_hours', value)} /><NumberField label="账号明细 / 签到统计保留天数" value={settings.stats_retention_days} min={1} max={3650} onChange={value => update('stats_retention_days', value)} /></div>
      <div className="settings-cleanup"><span className="form-hint">上次清理：{formatTime(initial.last_log_cleanup, settings.timezone)}<br />下次清理：{initial.settings.log_cleanup_enabled ? formatTime(initial.next_log_cleanup, settings.timezone) : '自动清理已关闭'}（按已保存设置）<br />覆盖运行日志（含备份日志）、账号明细和已结束队列；不影响账号凭证、等待/运行任务及其日志。</span><Button disabled={busy} onClick={cleanup}><Trash2 size={15} />立即清理到期记录</Button></div>
    </section>
    <div className="settings-save"><span className="form-hint">仅保存本页运行参数</span><Button type="submit" variant="primary" busy={busy}><Save size={16} />保存设置</Button></div>
  </form>;
}

function NumberField({ label, value, min, max, onChange }: { label: string; value: number; min: number; max: number; onChange: (value: number) => void }) {
  return <label className="field">{label}<input type="number" min={min} max={max} step={1} required value={value} onChange={event => onChange(Number(event.target.value))} /></label>;
}

function ProxyManager() {
  const toast = useToast();
  const confirm = useConfirm();
  const track = useTrack();
  const [page, setPage] = useState(1);
  const [editing, setEditing] = useState<ProxyNode | null | undefined>(undefined);
  const [bulk, setBulk] = useState(false);
  const [lines, setLines] = useState('');
  const [busy, setBusy] = useState(false);
  const [trusting, setTrusting] = useState<ProxyNode | null>(null);
  const [verified, setVerified] = useState(false);
  const { data, error, reload } = useResource<PageData<ProxyNode>>(`/proxies?page=${page}&limit=20`);
  const run = async (operation: () => Promise<void>) => { setBusy(true); try { await operation(); refreshData(); } catch (caught) { toast((caught as Error).message, 'error'); } finally { setBusy(false); } };
  const test = (node?: ProxyNode) => void run(async () => { const result = await api<{ queued: number; job_ids: string[] }>('/proxies/test', 'POST', node ? { ids: [node.id] } : { all_matching: true }); if (!result.job_ids?.length) { toast('没有可测试的节点', 'error'); return; } track(result.job_ids, { label: node ? `测试代理 ${node.name}` : `测试 ${result.job_ids.length} 个代理` }); });
  const toggle = (node: ProxyNode) => void run(async () => { await api(`/proxies/${node.id}`, 'PUT', { name: node.name, scheme: node.scheme, host: node.host, port: node.port, username: node.username, password: null, enabled: !node.enabled }); toast(node.enabled ? '节点已停用' : '节点已启用', 'success'); });
  const remove = async (node: ProxyNode) => { if (await confirm({ title: `删除代理「${node.name}」？`, message: '节点及其加密保存的认证信息会被删除。正在使用该节点的任务不受影响。', confirmLabel: '删除', danger: true })) void run(async () => { await api(`/proxies/${node.id}`, 'DELETE'); toast('代理已删除', 'success'); }); };
  const importNodes = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const values = lines.split(/\r?\n/).filter(line => line.trim());
      if (!values.length) throw new Error('请先输入代理');
      let inserted = 0;
      const jobIds: string[] = [];
      for (let offset = 0; offset < values.length; offset += 100) {
        const result = await api<{ inserted: number; job_ids: string[]; errors: { line: number; message: string }[] }>('/proxies/import', 'POST', { lines: values.slice(offset, offset + 100) });
        inserted += result.inserted;
        jobIds.push(...result.job_ids);
        if (result.errors.length) { track(jobIds, { label: `测试 ${inserted} 个新代理` }); throw new Error(`已加入 ${inserted} 个节点；第 ${offset + result.errors[0].line} 行：${result.errors[0].message}。请仅重新提交错误行。`); }
      }
      setLines(''); setBulk(false); track(jobIds, { label: `测试 ${inserted} 个新代理` });
    });
  };
  if (editing !== undefined) return <ProxyEditor node={editing} onClose={() => setEditing(undefined)} />;
  return <div className="proxy-manager"><div className="proxy-toolbar"><Button variant="primary" onClick={() => setEditing(null)}><Plus size={15} />添加代理</Button><Button onClick={() => setBulk(!bulk)}>批量添加</Button><Button className="push-right" disabled={busy} onClick={() => test()}><Play size={14} />测试全部</Button></div><div className="notice compact-notice"><Network size={17} /><span>支持 HTTP、SOCKS5 / s5、SSH。不凭端口猜协议；未知协议只做无凭证探测。</span></div>
    {bulk && <form onSubmit={importNodes} className="proxy-import"><label className="field">每行一个代理<textarea rows={5} value={lines} onChange={event => setLines(event.target.value)} required autoComplete="off" spellCheck={false} placeholder={'http://user:password@host:8080\nsocks5://user:password@host:1080\nssh://user:password@host:22\nhost 端口 用户名 密码'} /></label><p className="form-hint">URL 内的 @、#、: 等特殊字符应百分号编码。复杂密码建议使用单节点的四字段表单。</p><Button type="submit" busy={busy}>添加并测试</Button></form>}
    <ErrorNotice message={error} retry={reload} />
    {!data?.items.length ? <EmptyState title="添加你的第一个代理" description="只使用你有权使用的节点。SSH 仅做 TCP 转发，不执行远程命令。" /> : <div className="proxy-list">{data.items.map(node => <article className={`proxy-card ${node.enabled ? '' : 'disabled-node'}`} key={node.id}><div className="proxy-card-heading"><span className="proxy-type">{node.scheme === 'auto' ? '待指定' : node.scheme.toUpperCase()}</span><strong>{node.name}</strong><Badge status={node.status} /><span className="push-right proxy-latency">{node.latency !== null ? `${Math.round(node.latency)} ms` : '未测速'}</span></div><p className="proxy-address">{node.host}:{node.port}{node.username ? ` · ${node.username}` : ''}{node.has_password ? ' · 已加密保存密码' : ''}</p>{node.message && <p className="proxy-message">{node.message}</p>}<div className="proxy-actions"><label className="check-label"><input type="checkbox" checked={!!node.enabled} disabled={busy} onChange={() => toggle(node)} />启用</label><Button variant="ghost" disabled={busy} onClick={() => test(node)}>测试</Button><Button variant="ghost" disabled={busy} onClick={() => setEditing(node)}><Pencil size={13} />编辑</Button>{node.candidate_fingerprint && <Button disabled={busy} onClick={() => { setTrusting(node); setVerified(false); }}>核对 SSH 指纹</Button>}<button className="icon-button small push-right" disabled={busy} aria-label={`删除代理 ${node.name}`} onClick={() => void remove(node)}><Trash2 size={15} /></button></div></article>)}</div>}
    {trusting && <div className="trust-panel" role="region" aria-label="核对 SSH 指纹"><h4>请先从服务商控制台独立核对主机指纹</h4><p>节点：{trusting.name} · {trusting.host}:{trusting.port}</p><code>{trusting.candidate_fingerprint}</code><label className="check-label"><input type="checkbox" checked={verified} onChange={event => setVerified(event.target.checked)} />我已通过可信渠道核对，指纹完全一致</label><div className="modal-actions"><Button onClick={() => setTrusting(null)}>取消</Button><Button variant="primary" disabled={!verified} busy={busy} onClick={() => void run(async () => { const trusted = await api<{ job_ids?: string[] }>(`/proxies/${trusting.id}/trust`, 'POST', { fingerprint: trusting.candidate_fingerprint }); setTrusting(null); track(trusted.job_ids || [], { label: `测试代理 ${trusting.name}` }); })}><Check size={15} />确认并测试</Button></div></div>}
    <Pagination page={page} total={data?.total || 0} limit={20} onChange={setPage} /><p className="form-hint">节点网络错误才切换下一个。首次 SSH 连接或指纹变更时拒绝发送密码，必须在此手动核对确认。</p></div>;
}

function ProxyEditor({ node, onClose }: { node: ProxyNode | null; onClose: () => void }) {
  const toast = useToast();
  const track = useTrack();
  const [name, setName] = useState(node?.name || '');
  const [scheme, setScheme] = useState<ProxyNode['scheme']>(node?.scheme || 'auto');
  const [host, setHost] = useState(node?.host || '');
  const [port, setPort] = useState(node?.port || 8080);
  const [username, setUsername] = useState(node?.username || '');
  const [password, setPassword] = useState('');
  const [clearPassword, setClearPassword] = useState(false);
  const [enabled, setEnabled] = useState(node ? !!node.enabled : true);
  const [busy, setBusy] = useState(false);
  const [paste, setPaste] = useState('');
  const applyPaste = (value: string) => {
    setPaste(value);
    const parsed = parseProxyLine(value);
    if (!parsed) return;
    setScheme(parsed.scheme); setHost(parsed.host); setPort(parsed.port); setUsername(parsed.username); setPassword(parsed.password);
    if (!name) setName(parsed.host);
    toast(parsed.scheme === 'auto' ? '已识别地址、端口、用户名和密码；协议将自动探测' : `已识别为 ${parsed.scheme.toUpperCase()} 节点`, 'success');
  };
  const save = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true);
    try { const result = await api<{ job_ids?: string[] }>(node ? `/proxies/${node.id}` : '/proxies', node ? 'PUT' : 'POST', { name, scheme, host, port, username, password: clearPassword ? '' : password || (node ? null : ''), enabled }); setPassword(''); refreshData(); if (result.job_ids?.length) track(result.job_ids, { label: `测试新代理 ${name || host}` }); else toast('代理已保存，请重新测试', 'success'); onClose(); }
    catch (error) { toast((error as Error).message, 'error'); } finally { setBusy(false); }
  };
  return <form onSubmit={save} autoComplete="off"><Button variant="ghost" onClick={onClose}><ArrowLeft size={15} />返回代理列表</Button><h3>{node ? '编辑代理' : '添加代理'}</h3>{!node && <label className="field paste-field">粘贴一整行自动识别<input value={paste} onChange={event => applyPaste(event.target.value)} placeholder="例如 host:1000:用户名:密码，或 socks5://用户名:密码@host:1080" autoComplete="off" spellCheck={false} /><span className="form-hint">支持 IP:端口:用户名:密码、空格分隔四字段、以及 http:// socks5:// s5:// ssh:// 链接；识别后会自动填入下方各项。</span></label>}<div className="form-grid"><label className="field">节点名称<input value={name} onChange={event => setName(event.target.value)} maxLength={100} placeholder="可选，例如：常用节点" /></label><label className="field">协议<select value={scheme} onChange={event => setScheme(event.target.value as ProxyNode['scheme'])}><option value="auto">无凭证探测（不猜端口）</option><option value="http">HTTP</option><option value="socks5">SOCKS5 / s5</option><option value="ssh">SSH TCP 转发</option></select></label><label className="field">IP / 主机名<input value={host} onChange={event => setHost(event.target.value)} required maxLength={255} placeholder="不含协议与路径" /></label><NumberField label="端口" value={port} min={1} max={65535} onChange={setPort} /><label className="field">用户名<input value={username} onChange={event => setUsername(event.target.value)} maxLength={512} autoComplete="off" /></label><label className="field">密码<input type="password" value={password} onChange={event => setPassword(event.target.value)} maxLength={4096} autoComplete="new-password" placeholder={node?.has_password ? '留空保留现有密码' : '无认证时可留空'} /></label></div><div className="proxy-edit-checks"><label className="check-label"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} />启用节点</label>{node?.has_password && <label className="check-label"><input type="checkbox" checked={clearPassword} onChange={event => setClearPassword(event.target.checked)} />清除已保存的代理密码</label>}</div><p className="form-hint">更改主机、端口或用户名时需要重新输入密码，避免将旧凭证发送给新地址。SSH 首次使用还需核对指纹。</p><div className="modal-actions"><Button onClick={onClose}>取消</Button><Button type="submit" variant="primary" busy={busy}><Save size={16} />保存代理</Button></div></form>;
}

function BackupPanel() {
  const toast = useToast();
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);
  const [summary, setSummary] = useState<Record<string, number> | null>(null);
  const backup = async () => {
    setBusy(true);
    try {
      const response = await fetchApi('/backup', 'POST');
      const blob = await response.blob();
      const name = /filename="([^"]+)"/.exec(response.headers.get('content-disposition') || '')?.[1] || 'any-signin-backup.json';
      downloadBlob(blob, name);
      toast(`备份已生成：${response.headers.get('x-backup-accounts') || '?'} 个账号，已开始下载 ${name}`, 'success');
    } catch (error) { toast((error as Error).message, 'error'); } finally { setBusy(false); }
  };
  const restore = async (file: File) => {
    if (!await confirm({ title: `从「${file.name}」恢复？`, message: '按账号合并：备份里有而这里没有的账号会新增；已存在的账号会更新密码、备注和更新的凭证；计划、代理和签到统计一并合并。不会删除现有数据。', confirmLabel: '开始恢复' })) return;
    setBusy(true);
    try {
      const response = await fetch('/api/v1/backup/restore', { method: 'POST', credentials: 'same-origin', body: await file.arrayBuffer(), headers: { 'Content-Type': file.name.endsWith('.zip') ? 'application/zip' : 'application/json', 'X-CSRF-Token': (await api<{ csrf_token: string }>('/auth/me')).csrf_token } });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || `恢复失败（${response.status}）`);
      setSummary(payload); refreshData();
      toast(`恢复完成：新增 ${payload.accounts_added} 个账号，更新 ${payload.accounts_updated} 个，${payload.schedules} 个计划，${payload.proxies} 个代理，${payload.checkins} 条签到记录`, 'success');
    } catch (error) { toast((error as Error).message, 'error'); } finally { setBusy(false); }
  };
  return <div className="backup-panel"><div className="settings-section"><h3><DatabaseBackup size={15} /> 一键备份</h3><p className="form-hint">导出全部账号（含密码、凭证、备注、排序）、签到计划、代理节点、签到统计和运行参数为一个 JSON 文件。文件包含明文密码和 Cookie，请只保存在你自己的安全位置。</p><Button variant="primary" busy={busy} onClick={() => void backup()}><DatabaseBackup size={15} />下载备份文件</Button></div>
    <div className="settings-section"><h3><Upload size={15} /> 从备份恢复</h3><p className="form-hint">选择之前下载的 JSON 或远程备份 ZIP 文件。可以恢复到换了加密密钥的新服务器。恢复前请先暂停队列并等待正在执行的任务结束。</p><label className="button"><Upload size={15} />选择备份文件并恢复<input type="file" accept=".json,.zip,application/json,application/zip" hidden disabled={busy} onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void restore(file); }} /></label>{summary && <div className="notice"><strong>上次恢复结果</strong><p>新增账号 {summary.accounts_added} · 更新账号 {summary.accounts_updated} · 计划 {summary.schedules} · 代理 {summary.proxies} · 签到记录 {summary.checkins}</p></div>}</div>
    <div className="settings-section"><h3>服务器级备份</h3><p className="form-hint">数据库文件级备份（含加密密钥校验）请在服务器上运行 scripts/backup.sh；升级前会自动执行。两种备份互补：网页备份便于迁移，脚本备份便于回滚。</p></div></div>;
}
