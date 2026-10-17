import type { DailyCheckin } from './types';

export default function DailyCheckinBadge({ state }: { state: DailyCheckin }) {
  return <small title={`${state.checkin_day} · 北京时间每日 00:00 重新划分；未确认不代表网站一定未签`} className={`badge ${state.today_checkin === 'confirmed' ? 'green' : state.today_checkin === 'uncertain' ? 'amber' : 'gray'}`}>{state.today_checkin === 'confirmed' ? '今日已签' : state.today_checkin === 'uncertain' ? '今日待确认到账' : '今日未确认签到'}</small>;
}
