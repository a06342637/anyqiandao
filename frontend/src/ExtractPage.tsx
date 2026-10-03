import { useRef, useState, type FormEvent } from 'react';
import Papa from 'papaparse';
import { ArrowDownToLine, ArrowRight, Eye, EyeOff, FileUp, KeyRound, ListOrdered, LockKeyhole, ShieldCheck } from 'lucide-react';
import { api, downloadBlob, newId, refreshData } from './api';
import { Button, useToast, useTrack } from './ui';
import type { Dashboard } from './types';
import CredentialImport from './CredentialImport';

interface ImportRow { username: string; password: string; row_id: string; }
interface ImportResult { queued: number; skipped: number; account_ids: string[]; job_ids: string[]; duplicate_count: number; }
const rowBytes = (row: ImportRow) => new TextEncoder().encode(JSON.stringify(row)).length;

export default function ExtractPage({ onQueue, onAccounts, onSettings, dashboard }: {
  onQueue: () => void; onAccounts: () => void; onSettings: () => void; dashboard: Dashboard | null;
}) {
  const toast = useToast();
  const track = useTrack();
  const [mode, setMode] = useState<'single' | 'bulk' | 'session'>('single');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [visible, setVisible] = useState(false);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);

  const submitRows = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setProgress('正在加密入队…');
    let queued = 0, skipped = 0, duplicates = 0;
    try {
      const importId = newId();
      const lines = mode === 'bulk' ? text.split(/\r?\n/).filter(line => line.trim()) : [];
      const total = mode === 'single' ? 1 : lines.length;
      if (!total) throw new Error('请先输入账号');
      let buffer: ImportRow[] = [], bytes = 0;
      const jobIds: string[] = [];
      const send = async () => {
        const result = await api<ImportResult>('/accounts/import', 'POST', { import_id: importId, accounts: buffer });
        queued += result.queued; skipped += result.skipped; duplicates += result.duplicate_count; jobIds.push(...result.job_ids); buffer = []; bytes = 0; refreshData();
      };
      for (let index = 0; index < total; index += 1) {
        let row: ImportRow;
        if (mode === 'single') row = { username: username.trim(), password, row_id: '1' };
        else {
          const line = lines[index], divider = line.indexOf('----');
          if (divider < 1 || !line.slice(divider + 4)) throw new Error(`第 ${index + 1} 行格式不正确，请使用账号----密码`);
          if (line.indexOf('----', divider + 4) !== -1) throw new Error(`第 ${index + 1} 行存在分隔歧义，请改用 CSV 上传，密码不会被猜测或截断`);
          row = { username: line.slice(0, divider).trim(), password: line.slice(divider + 4), row_id: String(index + 1) };
        }
        buffer.push(row); bytes += rowBytes(row);
        if (buffer.length >= 100 || bytes >= 400000 || index === total - 1) {
          await send(); setProgress(`已提交 ${index + 1} / ${total} 行`);
        }
      }
      setPassword(''); setText(''); setProgress(`已加入 ${queued} 个任务${duplicates ? `，跳过 ${duplicates} 个已添加账号` : skipped ? `，跳过 ${skipped} 个重复请求` : ''}`);
      if (jobIds.length) track(jobIds, { label: `提取 ${jobIds.length} 个账号` });
      else toast(duplicates ? '账号已添加，不能重复添加；重新提取或修改密码请前往账号列表' : '没有新增提取任务', 'info');
    } catch (error) {
      toast(`${(error as Error).message}${queued ? `；此前已有 ${queued} 个任务入队，不会撤销` : ''}`, 'error');
      setProgress(queued ? `已有 ${queued} 个任务入队，其余尚未提交` : '提交未完成，请检查输入');
    } finally { setBusy(false); refreshData(); }
  };

  const importCsv = async (file: File) => {
    setBusy(true); setProgress('正在分块读取 CSV…');
    const importId = newId();
    let rowCount = 0, queued = 0, duplicates = 0, failed = false;
    const jobIds: string[] = [];
    const send = async (rows: ImportRow[]) => {
      const result = await api<ImportResult>('/accounts/import', 'POST', { import_id: importId, accounts: rows });
      jobIds.push(...result.job_ids); queued += result.queued; duplicates += result.duplicate_count; setProgress(`已读取 ${rowCount} 行 · 已入队 ${queued} 个任务 · 跳过 ${duplicates} 个已添加账号`); refreshData();
    };
    try {
      await new Promise<void>((resolve, reject) => {
        let buffer: ImportRow[] = [], bytes = 0;
        Papa.parse<Record<string, string>>(file, {
          header: true, skipEmptyLines: 'greedy', chunkSize: 262144,
          transformHeader: header => header.replace(/^\uFEFF/, '').trim().toLowerCase(),
          step: (result, parser) => {
            rowCount += 1;
            const account = result.data.username || result.data.email || result.data.account || result.data['账号'];
            const secret = result.data.password ?? result.data['密码'];
            if (result.errors.length || !account?.trim() || typeof secret !== 'string' || !secret) {
              failed = true; parser.abort(); reject(new Error(`CSV 第 ${rowCount} 条数据不正确，请检查 username/password 列、引号和空字段`)); return;
            }
            const row = { username: account.trim(), password: secret, row_id: String(rowCount) };
            buffer.push(row); bytes += rowBytes(row);
            if (buffer.length >= 100 || bytes >= 400000) {
              parser.pause(); const chunk = buffer; buffer = []; bytes = 0;
              void send(chunk).then(() => parser.resume()).catch(error => { failed = true; parser.abort(); reject(error); });
            }
          },
          complete: () => { if (!failed) void (buffer.length ? send(buffer) : Promise.resolve()).then(resolve).catch(reject); },
          error: () => reject(new Error('CSV 文件读取失败，请使用 UTF-8 编码')),
        });
      });
      if (!rowCount) throw new Error('CSV 中没有账号数据');
      if (jobIds.length) track(jobIds, { label: `CSV 提取 ${jobIds.length} 个账号` });
      else toast(duplicates ? 'CSV 中的账号均已添加，不会重复添加或覆盖密码' : '没有新增提取任务', 'info');
      setProgress(`CSV 导入完成 · ${queued} 个任务已入队${duplicates ? ` · 跳过 ${duplicates} 个已添加账号` : ''}`);
    } catch (error) { toast(`${(error as Error).message}；已入队 ${queued} 个任务`, 'error'); }
    finally { setBusy(false); if (fileInput.current) fileInput.current.value = ''; refreshData(); }
  };

  return <>
    <div className="extract-grid"><section className="panel input-panel"><div className="panel-heading"><div className="heading-icon"><KeyRound size={19} /></div><div><h2>添加账号</h2><p>已添加的账号自动跳过，不重复添加</p></div><span className="small-label push-right">步骤 01</span></div>
      <div className="segmented"><button disabled={busy} className={mode === 'single' ? 'selected' : ''} onClick={() => setMode('single')}>单个账号</button><button disabled={busy} className={mode === 'bulk' ? 'selected' : ''} onClick={() => setMode('bulk')}>批量导入</button><button disabled={busy} className={mode === 'session' ? 'selected' : ''} onClick={() => setMode('session')}>Session 导入</button></div>
      {mode === 'session' ? <CredentialImport /> : <form onSubmit={submitRows} autoComplete="off">
        {mode === 'single' ? <><label className="field">账号<input placeholder="输入用户名或邮箱" value={username} onChange={event => setUsername(event.target.value)} required maxLength={512} disabled={busy} autoComplete="off" /></label><label className="field">密码<div className="password-field"><input type={visible ? 'text' : 'password'} placeholder="输入账号的登录密码" value={password} onChange={event => setPassword(event.target.value)} required maxLength={4096} disabled={busy} autoComplete="new-password" /><button className="icon-button" type="button" aria-label={visible ? '隐藏密码' : '显示密码'} onClick={() => setVisible(!visible)}>{visible ? <EyeOff size={17} /> : <Eye size={17} />}</button></div></label></> : <><label className="field">逐行粘贴账号<textarea rows={5} placeholder={'账号----密码\n每行一个；复杂密码推荐使用 CSV'} value={text} onChange={event => setText(event.target.value)} disabled={busy} autoComplete="off" spellCheck={false} /></label><div className="csv-actions"><input ref={fileInput} type="file" accept=".csv,text/csv" aria-label="上传 CSV 文件" hidden onChange={event => { const file = event.target.files?.[0]; if (file) void importCsv(file); }} /><Button disabled={busy} onClick={() => fileInput.current?.click()}><FileUp size={16} />上传 CSV</Button><Button variant="ghost" disabled={busy} onClick={() => { downloadBlob(new Blob(['\uFEFFusername,password\r\n'], { type: 'text/csv;charset=utf-8' }), '账号导入模板.csv'); toast('CSV 模板已下载', 'success'); }}><ArrowDownToLine size={15} />下载模板</Button></div><p className="form-hint">大批量推荐 CSV，按块上传，无固定账号总数上限。逗号、引号、换行等特殊字符须按 CSV 规范引用。</p></>}
        <div className="secure-note"><LockKeyhole size={15} /><span>账号密码 AES-256 加密保存在你的服务器上，用于自动重新提取；开启全自动时，提取成功后自动签到并加入默认计划。</span></div>
        <Button variant="primary" type="submit" busy={busy} className="full-width">{busy ? '正在提交…' : '加入提取队列'}<ArrowRight size={17} /></Button>
        {progress && <p className="import-progress" role="status">{progress}</p>}
      </form>}
      {dashboard?.proxy_mode === 'pool' && !dashboard.enabled_proxies && <div className="notice warning compact-notice"><span>尚未添加代理。任务将等待，不会自动直连。</span><button className="text-button" onClick={onSettings}>前往设置</button></div>}
    </section><aside className="guide-card"><span className="eyebrow">使用流程</span><h2>先提取，<br />再安排签到。</h2><div className="workflow-step"><span>01</span><div><strong>提取登录凭证</strong><p>隔离的浏览器会话逐个登录，取回 session 与 api_user。</p></div></div><div className="workflow-step"><span>02</span><div><strong>检测与管理账号</strong><p>确认凭证仍然有效，再复制配置或重新提取。</p></div></div><div className="workflow-step"><span>03</span><div><strong>安排自动签到</strong><p>选择账号与执行间隔，内置脚本按计划运行。</p></div></div><div className="guide-bottom"><ShieldCheck size={17} /><span>遇到验证码或风控就停下来提示你，不绕过人工验证。</span></div><Button onClick={onAccounts} className="guide-button">前往账号列表<ArrowRight size={16} /></Button></aside></div>
    <div className="extract-queue-link"><span><ListOrdered size={17} />任务进度、暂停与并发设置已移至独立页面</span><Button onClick={onQueue}>前往执行队列<ArrowRight size={16} /></Button></div>
  </>;
}
