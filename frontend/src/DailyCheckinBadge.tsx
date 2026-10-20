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
  return <span className="daily-checkin-state"><small title={`${state.checkin_day} · 北京时间统计日；实际领取资格由网站决定${state.manual_confirmation ? '；已由管理员确认，不计新增奖励' : ''}`} className={`badge ${state.today_checkin === 'confirmed' ? 'green' : ['uncertain', 'no_credit'].includes(state.today_checkin) ? 'amber' : 'gray'}`}>{state.manual_confirmation ? '今日已确认' : state.today_checkin === 'confirmed' ? '今日有到账记录' : state.today_checkin === 'no_credit' ? '本次未到账' : state.today_checkin === 'uncertain' ? '本次结果待确认' : '今日未确认签到'}</small>{['uncertain', 'no_credit'].includes(state.today_checkin) && <button className="text-button" disabled={busy} aria-label={`确认 ${username} 今日签到`} title="确认已自行处理，清除今日待确认提示；不增加奖励" onClick={() => void confirm()}>{busy ? '确认中…' : '确认'}</button>}</span>;
}
