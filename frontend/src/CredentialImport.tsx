import { useState, type FormEvent } from 'react';
import { KeyRound, LockKeyhole } from 'lucide-react';
import { api, refreshData } from './api';
import { Button, useToast, useTrack } from './ui';
import type { Account } from './types';

interface CredentialRow { username?: string; session: string; api_user: string; }
interface ImportResult { added: number; skipped: number; queued: number; job_ids: string[]; }

export function parseCredentials(text: string): CredentialRow[] {
  const value = text.trim();
  if (!value) throw new Error('请先粘贴凭证');
  let entries: unknown[];
  if (value.startsWith('[') || value.startsWith('{')) {
    let parsed: unknown;
    try { parsed = JSON.parse(value); } catch { throw new Error('JSON 格式不正确，请使用凭证数组或 session----api_user'); }
    entries = Array.isArray(parsed) ? parsed : [parsed];
  } else {
    entries = value.split(/\r?\n/).filter(line => line.trim()).map((line, index) => {
      const parts = line.split('----');
      if (parts.length !== 2) throw new Error(`第 ${index + 1} 行格式有歧义，请改用 JSON 凭证格式`);
      return { session: parts[0].trim(), api_user: parts[1].trim() };
    });
  }
  if (!entries.length) throw new Error('凭证列表为空');
  return entries.map((entry, index) => {
    if (!entry || typeof entry !== 'object') throw new Error(`第 ${index + 1} 条不是有效凭证对象`);
    const row = entry as { username?: unknown; name?: unknown; session?: unknown; cookies?: { session?: unknown }; api_user?: unknown };
    const session = row.session ?? row.cookies?.session;
    if (typeof row.api_user === 'number' && !Number.isSafeInteger(row.api_user)) throw new Error(`第 ${index + 1} 条 api_user 请使用带引号的数字字符串`);
    const apiUser = String(row.api_user ?? '').trim();
    if (typeof session !== 'string' || !session.trim() || session.trim().length > 65536 || !/^\d{1,19}$/.test(apiUser) || BigInt(apiUser) < 1n || BigInt(apiUser) > 9223372036854775807n) throw new Error(`第 ${index + 1} 条缺少有效的 session 或 api_user`);
    const username = row.username ?? row.name;
    return { session: session.trim(), api_user: BigInt(apiUser).toString(), ...(typeof username === 'string' && username.trim() ? { username: username.trim() } : {}) };
  });
}

export default function CredentialImport({ account, onDone }: { account?: Account; onDone?: () => void }) {
  const [username, setUsername] = useState('');
  const [session, setSession] = useState('');
  const [apiUser, setApiUser] = useState('');
  const [bulk, setBulk] = useState(false);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState('');
  const toast = useToast();
  const track = useTrack();
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setProgress('');
    let added = 0, skipped = 0;
    try {
      const rows = parseCredentials(bulk ? text : JSON.stringify([{ username, session, api_user: apiUser }]));
      const jobIds: string[] = [];
      if (account) {
        const result = await api<{ job_ids: string[] }>(`/accounts/${account.id}/credentials`, 'PUT', { session: rows[0].session, api_user: rows[0].api_user });
        jobIds.push(...result.job_ids); added = 1;
      } else {
        let buffer: CredentialRow[] = [], bytes = 0;
        const send = async () => {
          const result = await api<ImportResult>('/accounts/import-credentials', 'POST', { accounts: buffer });
          added += result.added; skipped += result.skipped; jobIds.push(...result.job_ids); buffer = []; bytes = 0;
          setProgress(`已保存 ${added} 个，跳过 ${skipped} 个重复账号`); refreshData();
        };
        for (const row of rows) {
          const size = new TextEncoder().encode(JSON.stringify(row)).length;
          if (buffer.length && (buffer.length >= 100 || bytes + size > 400000)) await send();
          buffer.push(row); bytes += size;
        }
        if (buffer.length) await send();
      }
      setSession(''); setApiUser(''); setText(''); setUsername(''); refreshData();
      if (jobIds.length) track(jobIds, { label: `Session 账号签到 ${jobIds.length} 个` });
      const message = account ? '凭证已更新' : `已导入 ${added} 个 Session 账号${skipped ? `，跳过 ${skipped} 个重复账号（更新请到账号列表）` : ''}`;
      setProgress(message); toast(`${message}；${jobIds.length ? '只提交签到，不执行登录' : '未触发登录，可在账号列表签到'}`, 'success');
      onDone?.();
    } catch (caught) { toast(`${(caught as Error).message}${added ? `；此前已保存 ${added} 个，其余未提交` : ''}`, 'error'); }
    finally { setBusy(false); }
  };
  return <form onSubmit={submit} autoComplete="off" className="credential-import">
    {!account && <div className="segmented inline credential-mode"><button type="button" disabled={busy} className={!bulk ? 'selected' : ''} onClick={() => setBulk(false)}>单个凭证</button><button type="button" disabled={busy} className={bulk ? 'selected' : ''} onClick={() => setBulk(true)}>批量凭证</button></div>}
    {bulk ? <><label className="field">粘贴凭证或 GitHub 配置<textarea rows={7} required disabled={busy} value={text} onChange={event => setText(event.target.value)} spellCheck={false} maxLength={8388608} placeholder={'session值----api_user\n或 [{"cookies":{"session":"..."},"api_user":"123"}]'} /></label><p className="form-hint">一行一组 session----api_user，也支持 JSON 对象或数组；可选 username 作为账号显示名称。只需凭证，不需要密码。</p></> : <>
      <label className="field">{account ? '账号' : '账号名称（可选）'}<input value={account?.username || username} readOnly={!!account} disabled={busy} maxLength={512} onChange={event => setUsername(event.target.value)} placeholder="留空以 api_user 标识，不作为登录用户名" /></label>
      <label className="field">session<input type="password" value={session} onChange={event => setSession(event.target.value)} required maxLength={65536} disabled={busy} autoComplete="new-password" placeholder="粘贴 session 值，也可使用 session=值" /></label>
      <label className="field">api_user<input value={apiUser} onChange={event => setApiUser(event.target.value)} required inputMode="numeric" pattern="[0-9]+" maxLength={19} disabled={busy} placeholder="账号对应的数字用户 ID" /></label>
    </>}
    <div className="notice credential-notice"><LockKeyhole size={16} /><span>识别为 <strong>Session 手动导入 · 仅签到</strong>。凭证加密保存；失效后保留账号并暂停后续自动处理，等待你手动更新，不尝试密码登录。{account && ' api_user 必须与原账号一致。'}</span></div>
    <p className="form-hint">开启「提取后自动签到」时，保存后签到一次并加入默认计划；关闭时仅保存。重复账号不会覆盖已有密码或 Cookie。</p>
    <Button variant="primary" type="submit" busy={busy} className="full-width"><KeyRound size={16} />{account ? '更新凭证' : '导入 Session 凭证'}</Button>
    {progress && <p className="import-progress" role="status">{progress}</p>}
  </form>;
}
