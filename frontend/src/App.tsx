import { useCallback, useEffect, useRef, useState, type CSSProperties, type FormEvent, type ReactNode } from 'react';
import { ArrowRight, CalendarClock, ChevronRight, Gauge, KeyRound, Layers3, ListChecks, ListOrdered, LogOut, Moon, Settings2, ShieldCheck, Sun, Users, X } from 'lucide-react';
import { api, formatTime, newId, refreshData, setCsrfToken, useResource } from './api';
import { BrandingContext, Button, ConfirmProvider, DEFAULT_BRANDING, ErrorNotice, SealMark, ToastContext, TrackContext, kindLabel, type TrackOptions } from './ui';
import type { Dashboard, JobStatus, SiteBranding } from './types';
import DashboardPage from './DashboardPage';
import ExtractPage from './ExtractPage';
import AccountsPage from './AccountsPage';
import SchedulesPage from './SchedulesPage';
import LogsPage from './LogsPage';
import QueuePage from './QueuePage';
import SettingsPanel from './SettingsPanel';

type Page = 'dashboard' | 'extract' | 'accounts' | 'schedules' | 'logs' | 'queue';
const navigation = [
  { id: 'dashboard' as const, label: '仪表盘', icon: Gauge, description: '签到次数、新增余额和账号余额，按天 / 周 / 月统计。' },
  { id: 'extract' as const, label: '凭证提取', icon: KeyRound, description: '添加新账号，按队列并发登录并取回 session 与 api_user。' },
  { id: 'accounts' as const, label: '账号列表', icon: Users, description: '检测凭证是否仍然有效，复制配置，或重新提取。' },
  { id: 'schedules' as const, label: '签到管理', icon: CalendarClock, description: '选择账号、设定间隔，按计划运行内置签到脚本。' },
  { id: 'logs' as const, label: '运行日志', icon: ListChecks, description: '每一次提取、检测和签到的结果都在这里；不记录密码与 Cookie。' },
  { id: 'queue' as const, label: '执行队列', icon: ListOrdered, description: '统一管理任务、调整普通队列与签到并发，实时查看执行进度。' },
];

function initialTheme(): 'light' | 'dark' {
  try { const stored = localStorage.getItem('any-theme'); if (stored === 'light' || stored === 'dark') return stored; } catch {}
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export default function App() {
  const [theme, setTheme] = useState(initialTheme);
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [page, setPage] = useState<Page>('dashboard');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [logJob, setLogJob] = useState('');
  const [logAccount, setLogAccount] = useState('');
  const [toasts, setToasts] = useState<{ message: string; kind: string; id: string; created: number }[]>([]);
  const trackers = useRef(new Map<string, { ids: string[]; options: TrackOptions; started: number }>());
  const [initialError, setInitialError] = useState('');
  const { data: branding } = useResource<SiteBranding>('/branding', 0);
  const site = branding || DEFAULT_BRANDING;
  const { data: dashboard, error: dashboardError, reload } = useResource<Dashboard>(authenticated ? '/dashboard' : null);
  const timezone = dashboard?.timezone || 'Asia/Seoul';
  const current = navigation.find(item => item.id === page)!;
  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);
  useEffect(() => {
    document.title = site.site_name;
    const icon = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
    if (icon) icon.href = `/favicon.svg?icon=${encodeURIComponent(site.site_icon_text)}`;
  }, [site.site_name, site.site_icon_text]);
  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const changed = () => { try { if (!localStorage.getItem('any-theme')) setTheme(media.matches ? 'dark' : 'light'); } catch { setTheme(media.matches ? 'dark' : 'light'); } };
    media.addEventListener('change', changed); return () => media.removeEventListener('change', changed);
  }, []);
  useEffect(() => {
    let active = true;
    void api<{ csrf_token: string }>('/auth/me').then(result => { if (active) { setCsrfToken(result.csrf_token); setAuthenticated(true); } }).catch(error => {
      if (active) { setAuthenticated(false); if (error.status !== 401) setInitialError('暂时无法连接服务，请检查网络后重试'); }
    });
    const expired = () => { setCsrfToken(''); setAuthenticated(false); setSettingsOpen(false); trackers.current.clear(); setToasts([]); };
    window.addEventListener('any-auth-expired', expired);
    return () => { active = false; window.removeEventListener('any-auth-expired', expired); };
  }, []);
  useEffect(() => { const timer = window.setInterval(() => setToasts(previous => previous.length ? previous.filter(item => item.kind === 'progress' || Date.now() - item.created < 8500) : previous), 1000); return () => clearInterval(timer); }, []);
  const notify = useCallback((message: string, kind = 'info') => setToasts(previous => [...previous, { message, kind, id: newId(), created: Date.now() }].slice(-4)), []);
  const updateProgress = useCallback((id: string, message: string, create = false) => setToasts(previous => {
    const existing = previous.find(item => item.id === id);
    if (existing?.message === message) return previous;
    return existing ? previous.map(item => item.id === id ? { ...item, message } : item) : create ? [...previous, { id, message, kind: 'progress', created: Date.now() }].slice(-4) : previous;
  }), []);
  const track = useCallback((jobIds: string[], options: TrackOptions) => {
    const ids = [...new Set(jobIds.filter(Boolean))];
    if (!ids.length) return;
    const key = newId();
    trackers.current.set(key, { ids, options, started: Date.now() });
    updateProgress(key, `${options.label}：已入队，完成 0 / ${ids.length}`, true);
  }, [updateProgress]);
  useEffect(() => {
    if (!authenticated) return;
    let loading = false;
    let disposed = false;
    const timer = window.setInterval(async () => {
      if (!trackers.current.size || document.hidden || loading) return;
      const all = [...new Set([...trackers.current.values()].flatMap(entry => entry.ids))];
      const statuses: JobStatus[] = [];
      loading = true;
      try { for (let offset = 0; offset < all.length; offset += 200) statuses.push(...(await api<{ items: JobStatus[] }>(`/jobs/status?ids=${all.slice(offset, offset + 200).join(',')}`)).items); }
      catch { return; } finally { loading = false; }
      if (disposed) return;
      const byId = new Map(statuses.map(item => [item.id, item]));
      for (const [key, entry] of [...trackers.current.entries()]) {
        const jobs = entry.ids.map(id => byId.get(id)).filter((item): item is JobStatus => !!item);
        const done = jobs.filter(item => !['pending', 'running'].includes(item.status));
        const missing = entry.ids.length - jobs.length;
        if (done.length + missing < entry.ids.length) {
          if (Date.now() - entry.started > 2 * 60 * 60 * 1000) {
            trackers.current.delete(key);
            setToasts(previous => previous.filter(item => item.id !== key));
            notify(`${entry.options.label}尚未全部完成，后台任务会继续，请检查队列是否暂停`, 'info');
            continue;
          }
          const active = jobs.find(item => item.status === 'running') || jobs.find(item => item.status === 'pending');
          updateProgress(key, `${entry.options.label} · 已完成 ${done.length} / ${entry.ids.length}：${active?.message || (active?.status === 'pending' ? '等待队列执行' : '正在执行…')}`);
          continue;
        }
        trackers.current.delete(key);
        setToasts(previous => previous.filter(item => item.id !== key));
        refreshData();
        entry.options.onDone?.();
        if (done.length === 1 && entry.ids.length === 1) {
          const job = done[0];
          const subject = job.username || job.proxy_name || '';
          const good = ['success', 'signed', 'already_signed'].includes(job.status);
          const quota = job.quota !== null && !['proxy_test', 'checkin'].includes(job.kind) ? `，余额 $${job.quota.toFixed(4)}` : '';
          notify(`${kindLabel(job.kind)}${subject ? ` · ${subject}` : ''}：${job.message || job.status}${good ? quota : ''}`, good ? 'success' : 'error');
        } else {
          const good = done.filter(item => ['success', 'signed', 'already_signed'].includes(item.status)).length;
          const bad = done.length - good;
          const failures = done.filter(item => !['success', 'signed', 'already_signed'].includes(item.status)).slice(0, 3).map(item => `${item.username || item.proxy_name || ''} ${item.message}`.trim());
          notify(`${entry.options.label}完成：成功 ${good} 个${bad ? `，失败 ${bad} 个（${failures.join('；')}${bad > 3 ? '…' : ''}）` : ''}${missing ? `，${missing} 个任务已不存在，无法确认结果` : ''}`, bad || missing ? 'error' : 'success');
        }
      }
    }, 1500);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [authenticated, notify, updateProgress]);
  const toggleTheme = () => { const next = theme === 'light' ? 'dark' : 'light'; setTheme(next); try { localStorage.setItem('any-theme', next); } catch {} };
  const openLogs = (jobId: string) => { setLogJob(jobId); setLogAccount(''); setPage('logs'); };
  const clearLogFilter = () => { setLogJob(''); setLogAccount(''); };
  const logout = async () => { try { await api('/auth/logout', 'POST'); setAuthenticated(false); setCsrfToken(''); trackers.current.clear(); setToasts([]); } catch (error) { notify((error as Error).message, 'error'); } };
  const themeButton = <button className="icon-button" aria-label={theme === 'light' ? '切换深色主题' : '切换浅色主题'} title="明暗切换" onClick={toggleTheme}>{theme === 'light' ? <Moon size={18} /> : <Sun size={18} />}</button>;
  const queueState = !dashboard ? null : dashboard.storage_error ? { tone: 'red', text: '存储异常' } : dashboard.paused ? { tone: 'amber', text: '队列已暂停' } : dashboard.jobs.running ? { tone: 'blue', text: `执行中 · ${dashboard.jobs.pending} 个等待` } : dashboard.jobs.pending ? { tone: 'blue', text: `${dashboard.jobs.pending} 个任务等待` } : { tone: 'green', text: '队列空闲' };

  return <BrandingContext.Provider value={site}><ToastContext.Provider value={notify}><TrackContext.Provider value={track}><ConfirmProvider>
    {authenticated ? <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#" title={site.site_name} onClick={event => { event.preventDefault(); setPage('dashboard'); }}><SealMark /><span className="brand-text">{site.site_name}</span></a>
        <div className="topbar-right">
          {queueState && <button className={`queue-pill ${queueState.tone}`} title="查看执行队列" onClick={() => setPage('queue')}><span className="live-dot" />{queueState.text}</button>}
          <button className="icon-button topbar-settings" aria-label="打开设置" title="设置" onClick={() => setSettingsOpen(true)}><Settings2 size={18} /></button>
          {themeButton}
          <button className="icon-button" aria-label="退出登录" title="退出登录" onClick={logout}><LogOut size={18} /></button>
        </div>
      </header>
      <aside className="sidebar">
        <nav aria-label="主导航">{navigation.map(item => <button key={item.id} className={`nav-item ${page === item.id ? 'active' : ''}`} onClick={() => { setPage(item.id); if (item.id === 'logs') clearLogFilter(); }}><item.icon size={18} strokeWidth={1.9} /><span>{item.label}</span>{page === item.id && <ChevronRight size={14} className="nav-chevron" />}</button>)}</nav>
        <div className="sidebar-bottom">
          <div className="sidebar-note"><ShieldCheck size={16} strokeWidth={1.8} /><span>账号、密码与凭证加密保存在你自己的服务器上，不接入第三方统计。</span></div>
          <button className={`nav-item nav-settings ${settingsOpen ? 'active' : ''}`} onClick={() => setSettingsOpen(true)}><Settings2 size={18} strokeWidth={1.9} /><span>设置</span><span className="sidebar-version">v{__APP_VERSION__}</span></button>
        </div>
      </aside>
      <main className="workspace">
        <div className="page-heading"><div><h1>{current.label}</h1><p>{current.description}</p></div><span className={`workspace-chip ${dashboard?.proxy_mode === 'direct' ? 'direct' : ''}`}><ShieldCheck size={14} />{dashboard?.proxy_mode === 'direct' ? '直连模式（已明确选择）' : `代理模式 · ${dashboard?.enabled_proxies ?? 0} 个启用节点`}</span></div>
        <ErrorNotice message={dashboardError} retry={reload} />
        {dashboard?.storage_error && <div className="notice warning" role="alert">存储出现异常，执行器已停止。请检查空间和磁盘权限，再重启本项目；不会自动删除凭证。</div>}
        {page === 'dashboard' && <div className="stats-grid">
          <Stat icon={<Users size={17} />} label="管理账号" value={dashboard?.accounts.total} caption={dashboard ? `${dashboard.accounts.extracted} 个已提取凭证` : '所有导入的账号'} />
          <Stat icon={<ShieldCheck size={17} />} label="有效凭证" value={dashboard?.accounts.valid} caption={dashboard?.accounts.invalid ? `${dashboard.accounts.invalid} 个已失效，需要更新凭证` : '以最近一次检测结果为准'} tone={dashboard?.accounts.invalid ? 'warning-text' : ''} />
          <Stat icon={<Layers3 size={17} />} label="等待执行" value={dashboard?.jobs.pending} caption={dashboard?.paused ? '队列已暂停' : `普通并发 ${dashboard?.max_concurrency || 1} · 签到并发 ${dashboard?.checkin_concurrency || 1}`} tone={dashboard?.paused ? 'warning-text' : ''} />
          <div className="stat-card"><div className="stat-label">下一次计划签到<CalendarClock size={17} /></div><div className="stat-value compact">{dashboard?.next_run ? formatTime(dashboard.next_run, timezone) : '尚未安排'}</div><span className="stat-caption">{dashboard?.next_run ? `时区 ${timezone}` : '在签到管理中创建计划'}</span></div>
        </div>}
        {page === 'dashboard' && <DashboardPage timezone={timezone} onLogs={openLogs} />}
        {page === 'extract' && <ExtractPage onQueue={() => setPage('queue')} onAccounts={() => setPage('accounts')} onSettings={() => setSettingsOpen(true)} dashboard={dashboard} />}
        {page === 'accounts' && <AccountsPage timezone={timezone} onExtract={() => setPage('extract')} onLogs={accountId => { setLogAccount(accountId); setLogJob(''); setPage('logs'); }} />}
        {page === 'schedules' && <SchedulesPage timezone={timezone} onLogs={() => { clearLogFilter(); setPage('logs'); }} />}
        {page === 'logs' && <LogsPage timezone={timezone} jobId={logJob} accountId={logAccount} onClearFilter={clearLogFilter} />}
        {page === 'queue' && <QueuePage timezone={timezone} onLogs={openLogs} />}
        <footer className="footer"><span>{site.site_name} <b>v{__APP_VERSION__}</b></span><span><ShieldCheck size={13} /> AES-256-GCM 字段加密 · 私有部署</span></footer>
      </main>
      {settingsOpen && <SettingsPanel onClose={() => setSettingsOpen(false)} />}
    </div> : authenticated === null ? <div className="loading-screen"><SealMark size="large" /><p>正在连接工作空间…</p></div> : <div className="login-layout">
      <header className="login-header"><span className="brand" title={site.site_name}><SealMark /><span className="brand-text">{site.site_name}</span></span>{themeButton}</header>
      <main className="login-main">
        <section className="login-intro">
          <WeekStrip />
          <h1>提取一次，<br /><span>天天签到。</span></h1>
          <p>从 AnyRouter 账号提取登录凭证，检测有效性，并按你设定的间隔自动签到。私有部署，凭证只保存在你自己的服务器上。</p>
          <div className="login-steps"><span><KeyRound size={16} />提取凭证</span><ArrowRight size={14} /><span><ShieldCheck size={16} />检测有效</span><ArrowRight size={14} /><span><CalendarClock size={16} />定时签到</span></div>
        </section>
        <LoginForm error={initialError} onSuccess={token => { setCsrfToken(token); setAuthenticated(true); setPage('dashboard'); setInitialError(''); refreshData(); }} />
      </main>
      <footer className="login-footer"><span>{site.site_name} · v{__APP_VERSION__}</span><span><ShieldCheck size={14} />私有部署 · AES-256-GCM 加密存储</span></footer>
    </div>}
    <div className="toast-stack">{toasts.map(item => <div key={item.id} className={`toast ${item.kind}`} role={item.kind === 'error' ? 'alert' : 'status'}>{item.kind === 'progress' && <span className="loading-dot" />}<span>{item.message}</span><button className="icon-button small" aria-label="关闭提示" onClick={() => setToasts(previous => previous.filter(toast => toast.id !== item.id))}><X size={16} /></button></div>)}</div>
  </ConfirmProvider></TrackContext.Provider></ToastContext.Provider></BrandingContext.Provider>;
}

function Stat({ icon, label, value, caption, tone = '' }: { icon: ReactNode; label: string; value?: number; caption: string; tone?: string }) {
  return <div className="stat-card"><div className="stat-label">{label}{icon}</div><div className="stat-value">{value === undefined ? '—' : value.toLocaleString()}<span>个</span></div><span className={`stat-caption ${tone}`}>{caption}</span></div>;
}

// Decorative only: stamps mark the days of this week that have already passed, not real check-in results.
function WeekStrip() {
  const index = (new Date().getDay() + 6) % 7;
  const days = ['一', '二', '三', '四', '五', '六', '日'];
  return <div className="week-strip" aria-hidden="true">{days.map((day, position) => <span key={day} className={`week-cell ${position < index ? 'stamped' : position === index ? 'today' : ''}`} style={{ '--i': position } as CSSProperties}><span className="week-stamp">签</span><small>周{day}</small></span>)}</div>;
}

function LoginForm({ onSuccess, error }: { onSuccess: (token: string) => void; error: string }) {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setMessage('');
    try { const result = await api<{ csrf_token: string }>('/auth/login', 'POST', { username, password }); setPassword(''); onSuccess(result.csrf_token); }
    catch (caught) { setMessage((caught as Error).message); } finally { setBusy(false); }
  };
  return <section className="login-card"><span className="login-lock"><KeyRound size={22} strokeWidth={1.6} /></span><h2>登录</h2><p>使用部署时设置的管理员账号和密码。部署信息保存在服务器项目目录的「部署信息.txt」中。</p><form onSubmit={submit}><label className="field">管理员账号<input name="username" value={username} onChange={event => setUsername(event.target.value)} required maxLength={64} autoComplete="username" /></label><label className="field">管理员密码<input name="password" type="password" value={password} onChange={event => setPassword(event.target.value)} placeholder="输入管理员密码" required autoComplete="current-password" maxLength={1024} autoFocus /></label><ErrorNotice message={message || error} /><Button variant="primary" type="submit" busy={busy} className="full-width">登录 <ArrowRight size={17} /></Button></form><div className="login-card-note"><ShieldCheck size={15} />登录状态保留 24 小时；连续输错 10 次会暂停 15 分钟。</div></section>;
}
