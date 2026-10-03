import { useEffect, useState, type FormEvent } from 'react';
import { Cloud, DatabaseBackup, FolderOpen, PlugZap, Save, Server, ShieldCheck, Upload, X } from 'lucide-react';
import { api, formatTime, refreshData, useResource } from './api';
import { Button, ErrorNotice, useConfirm, useToast } from './ui';
import './remote-backup.css';

type Target = 'oss' | 'sftp';
interface OSS { enabled: boolean; region: string; bucket: string; access_key_id: string; access_key_secret?: string; has_access_key_secret?: boolean; prefix: string; internal: boolean; endpoint: string; keep: number; }
interface SFTP { enabled: boolean; host: string; port: number; username: string; auth: 'password' | 'private_key'; password?: string; private_key?: string; passphrase?: string; has_password?: boolean; has_private_key?: boolean; has_passphrase?: boolean; directory: string; keep: number; }
interface Settings { enabled: boolean; every: number; unit: 'days' | 'hours'; time: string; timezone: string; mode: 'app' | 'full'; oss: OSS; sftp: SFTP; }
interface Result { at: number; name: string; size: number; status: string; message: string; duration: number; }
interface BackupData { settings: Settings; fingerprint: string; busy: boolean; state: { running?: boolean; next_run?: number; last_run?: number; started_at?: number; source?: string; status?: string; result?: string; targets?: Partial<Record<Target, Result>>; }; }
interface Directory { path: string; parent: string; directories: string[]; truncated: boolean; }

function payload(settings: Settings) {
  const value = structuredClone(settings);
  delete value.oss.has_access_key_secret;
  delete value.sftp.has_password; delete value.sftp.has_private_key; delete value.sftp.has_passphrase;
  return value;
}

export default function RemoteBackupPanel() {
  const { data, error, reload } = useResource<BackupData>('/remote-backup', 2500);
  return <><ErrorNotice message={error} retry={reload} />{data ? <BackupForm data={data} reload={reload} /> : <p className="muted">正在读取备份设置…</p>}</>;
}

function BackupForm({ data, reload }: { data: BackupData; reload: () => Promise<boolean> }) {
  const [settings, setSettings] = useState(data.settings);
  const [baseline, setBaseline] = useState(JSON.stringify(data.settings));
  const [busy, setBusy] = useState('');
  const [browser, setBrowser] = useState<{ kind: Target; listing: Directory } | null>(null);
  const toast = useToast();
  const confirm = useConfirm();
  const dirty = JSON.stringify(settings) !== baseline;
  const disabled = !!busy || data.busy;
  useEffect(() => {
    if (!dirty) { setSettings(data.settings); setBaseline(JSON.stringify(data.settings)); }
  }, [data.settings]);
  const update = <K extends keyof Settings>(key: K, value: Settings[K]) => setSettings(previous => ({ ...previous, [key]: value }));
  const oss = <K extends keyof OSS>(key: K, value: OSS[K]) => setSettings(previous => ({ ...previous, oss: { ...previous.oss, [key]: value } }));
  const sftp = <K extends keyof SFTP>(key: K, value: SFTP[K]) => setSettings(previous => ({ ...previous, sftp: { ...previous.sftp, [key]: value } }));
  const saved = (result: BackupData) => { setSettings(result.settings); setBaseline(JSON.stringify(result.settings)); };
  const save = async (event: FormEvent) => {
    event.preventDefault(); setBusy('save');
    try { saved(await api<BackupData>('/remote-backup', 'PUT', payload(settings))); await reload(); refreshData(); toast('远程备份设置已保存', 'success'); }
    catch (error) { toast((error as Error).message, 'error'); } finally { setBusy(''); }
  };
  const test = async (kind: Target) => {
    setBusy(kind);
    try { const result = await api<{ message: string }>(`/remote-backup/${kind}/test`, 'POST'); toast(result.message, 'success'); await reload(); refreshData(); }
    catch (error) { toast((error as Error).message, 'error'); } finally { setBusy(''); }
  };
  const browse = async (kind: Target, path: string) => {
    setBusy('browse');
    try { setBrowser({ kind, listing: await api<Directory>(`/remote-backup/${kind}/browse`, 'POST', { path }) }); await reload(); }
    catch (error) { toast((error as Error).message, 'error'); } finally { setBusy(''); }
  };
  const run = async () => {
    setBusy('run');
    try { await api('/remote-backup/run', 'POST'); await reload(); refreshData(); toast('备份已开始，可在这里或运行日志查看结果', 'progress'); }
    catch (error) { toast((error as Error).message, 'error'); } finally { setBusy(''); }
  };
  const resetKey = async () => {
    if (!await confirm({ title: '重置 SFTP 主机指纹？', message: '请先确认服务器重装或主机密钥变更。重置后，下次成功连接的服务器密钥会成为新的信任指纹。', confirmLabel: '已核实，重置信任', danger: true })) return;
    setBusy('key');
    try { await api('/remote-backup/sftp/host-key', 'DELETE'); await reload(); toast('主机指纹已重置', 'success'); }
    catch (error) { toast((error as Error).message, 'error'); } finally { setBusy(''); }
  };
  const result = (kind: Target) => {
    const status = data.state.targets?.[kind];
    return status ? <small className={`backup-result ${status.status}`}>{formatTime(status.at, settings.timezone)}：{status.message} · {status.duration} 秒</small> : <small>{settings[kind].enabled ? '已启用，等待首次备份' : '未启用'}</small>;
  };
  const secretPlaceholder = (exists?: boolean) => exists ? '已保存，留空表示不修改' : '未设置';
  return <form className="remote-backup" onSubmit={save}>
    <label className={`backup-master ${settings.enabled ? 'enabled' : ''}`}>
      <input type="checkbox" role="switch" checked={settings.enabled} disabled={disabled} onChange={event => update('enabled', event.target.checked)} />
      <span><strong>启用远程自动备份</strong><small>按计划将备份上传到已启用的目标。关闭后保留配置，仍可手动备份；修改后请保存。</small></span><b>{settings.enabled ? '已开启' : '已关闭'}</b>
    </label>
    <p className="form-hint">每次生成 ZIP 文件并上传至所有已启用目标，上传成功后按各自“保留份数”清理本实例的旧备份。备份和恢复无需额外密码，文件包含账号与凭证，请保存在自己的私有存储中。备份结果可在运行日志的“备份”分类查看，并随日志保留策略清理。</p>
    <div className="backup-overview" aria-live="polite">
      <div><small>下次备份</small><strong>{data.settings.enabled ? formatTime(data.state.next_run, data.settings.timezone) : '自动备份已关闭'}</strong></div>
      <div><small>上次备份</small><strong>{formatTime(data.state.last_run, data.settings.timezone)}{data.state.last_run ? `（${data.state.source === 'scheduled' ? '定时' : '手动'}）` : ''}</strong></div>
      <div><small>执行结果</small><strong className={`backup-result ${data.state.status || ''}`}>{data.state.running ? '正在备份…' : data.state.result || '尚未执行'}</strong></div>
      <Button variant="primary" type="button" busy={data.state.running || busy === 'run'} disabled={disabled || dirty || !(settings.oss.enabled || settings.sftp.enabled)} onClick={run}><Upload size={15} />立即备份</Button>
    </div>
    <fieldset className="backup-plan" disabled={disabled}>
      <legend>备份计划</legend>
      <div className="backup-grid">
        <label className="field">备份频率<div className="backup-frequency"><input aria-label="备份间隔" type="number" min={1} max={365} required value={settings.every} onChange={event => update('every', event.target.valueAsNumber)} /><select aria-label="备份频率单位" value={settings.unit} onChange={event => update('unit', event.target.value as Settings['unit'])}><option value="days">天</option><option value="hours">小时</option></select></div></label>
        <label className="field">{settings.unit === 'days' ? '执行时刻' : '起始时刻'}<input type="time" required value={settings.time} onChange={event => update('time', event.target.value)} /></label>
        <label className="field">计划时区<select value={settings.timezone} onChange={event => update('timezone', event.target.value)}>{['Asia/Shanghai', 'Asia/Seoul', 'Asia/Tokyo', 'UTC', 'America/Los_Angeles'].map(zone => <option key={zone}>{zone}</option>)}</select></label>
      </div>
      <p className="form-hint">每 {settings.every} {settings.unit === 'days' ? `天 ${settings.time}` : `小时（以 ${settings.time} 为起点）`}备份一次。停机期间错过的计划，恢复后补做一次；手动备份不改变定时计划。</p>
      <div className="backup-modes">
        <label className={settings.mode === 'app' ? 'selected' : ''}><input type="radio" name="backup-mode" value="app" checked={settings.mode === 'app'} onChange={() => update('mode', 'app')} /><span><strong>应用数据包（推荐）</strong><small>账号、凭证、代理、计划、签到统计及运行设置。可在“备份与恢复”直接导入 ZIP 或 JSON。</small></span></label>
        <label className={settings.mode === 'full' ? 'selected' : ''}><input type="radio" name="backup-mode" value="full" checked={settings.mode === 'full'} onChange={() => update('mode', 'full')} /><span><strong>完整备份</strong><small>应用数据包、程序源码、数据库快照、原密钥和基础部署配置。不含日志、临时会话和远程备份凭证。</small></span></label>
      </div>
    </fieldset>
    <section className={`backup-target ${settings.oss.enabled ? 'enabled' : ''}`}>
      <div className="backup-target-heading"><label><input type="checkbox" checked={settings.oss.enabled} disabled={disabled} onChange={event => oss('enabled', event.target.checked)} /><Cloud size={18} /><span><strong>阿里云 OSS</strong>{result('oss')}</span></label></div>
      <fieldset disabled={disabled} className="backup-target-body">
        <div className="backup-grid">
          <label className="field">地域<input required={settings.oss.enabled} value={settings.oss.region} onChange={event => oss('region', event.target.value)} placeholder="cn-hangzhou" /></label>
          <label className="field">Bucket 名称<input required={settings.oss.enabled} value={settings.oss.bucket} onChange={event => oss('bucket', event.target.value)} placeholder="my-backups" /></label>
          <label className="field">保留份数<input type="number" min={1} max={1000} required value={settings.oss.keep} onChange={event => oss('keep', event.target.valueAsNumber)} /></label>
          <label className="field">AccessKey ID<input required={settings.oss.enabled} autoComplete="off" value={settings.oss.access_key_id} onChange={event => oss('access_key_id', event.target.value)} /></label>
          <label className="field backup-span-2">AccessKey Secret<input type="password" autoComplete="new-password" value={settings.oss.access_key_secret || ''} onChange={event => oss('access_key_secret', event.target.value)} placeholder={secretPlaceholder(settings.oss.has_access_key_secret)} /></label>
        </div>
        <label className="field">保存目录（Bucket 内的路径，留空为根目录）<div className="backup-path"><input value={settings.oss.prefix} onChange={event => oss('prefix', event.target.value)} placeholder="any-signin/" /><Button type="button" disabled={disabled || dirty} onClick={() => browse('oss', settings.oss.prefix)}><FolderOpen size={14} />浏览</Button></div></label>
        <label className="check-label"><input type="checkbox" checked={settings.oss.internal} onChange={event => oss('internal', event.target.checked)} />使用内网 Endpoint（应用部署在同地域阿里云 ECS 时使用）</label>
        <label className="field">自定义 Endpoint（一般留空，按地域自动生成；填写后优先生效）<input value={settings.oss.endpoint} onChange={event => oss('endpoint', event.target.value)} placeholder={`oss-${settings.oss.region}${settings.oss.internal ? '-internal' : ''}.aliyuncs.com`} /></label>
        <Button type="button" busy={busy === 'oss'} disabled={disabled || dirty} onClick={() => test('oss')}><PlugZap size={14} />测试连接</Button>
      </fieldset>
    </section>
    <section className={`backup-target ${settings.sftp.enabled ? 'enabled' : ''}`}>
      <div className="backup-target-heading"><label><input type="checkbox" checked={settings.sftp.enabled} disabled={disabled} onChange={event => sftp('enabled', event.target.checked)} /><Server size={18} /><span><strong>SSH / SFTP 服务器</strong>{result('sftp')}</span></label></div>
      <fieldset disabled={disabled} className="backup-target-body">
        <div className="backup-grid">
          <label className="field backup-span-2">服务器 IP 或域名<input required={settings.sftp.enabled} value={settings.sftp.host} onChange={event => sftp('host', event.target.value)} placeholder="203.0.113.10" /></label>
          <label className="field">SSH 端口<input type="number" min={1} max={65535} required value={settings.sftp.port} onChange={event => sftp('port', event.target.valueAsNumber)} /></label>
          <label className="field">用户名<input required={settings.sftp.enabled} autoComplete="off" value={settings.sftp.username} onChange={event => sftp('username', event.target.value)} /></label>
          <label className="field">登录方式<select value={settings.sftp.auth} onChange={event => sftp('auth', event.target.value as SFTP['auth'])}><option value="password">密码</option><option value="private_key">SSH 私钥</option></select></label>
          {settings.sftp.auth === 'password' ? <label className="field">密码<input type="password" autoComplete="new-password" value={settings.sftp.password || ''} placeholder={secretPlaceholder(settings.sftp.has_password)} onChange={event => sftp('password', event.target.value)} /></label> : <label className="field">私钥口令（可选）<input type="password" autoComplete="new-password" value={settings.sftp.passphrase || ''} placeholder={secretPlaceholder(settings.sftp.has_passphrase)} onChange={event => sftp('passphrase', event.target.value)} /></label>}
          <label className="field">保留份数<input type="number" min={1} max={1000} required value={settings.sftp.keep} onChange={event => sftp('keep', event.target.valueAsNumber)} /></label>
        </div>
        {settings.sftp.auth === 'private_key' && <label className="field">SSH 私钥<textarea rows={5} autoComplete="off" spellCheck={false} value={settings.sftp.private_key || ''} placeholder={secretPlaceholder(settings.sftp.has_private_key)} onChange={event => sftp('private_key', event.target.value)} /></label>}
        <label className="field">保存目录（不存在会自动创建）<div className="backup-path"><input required value={settings.sftp.directory} onChange={event => sftp('directory', event.target.value)} /><Button type="button" disabled={disabled || dirty} onClick={() => browse('sftp', settings.sftp.directory)}><FolderOpen size={14} />浏览</Button></div></label>
        <p className="backup-fingerprint"><ShieldCheck size={14} /><span>主机指纹：{data.fingerprint || '首次连接成功后自动记录'}<small>记录后，服务器指纹变化会拒绝连接，防止备份被截获。</small></span>{data.fingerprint && <Button type="button" disabled={disabled || dirty} onClick={resetKey}>重置信任</Button>}</p>
        <Button type="button" busy={busy === 'sftp'} disabled={disabled || dirty} onClick={() => test('sftp')}><PlugZap size={14} />测试连接</Button>
      </fieldset>
    </section>
    {browser && <section className="backup-browser"><div><strong>选择{browser.kind === 'oss' ? ' OSS ' : ' SFTP '}目录</strong><Button type="button" onClick={() => setBrowser(null)} aria-label="关闭目录浏览"><X size={14} /></Button></div><code>{browser.listing.path || '/'}</code><div><Button type="button" disabled={disabled || browser.listing.parent === browser.listing.path} onClick={() => browse(browser.kind, browser.listing.parent)}>上一级</Button><Button type="button" variant="primary" disabled={disabled} onClick={() => { if (browser.kind === 'oss') oss('prefix', browser.listing.path); else sftp('directory', browser.listing.path); setBrowser(null); }}>使用此目录</Button></div><ul>{browser.listing.directories.map(path => <li key={path}><button type="button" disabled={disabled} onClick={() => browse(browser.kind, path)}><FolderOpen size={14} />{path}</button></li>)}</ul>{!browser.listing.directories.length && <p className="form-hint">此目录下没有子目录。也可直接输入新目录并保存。</p>}{browser.listing.truncated && <p className="form-hint">目录过多，仅显示前 1000 项；可直接填写完整路径。</p>}</section>}
    <div className="backup-save"><Button type="submit" variant="primary" busy={busy === 'save'} disabled={disabled || !dirty}><Save size={15} />保存设置</Button><small>{dirty ? '有未保存的修改，请先保存再测试或备份' : '配置已保存'}</small></div>
    <p className="form-hint"><DatabaseBackup size={13} /> 关闭总开关不会删除远程备份；保留份数只在该目标上传成功后执行。密钥仅加密保存在服务器，页面不会回显。</p>
  </form>;
}
