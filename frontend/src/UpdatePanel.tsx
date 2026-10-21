import { useEffect, useRef, useState } from 'react';
import { ArrowUpCircle, CheckCircle2, Clock3, Download, Github, RefreshCw, Save, ShieldCheck } from 'lucide-react';
import { api, formatTime, useResource } from './api';
import { Button, ErrorNotice, useConfirm, useToast } from './ui';
import { isUpdateRunning, readUpdateSnapshot, rememberUpdateView, saveUpdateSnapshot, selectUpdateOperation, updateTrackingKey, type UpdateOperation, type UpdateState, type VersionCheck } from './updateState';

export default function UpdatePanel() {
  const toast = useToast();
  const confirm = useConfirm();
  const { data: live, error, reload } = useResource<UpdateState>('/updates', 2500);
  const [snapshot, setSnapshot] = useState(readUpdateSnapshot);
  const [submitted, setSubmitted] = useState<UpdateOperation | null>(null);
  const [repository, setRepository] = useState('');
  const [force, setForce] = useState(false);
  const [busy, setBusy] = useState('');
  const initialized = useRef(false);
  const handled = useRef('');
  const data = live || snapshot;
  const operation = selectUpdateOperation(submitted || snapshot?.operation, live?.operation);
  const running = isUpdateRunning(operation);
  const reconnecting = !!operation?.id && (!!error || live?.operation.id !== operation.id);
  const release = data?.check.release;
  useEffect(() => { rememberUpdateView(true); }, []);
  useEffect(() => {
    if (!live) return;
    setSnapshot(previous => {
      const next = { ...live, operation: selectUpdateOperation(previous?.operation, live.operation) || live.operation };
      saveUpdateSnapshot(next);
      return next;
    });
    setSubmitted(previous => previous && selectUpdateOperation(previous, live.operation) === live.operation ? null : previous);
  }, [live]);
  useEffect(() => { if (data && !initialized.current) { initialized.current = true; setRepository(data.repository); } }, [data]);
  useEffect(() => {
    if (!live?.operation.id || live.operation.id !== operation?.id || error || isUpdateRunning(live.operation) || handled.current === live.operation.id) return;
    const completedOperation = live.operation;
    let tracked = '';
    try { tracked = localStorage.getItem(updateTrackingKey) || ''; } catch {}
    if (tracked !== completedOperation.id) return;
    handled.current = completedOperation.id!;
    try { localStorage.removeItem(updateTrackingKey); } catch {}
    if (completedOperation.stage === 'complete') {
      toast(completedOperation.message || '更新完成', 'success');
      if (live.current_version !== __APP_VERSION__) window.setTimeout(() => window.location.reload(), 1600);
    } else toast(completedOperation.message || '更新未完成，原版本已保留', 'error');
  }, [live, operation?.id, error, toast]);

  const saveSource = async () => {
    if (!await confirm({ title: '使用这个版本发布仓库？', message: '请只使用你自己维护或完全信任的 any签到助手发布仓库。在线更新会下载并运行其中的程序；签到脚本上游 millylee/anyrouter-check-in 不是本程序的更新源。', confirmLabel: '保存更新源' })) return;
    setBusy('source');
    try {
      const result = await api<{ repository: string }>('/updates/source', 'PUT', { repository });
      setRepository(result.repository); await reload(); toast('更新源已保存，请检测版本', 'success');
    } catch (caught) { toast((caught as Error).message, 'error'); } finally { setBusy(''); }
  };
  const check = async () => {
    setBusy('check');
    try {
      const result = await api<VersionCheck>('/updates/check', 'POST');
      await reload();
      toast(result.update_available ? `发现新版本 v${result.release?.version}` : `已检测远程 v${result.release?.version}，当前没有更新的稳定版本`, 'success');
    } catch (caught) { toast((caught as Error).message, 'error'); await reload().catch(() => {}); } finally { setBusy(''); }
  };
  const install = async () => {
    if (!release) return;
    if (!await confirm({ title: `更新到 v${release.version}？`, message: `程序会校验源码包，构建镜像后暂停队列，备份账号、签到数据及密钥，再重启服务并检查健康状态。${force ? '强制模式会覆盖本地源码改动，原文件将保存在备份中。' : '发现本地源码改动会拒绝更新。'}请勿重复点击。`, confirmLabel: force ? '备份并强制更新' : '备份并更新', danger: force })) return;
    setBusy('install');
    try {
      const result = await api<{ id: string }>('/updates/install', 'POST', { version: release.version, force, acknowledged: true });
      const queued = { id: result.id, stage: 'queued', version: release.version, message: '等待更新执行器接收请求', progress: 0, started_at: Date.now() / 1000 };
      setSubmitted(queued);
      if (data) { const next = { ...data, operation: queued }; setSnapshot(next); saveUpdateSnapshot(next); }
      try { localStorage.setItem(updateTrackingKey, result.id); } catch {}
      rememberUpdateView(true);
      toast('更新开始，进度会持续显示；重启期间会自动重新连接', 'progress');
      await reload();
    } catch (caught) { toast((caught as Error).message, 'error'); } finally { setBusy(''); }
  };

  return <div className="update-panel">
    <div className="update-heading"><span className="update-emblem"><RefreshCw size={24} /></span><div><h3>版本更新</h3><p>先备份，再更新。你的账号与签到数据始终保留。</p></div></div>
    <ErrorNotice message={running && error ? '服务正在切换或暂时不可达，保留上次进度并自动重新连接，请勿重复提交。' : error} retry={reload} />
    {reconnecting && <p className="form-hint" role="status">正在重新读取服务器进度；下方显示上次保存的状态，连接恢复后会自动更新。</p>}
    <div className="version-card"><div><span>当前版本</span><strong>v{data?.current_version || __APP_VERSION__}</strong></div><div><span>远程版本</span><strong>{release ? `v${release.version}` : data?.repository ? '尚未检测' : '未配置更新源'}</strong></div><p><Clock3 size={13} />上次检测：{formatTime(data?.check.checked_at)}{data?.check.update_available && <span className="version-new">发现新版本</span>}</p></div>
    {!!data?.check.error && <div className="notice warning">{data.check.error}</div>}
    <label className="check-label update-force"><input type="checkbox" checked={force} onChange={event => setForce(event.target.checked)} disabled={running || !!busy} />强制更新版本（备份后覆盖本地源码改动）</label>
    <div className="update-actions"><Button busy={busy === 'check'} disabled={!!busy || running || !!error || !live?.repository} onClick={check}><RefreshCw size={16} />检测版本</Button><Button variant="primary" busy={busy === 'install' || running} disabled={!!busy || running || !!error || !live?.updater_available || !release || (!live.check.update_available && !force)} onClick={install}><Download size={16} />{running ? '正在更新' : '更新版本'}</Button></div>
    {live && !error && !live.updater_available && <p className="form-hint">更新执行器尚未连接。Linux 服务器运行 <code>sudo bash scripts/install-updater.sh</code> 后即可使用；Web 容器不需要 Docker 权限。</p>}
    {operation?.id && <section className={`update-operation ${operation.stage || ''}`} aria-label={running ? '当前更新进度' : '最近一次更新结果'} aria-live="polite"><div className="update-operation-meta"><strong>{running ? '当前更新' : '最近一次更新'}{operation.version ? ` · v${operation.version}` : ''}</strong><time>{formatTime(operation.finished_at || operation.updated_at || operation.started_at)}</time></div><div className="update-operation-title">{operation.stage === 'complete' ? <CheckCircle2 size={17} /> : <ArrowUpCircle size={17} />}<strong>{operation.message}</strong><span>{operation.progress || 0}%</span></div><progress max={100} value={operation.progress || 0} aria-label="更新进度" />{operation.history && <ol>{operation.history.map((item, index) => <li key={`${item.at}-${index}`}><time>{formatTime(item.at)}</time><span>{item.message}</span></li>)}</ol>}{operation.backup_path && <p>服务器备份：<code>{operation.backup_path}</code></p>}</section>}
    <div className="notice update-note"><ShieldCheck size={17} /><p>小型服务器首次构建可能需要十几分钟。构建时原服务继续运行；切换前自动备份，失败自动回滚。刷新页面会恢复当前进度，完成或失败后仍可查看最近一次结果；服务切换期间自动重连，载入新版后继续显示更新结果。</p></div>
    <div className="update-source"><h4><Github size={16} />发布渠道</h4><p>默认使用 a06342637/anyqiandao 的 GitHub Releases，也可自定义仓库；留空保存可恢复默认。发布需包含源码包与 <code>release.json</code> 校验清单。请勿填写签到脚本的上游地址，只使用此助手自身的发布仓库。</p><label className="field">GitHub 仓库<input value={repository} disabled={!!busy || running} onChange={event => setRepository(event.target.value)} maxLength={240} placeholder="a06342637/anyqiandao" autoComplete="off" spellCheck={false} /></label><a href="https://github.com/a06342637/anyqiandao" target="_blank" rel="noreferrer">打开默认仓库</a> <Button onClick={saveSource} busy={busy === 'source'} disabled={!!busy || running || repository === data?.repository}><Save size={15} />保存更新源</Button></div>
    {release?.notes && <details className="release-notes"><summary>v{release.version} 发布说明</summary><p>{release.notes}</p><a href={release.release_url} target="_blank" rel="noreferrer">在 GitHub 查看发布 ↗</a></details>}
  </div>;
}
