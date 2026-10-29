import { useEffect, useRef, useState, type FormEvent } from 'react';
import { CalendarClock, Clock3, FileText, Pause, Pencil, Play, Plus, ShieldCheck, Sparkles, Trash2, Users } from 'lucide-react';
import { api, formatTime, refreshData, useResource } from './api';
import { Badge, Button, EmptyState, ErrorNotice, LoadingState, Modal, Pagination, RefreshButton, useConfirm, usePageClamp, usePageSize, useToast, useTrack } from './ui';
import { AccountPicker, SelectedAccounts } from './selection';
import { AccountCheckinRouting, RouteSelect } from './NetworkRouting';
import type { NetworkRoute, PageData, ProxyOption, Schedule, Selection } from './types';

export default function SchedulesPage({ timezone, onLogs }: { timezone: string; onLogs: () => void }) {
  const toast = useToast();
  const confirm = useConfirm();
  const track = useTrack();
  const [page, setPage] = useState(1);
  const [limit, setLimit] = usePageSize('schedules', 10);
  const { data, error, reload } = useResource<PageData<Schedule> & { auto_schedule_id: string | null }>(`/schedules?page=${page}&limit=${limit}`);
  usePageClamp(page, data?.total, limit, setPage);
  const [editing, setEditing] = useState<Schedule | null | undefined>(undefined);
  const [viewing, setViewing] = useState<Schedule | null>(null);
  const [busy, setBusy] = useState('');
  const control = async (schedule: Schedule, action: 'pause' | 'resume' | 'run' | 'delete') => {
    if (action === 'delete' && !await confirm({ title: `删除计划「${schedule.name}」？`, message: schedule.id === data?.auto_schedule_id ? '这是默认自动计划。删除后，新提取的账号仍会重新创建一个默认计划。已保存的账号和凭证不会删除。' : '已保存的账号和凭证不会删除，只移除这个计划及其等待中的任务。', confirmLabel: '删除', danger: true })) return;
    if (action === 'run' && !await confirm({ title: schedule.mode === 'random_window' ? '重新安排随机时间？' : '立即执行一次？', message: schedule.mode === 'random_window' ? '重新分配尚未执行账号的随机时间；已执行账号不重复安排。窗口已过则安排次日。' : `会为此计划中的 ${schedule.account_count} 个账号各加入一次签到任务；已失效凭证会先自动重新提取。`, confirmLabel: schedule.mode === 'random_window' ? '重新安排' : '立即执行' })) return;
    setBusy(schedule.id);
    try {
      const result = await api<{ queued?: number; job_ids?: string[]; planned?: boolean }>(`/schedules/${schedule.id}${action === 'delete' ? '' : '/' + action}`, action === 'delete' ? 'DELETE' : 'POST');
      if (action === 'run') { if (result.planned) toast('随机执行时间已重新安排，可查看分配列表', 'success'); else if (result.queued) track(result.job_ids || [], { label: `计划「${schedule.name}」签到 ${result.queued} 个账号` }); else toast('没有可签到的账号：计划为空，或账号已失效 / 已有任务在队列中', 'error'); }
      else toast({ pause: '计划已暂停，等待中的任务已取消', resume: '计划已继续，下一次按间隔执行', delete: '计划已删除' }[action], 'success');
      refreshData();
    } catch (caught) { toast((caught as Error).message, 'error'); } finally { setBusy(''); }
  };
  return <><div className="notice schedule-notice"><ShieldCheck size={19} /><div><strong>开启全自动后，提取成功的账号自动加入默认计划</strong><p>AnyRouter 使用签到接口；AgentRouter 用保存的账号密码在新会话登录并查询用户信息。支持固定间隔或每天随机时间窗口，结果均核对实际到账。</p></div></div><section className="panel"><div className="panel-heading"><div className="heading-icon"><CalendarClock size={19} /></div><div><h2>我的签到计划</h2><p>所有计划与手动任务统一排队，一个结束后再执行下一个</p></div><div className="panel-tools"><RefreshButton onClick={reload} label="签到计划已刷新" /><Button variant="primary" onClick={() => setEditing(null)}><Plus size={16} />创建计划</Button></div></div><ErrorNotice message={error} retry={reload} />
    {!data?.items.length ? <EmptyState title="还没有签到计划" description="选择已提取凭证的账号，设置一个执行间隔，助手会按时签到。" action={<Button variant="primary" onClick={() => setEditing(null)}><Plus size={16} />创建计划</Button>} /> : <div className="schedule-grid">{data.items.map(schedule => <article className="schedule-card" key={schedule.id}><div className="schedule-card-top"><span className="schedule-icon"><CalendarClock size={22} /></span><span className={`badge ${schedule.enabled ? 'green' : 'gray'}`}><span className="badge-dot" />{schedule.enabled ? '运行中' : '已暂停'}</span></div><h3>{schedule.name}{schedule.id === data?.auto_schedule_id && <span className="auto-tag"><Sparkles size={12} />自动</span>}</h3><div className="schedule-details"><span><Users size={15} />{schedule.account_count} 个账号</span><span><Clock3 size={15} />{schedule.mode === 'random_window' ? `每天 ${schedule.window_start}–${schedule.window_end}（北京时间）` : <>每 {schedule.interval_minutes % 60 === 0 ? `${schedule.interval_minutes / 60} 小时` : `${schedule.interval_minutes} 分钟`}</>}</span></div><div className="schedule-next"><span>下一次执行</span><strong>{schedule.enabled ? formatTime(schedule.next_run, schedule.mode === 'random_window' ? 'Asia/Shanghai' : timezone) : '计划已暂停'}</strong><small>最近调度：{formatTime(schedule.last_run, timezone)}</small></div><div className="schedule-actions"><Button disabled={!!busy} onClick={() => control(schedule, 'run')}><Play size={14} />{schedule.mode === 'random_window' ? '重新随机安排' : '执行一次'}</Button>{schedule.mode === 'random_window' && <Button onClick={() => setViewing(schedule)}><Clock3 size={14} />查看随机安排</Button>}<Button disabled={!!busy} onClick={() => control(schedule, schedule.enabled ? 'pause' : 'resume')}>{schedule.enabled ? <Pause size={14} /> : <Play size={14} />}{schedule.enabled ? '暂停' : '继续'}</Button><button className="icon-button small" disabled={!!busy} aria-label={`编辑 ${schedule.name}`} onClick={() => setEditing(schedule)}><Pencil size={15} /></button><button className="icon-button small" disabled={!!busy} aria-label={`删除 ${schedule.name}`} onClick={() => control(schedule, 'delete')}><Trash2 size={15} /></button></div></article>)}</div>}
    <Pagination page={page} total={data?.total || 0} limit={limit} onChange={setPage} onLimitChange={setLimit} />
    <div className="panel-footnote"><span>间隔从保存或恢复计划时起计算，首次不会立即执行。服务中断不会补发不确定的签到请求。</span><button className="text-button push-right" onClick={onLogs}><FileText size={14} />查看日志</button></div></section>
    <AccountCheckinRouting />
    {editing !== undefined && <ScheduleEditor schedule={editing} onClose={() => setEditing(undefined)} />}
    {viewing && <RandomAssignments schedule={viewing} onClose={() => setViewing(null)} />}</>;
}

function ScheduleEditor({ schedule, onClose }: { schedule: Schedule | null; onClose: () => void }) {
  const toast = useToast();
  const [name, setName] = useState(schedule?.name || '每日签到');
  const [networkRoute, setNetworkRoute] = useState<NetworkRoute>(schedule?.network_route || { mode: 'inherit' });
  const nodes = useResource<{ items: ProxyOption[] }>('/proxies/options', 0);
  const initialMinutes = schedule?.interval_minutes || 1440;
  const [unit, setUnit] = useState(initialMinutes % 60 === 0 ? 60 : 1);
  const [interval, setInterval] = useState(initialMinutes % 60 === 0 ? initialMinutes / 60 : initialMinutes);
  const [enabled, setEnabled] = useState(schedule ? !!schedule.enabled : true);
  const [mode, setMode] = useState<'interval' | 'random_window'>(schedule?.mode || 'interval');
  const [windowStart, setWindowStart] = useState(schedule?.window_start || '13:00');
  const [windowEnd, setWindowEnd] = useState(schedule?.window_end || '18:00');
  const [replace, setReplace] = useState(!schedule);
  const [selection, setSelection] = useState<Selection>({ ids: [] });
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const minutes = mode === 'random_window' ? 1440 : interval * unit;
    if (mode === 'random_window' && (!windowStart || windowStart >= windowEnd)) { toast('请选择同一天内结束晚于开始的时间范围', 'error'); return; }
    if (!Number.isInteger(minutes) || minutes < 5 || minutes > 525600) { toast('签到间隔须为 5 分钟至 365 天', 'error'); return; }
    setBusy(true);
    try { await api(schedule ? `/schedules/${schedule.id}` : '/schedules', schedule ? 'PUT' : 'POST', { ...selection, name, interval_minutes: minutes, mode, window_start: windowStart, window_end: windowEnd, enabled, network_route: networkRoute, keep_accounts: !!schedule && !replace }); refreshData(); toast('签到计划已保存', 'success'); onClose(); }
    catch (error) { toast((error as Error).message, 'error'); } finally { setBusy(false); }
  };
  return <Modal title={schedule ? '编辑签到计划' : '创建签到计划'} subtitle={mode === 'random_window' ? '按北京时间为每个账号分配随机时间；刷新或重启不重新抽签，错过整个窗口不会集中补发。' : '保存后按间隔调度，不会立即触发签到。'} wide className="schedule-editor" onClose={onClose}><form onSubmit={submit}><div className="schedule-editor-content"><label className="field">执行方式<select aria-label="签到执行方式" value={mode} onChange={event => setMode(event.target.value as 'interval' | 'random_window')}><option value="interval">固定间隔</option><option value="random_window">每日随机时间窗口</option></select></label>{mode === 'random_window' && <div className="form-grid"><label className="field">开始时间（北京时间）<input type="time" aria-label="随机开始时间" value={windowStart} onChange={event => setWindowStart(event.target.value)} required /></label><label className="field">结束时间（北京时间）<input type="time" aria-label="随机结束时间" value={windowEnd} onChange={event => setWindowEnd(event.target.value)} required /></label></div>}<div className="form-grid"><label className="field">计划名称<input value={name} onChange={event => setName(event.target.value)} required maxLength={100} /></label>{mode === 'interval' && <label className="field">执行间隔<div className="interval-input"><span>每</span><input type="number" min={1} step={1} value={interval} onChange={event => setInterval(Number(event.target.value))} required /><select aria-label="签到间隔单位" value={unit} onChange={event => setUnit(Number(event.target.value))}><option value={1}>分钟</option><option value={60}>小时</option><option value={1440}>天</option></select></div></label>}</div><label className="field">本计划签到线路<RouteSelect inherit value={networkRoute} onChange={setNetworkRoute} proxies={nodes.data?.items || []} label="本计划签到线路" /><span className="form-hint">跟随设置时使用账号指定线路；显式选择直连或代理时，本计划统一使用该线路。</span></label><label className="check-label"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} />启用此计划</label>{schedule && <div className="notice compact-notice"><span>原计划包含 {schedule.account_count} 个账号</span><label className="check-label"><input type="checkbox" checked={replace} onChange={event => { setReplace(event.target.checked); setSelection({ ids: [] }); }} />重新选择账号</label></div>}{replace && <AccountPicker onChange={setSelection} />}<SelectedAccounts scheduleId={!replace ? schedule?.id : undefined} selection={replace ? selection : undefined} /></div><div className="modal-actions"><Button onClick={onClose}>取消</Button><Button type="submit" variant="primary" busy={busy} disabled={busy || replace && !selection.all_matching && !selection.ids?.length}>保存计划</Button></div></form></Modal>;
}

function RandomAssignments({ schedule, onClose }: { schedule: Schedule; onClose: () => void }) {
  const [page, setPage] = useState(1);
  const shownDay = useRef<string | null>(null);
  const { data, error, reload } = useResource<PageData<{ account_id: string; username: string; provider: string; day: string; due_at: number; status: string }> & { day: string | null; next_day: boolean }>('/schedules/' + schedule.id + '/assignments?page=' + page + '&limit=20');
  usePageClamp(page, data?.total, 20, setPage);
  useEffect(() => {
    if (!data?.day) return;
    if (shownDay.current && shownDay.current !== data.day) setPage(1);
    shownDay.current = data.day;
  }, [data?.day]);
  return <Modal title={schedule.name + ' · 随机安排'} subtitle="北京时间；到点进入串行队列，实际开始时间受资源与前序任务影响。窗口结束后未开始的任务取消，不集中补发。" onClose={onClose} wide><ErrorNotice message={error} retry={reload} />{data?.day && <div className="random-assignment-heading"><strong>{data.next_day ? '下一天的随机安排' : '本日随机安排'} · {data.day}</strong>{data.next_day && <span>本日安排已结束，显示下一次签到时间</span>}</div>}{!data ? !error && <LoadingState label="正在读取随机安排" /> : !data.items.length ? <EmptyState title="暂无随机安排" description="启用计划并选择账号后，将显示每个账号的执行时间。" /> : <div className="job-list">{data.items.map(item => <div className="job-row" key={item.day + item.account_id}><div className="job-description"><strong>{item.username}</strong><p>{item.provider === 'agentrouter' ? 'AgentRouter' : 'AnyRouter'} · {item.day}</p></div><span>{formatTime(item.due_at, 'Asia/Shanghai')}</span><Badge status={item.status} /></div>)} </div>}{data && <Pagination page={page} total={data.total} limit={20} onChange={setPage} />}</Modal>;
}
