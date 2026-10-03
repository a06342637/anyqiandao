import { createContext, useContext, useEffect, useId, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { AlertCircle, ChevronLeft, ChevronRight, Inbox, LoaderCircle, RefreshCw, TriangleAlert, X } from 'lucide-react';
import type { SiteBranding } from './types';

export const DEFAULT_BRANDING: SiteBranding = { site_name: 'any签到助手', site_icon_text: '签' };
export const BrandingContext = createContext<SiteBranding>(DEFAULT_BRANDING);
export const useBranding = () => useContext(BrandingContext);

export const ToastContext = createContext<(message: string, kind?: 'success' | 'error' | 'info' | 'progress') => void>(() => {});
export const useToast = () => useContext(ToastContext);

export interface ConfirmOptions { title: string; message?: string; confirmLabel?: string; danger?: boolean; }
export const ConfirmContext = createContext<(options: ConfirmOptions) => Promise<boolean>>(() => Promise.resolve(false));
export const useConfirm = () => useContext(ConfirmContext);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<(ConfirmOptions & { resolve: (value: boolean) => void }) | null>(null);
  const confirm = (options: ConfirmOptions) => new Promise<boolean>(resolve => setPending({ ...options, resolve }));
  const settle = (value: boolean) => { pending?.resolve(value); setPending(null); };
  return <ConfirmContext.Provider value={confirm}>{children}{pending && <Modal title={pending.title} onClose={() => settle(false)} role="alertdialog"><div className="confirm-body">{pending.danger ? <span className="confirm-icon danger"><TriangleAlert size={22} /></span> : <span className="confirm-icon"><AlertCircle size={22} /></span>}<p>{pending.message}</p></div><div className="modal-actions"><Button onClick={() => settle(false)}>取消</Button><Button variant={pending.danger ? 'danger' : 'primary'} onClick={() => settle(true)} autoFocus>{pending.confirmLabel || '确认'}</Button></div></Modal>}</ConfirmContext.Provider>;
}

export function RefreshButton({ onClick, label = '已刷新最新状态' }: { onClick: () => void | boolean | Promise<void | boolean>; label?: string }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    setBusy(true);
    try { if (await onClick() !== false) toast(label, 'success'); }
    catch (error) { toast((error as Error).message || '刷新失败', 'error'); }
    finally { setBusy(false); }
  };
  return <button className={`icon-button ${busy ? 'spin-icon' : ''}`} aria-label="刷新" title="刷新最新状态" onClick={() => void refresh()} disabled={busy}><RefreshCw size={17} /></button>;
}

export interface TrackOptions { label: string; onDone?: () => void; }
export const TrackContext = createContext<(jobIds: string[], options: TrackOptions) => void>(() => {});
export const useTrack = () => useContext(TrackContext);

export function SealMark({ size = 'normal', text }: { size?: 'normal' | 'large'; text?: string }) {
  const branding = useBranding();
  const label = text || branding.site_icon_text;
  return <span className={`seal-mark ${size} ${Array.from(label).length > 1 ? 'two-characters' : ''}`} aria-hidden="true"><span>{label}</span></span>;
}
export function Button({ children, variant = 'secondary', busy, className = '', ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'ghost' | 'danger'; busy?: boolean }) {
  return <button type="button" {...props} disabled={props.disabled || busy} className={`button ${variant} ${className}`}>{busy && <LoaderCircle size={16} className="spin" />}{children}</button>;
}
const labels: Record<string, string> = {
  valid: '有效', invalid: '已失效', unknown: '未检测', blocked: '需人工处理', pending: '等待中', running: '执行中',
  success: '已完成', signed: '签到成功', already_signed: '已签到', needs_manual: '需人工处理', cancelled: '已取消',
  network_error: '网络异常', proxy_error: '代理异常', login_error: '登录未完成', invalid_credentials: '账密有误', password_required: '需输入密码',
  script_error: '脚本异常', checkin_failed: '签到失败', uncertain: '结果待确认', upstream_error: '网站异常', error: '执行异常',
  healthy: '连通', unavailable: '不可用', needs_trust: '待确认指纹', host_key_required: '待确认指纹', browser_missing: '缺少浏览器',
  missing_session: '凭证不完整',
};
export const kindLabel = (kind: string) => ({ extract: '提取凭证', validate: '有效性检测', checkin: '签到', insights: '账号数据查询', proxy_test: '代理测试', script: '签到脚本', backup: '备份', system: '系统' }[kind] || kind);
export function Badge({ status, label }: { status: string; label?: string }) {
  const tone = ['valid', 'success', 'signed', 'already_signed', 'healthy'].includes(status) ? 'green' : ['invalid', 'invalid_credentials', 'error', 'unavailable', 'script_error', 'checkin_failed'].includes(status) ? 'red' : ['running'].includes(status) ? 'blue' : ['blocked', 'needs_manual', 'needs_trust', 'uncertain', 'network_error', 'proxy_error', 'host_key_required'].includes(status) ? 'amber' : 'gray';
  return <span className={`badge ${tone}`}><span className="badge-dot" />{label || labels[status] || status}</span>;
}
export function EmptyState({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return <div className="empty-state"><span className="empty-icon"><Inbox size={24} strokeWidth={1.4} /></span><strong>{title}</strong><p>{description}</p>{action}</div>;
}
export function LoadingState({ label }: { label: string }) {
  return <div className="empty-state" role="status" aria-busy="true"><LoaderCircle size={24} className="spin" /><strong>{label}</strong><p>正在读取最新数据，请稍候。</p></div>;
}
export function ErrorNotice({ message, retry }: { message: string; retry?: () => void }) {
  if (!message) return null;
  return <div className="notice error-notice" role="alert"><AlertCircle size={17} /><span>{message}</span>{retry && <button onClick={() => { void Promise.resolve(retry()).catch(() => {}); }}>重试</button>}</div>;
}
export const PAGE_SIZES = [5, 10, 15, 20, 30, 50];
export function usePageClamp(page: number, total: number | undefined, limit: number, onChange: (page: number) => void) {
  useEffect(() => { if (total !== undefined && page > Math.max(1, Math.ceil(total / limit))) onChange(Math.max(1, Math.ceil(total / limit))); }, [page, total, limit, onChange]);
}
export function usePageSize(key: string, fallback = 10): [number, (value: number) => void] {
  const [size, setSize] = useState(() => { try { const stored = Number(localStorage.getItem(`any-page-size:${key}`)); return PAGE_SIZES.includes(stored) ? stored : fallback; } catch { return fallback; } });
  const update = (value: number) => { setSize(value); try { localStorage.setItem(`any-page-size:${key}`, String(value)); } catch {} };
  return [size, update];
}
export function Pagination({ page, total, limit, onChange, onLimitChange }: { page: number; total: number; limit: number; onChange: (page: number) => void; onLimitChange?: (limit: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / limit));
  const [draft, setDraft] = useState('');
  const jump = () => { const target = Number(draft); if (Number.isInteger(target) && target >= 1 && target <= pages) onChange(target); setDraft(''); };
  const window = [...new Set([1, page - 1, page, page + 1, pages].filter(value => value >= 1 && value <= pages))].sort((a, b) => a - b);
  return <div className="pagination"><span className="pagination-summary">共 {total.toLocaleString()} 项 · 第 {page} / {pages} 页</span>
    <div className="pagination-controls">
      {onLimitChange && <label className="page-size">每页<select aria-label="每页条数" value={limit} onChange={event => { onLimitChange(Number(event.target.value)); onChange(1); }}>{PAGE_SIZES.map(size => <option key={size} value={size}>{size}</option>)}</select></label>}
      <Button aria-label="上一页" disabled={page <= 1} onClick={() => onChange(page - 1)}><ChevronLeft size={16} /></Button>
      {window.map((value, index) => <span key={value} className="page-numbers">{index > 0 && value - window[index - 1] > 1 && <span className="page-gap">…</span>}<button className={`page-number ${value === page ? 'current' : ''}`} onClick={() => onChange(value)} aria-current={value === page ? 'page' : undefined}>{value}</button></span>)}
      <Button aria-label="下一页" disabled={page >= pages} onClick={() => onChange(page + 1)}><ChevronRight size={16} /></Button>
      {pages > 5 && <input className="page-jump" aria-label="跳转页码" placeholder="页码" value={draft} onChange={event => setDraft(event.target.value.replace(/\D/g, ''))} onKeyDown={event => { if (event.key === 'Enter') jump(); }} onBlur={jump} />}
    </div></div>;
}
export function Modal({ title, subtitle, children, onClose, wide = false, role = 'dialog', className = '' }: { title: string; subtitle?: string; children: ReactNode; onClose: () => void; wide?: boolean; role?: 'dialog' | 'alertdialog'; className?: string }) {
  const titleId = useId();
  const container = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose); closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const frame = requestAnimationFrame(() => container.current?.querySelector<HTMLElement>('button, input, select, textarea')?.focus());
    const listener = (event: KeyboardEvent) => {
      const dialogs = document.querySelectorAll('[aria-modal="true"]');
      if (dialogs[dialogs.length - 1] !== container.current) return;
      if (event.key === 'Escape') closeRef.current();
      if (event.key === 'Tab') {
        const focusable = container.current?.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]');
        if (!focusable?.length) return;
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener('keydown', listener);
    return () => { cancelAnimationFrame(frame); document.body.style.overflow = overflow; document.removeEventListener('keydown', listener); previous?.focus(); };
  }, []);
  return createPortal(<div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><div ref={container} className={`modal ${wide ? 'wide' : ''} ${className}`} role={role} aria-modal="true" aria-labelledby={titleId}><header className="modal-heading"><div><h2 id={titleId}>{title}</h2>{subtitle && <p>{subtitle}</p>}</div><button className="icon-button" aria-label="关闭窗口" onClick={onClose}><X size={20} /></button></header>{children}</div></div>, document.body);
}
