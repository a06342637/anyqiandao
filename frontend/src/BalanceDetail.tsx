import type { CheckinBalance } from './types';

const money = (value: number | null) => value === null ? '未读取' : `$${value.toFixed(4)}`;

export default function BalanceDetail({ balance, compact = false }: { balance: CheckinBalance | null; compact?: boolean }) {
  if (!balance) return null;
  const measured = balance.balance_source === 'live';
  const confirmed = measured && balance.earned !== null;
  return <div className={`balance-detail${compact ? ' compact' : ''}`} aria-label={compact ? '最近签到新增' : '最近签到余额变化'}>
    {!compact && <div className="balance-pair"><span>签到前<b>{money(balance.quota_before)}</b></span><span className="balance-arrow">→</span><span>签到后<b>{money(balance.quota_after)}</b></span></div>}
    {confirmed ? <span className={`balance-gain ${balance.earned! > 0 ? 'positive' : ''}`}>签到新增 +{money(balance.earned)}{!compact && balance.delta !== null && balance.delta < 0 ? ' · 含期间消耗' : ''}</span>
      : !measured ? <span className="balance-hint">历史记录 · 旧余额差不计入新增</span>
      : balance.code === 'already_signed' ? <span className="balance-hint">本次已签过 · 不重复计入新增</span>
      : <span className="balance-hint">新增未确认 · 详见日志</span>}
  </div>;
}
