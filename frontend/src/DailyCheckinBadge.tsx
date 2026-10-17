import type { DailyCheckin } from './types';
import { useState } from 'react';
import { api, refreshData } from './api';
import { useToast } from './ui';

export default function DailyCheckinBadge({ state, accountId, username }: { state: DailyCheckin; accountId: string; username: string }) {
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const confirm = async () => {
    setBusy(true);
    try {
      await api(`/accounts/${encodeURIComponent(accountId)}/checkin/confirm`, 'POST', { day: state.checkin_day });
      refreshData();
      toast('已确认处理，待确认提示已清除；未增加签到奖励', 'success');
    } catch (error) { toast((error as Error).message, 'error'); }
    finally { setBusy(false); }
  };
  return <span className="daily-checkin-state"><small title={`${state.checkin_day} · 北京时间每日 00:00 重新划分${state.manual_confirmation ? '；已由管理员确认，不计新增奖励' : ''}`} className={`badge ${state.today_checkin === 'confirmed' ? 'green' : state.today_checkin === 'uncertain' ? 'amber' : 'gray'}`}>{state.manual_confirmation ? '今日已确认' : state.today_checkin === 'confirmed' ? '今日已签' : state.today_checkin === 'uncertain' ? '今日待确认到账' : '今日未确认签到'}</small>{state.today_checkin === 'uncertain' && <button className="text-button" disabled={busy} aria-label={`确认 ${username} 今日签到`} title="确认已自行处理，清除今日待确认提示；不增加奖励" onClick={() => void confirm()}>{busy ? '确认中…' : '确认'}</button>}</span>;
}
