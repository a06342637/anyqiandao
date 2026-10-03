import { useState } from 'react';
import { Trash2 } from 'lucide-react';
import { Button, useToast } from './ui';
import { useSelection } from './selection';
import type { Selection } from './types';

export default function HistoryToolbar({ selection, pageIds, total, busy, onRemove }: {
  selection: ReturnType<typeof useSelection>; pageIds: string[]; total: number; busy: boolean;
  onRemove: (payload: Selection & { before?: number }) => void;
}) {
  const [before, setBefore] = useState('');
  const toast = useToast();
  const removeBefore = () => {
    const timestamp = new Date(before).getTime() / 1000;
    if (!Number.isFinite(timestamp) || timestamp > Date.now() / 1000) { toast('请选择不晚于当前时间的清理截止时间', 'error'); return; }
    onRemove({ all_matching: true, before: timestamp });
  };
  return <div className="history-toolbar">
    <div className="history-selection"><label className="check-label"><input type="checkbox" aria-label="选择本页记录" checked={!!pageIds.length && pageIds.every(selection.selected)} onChange={event => pageIds.forEach(id => selection.toggle(id, event.target.checked))} />本页</label><span>已选 <strong>{selection.count(total)}</strong> 条{selection.all ? ' · 跨页全选' : ''}</span><button className="text-button" onClick={selection.selectAll}>全选当前筛选</button><button className="text-button" onClick={selection.clear}>清空选择</button><Button variant="danger" disabled={busy || !selection.count(total)} onClick={() => onRemove(selection.payload)}><Trash2 size={14} />删除所选</Button></div>
    <div className="history-cutoff"><label>清理截止时间（本机时区）<input type="datetime-local" aria-label="清理截止时间" value={before} onChange={event => setBefore(event.target.value)} /></label><Button disabled={busy || !before || !total} onClick={removeBefore}>清理此前记录</Button></div>
  </div>;
}
