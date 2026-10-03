import { useState, type FormEvent } from 'react';
import { RotateCcw, Save } from 'lucide-react';
import { api, refreshData } from './api';
import { Button, DEFAULT_BRANDING, SealMark, useToast } from './ui';
import type { SiteBranding } from './types';

export default function AppearancePanel({ initial }: { initial: SiteBranding }) {
  const [values, setValues] = useState<SiteBranding>({ site_name: initial.site_name, site_icon_text: initial.site_icon_text });
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const save = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      const result = await api<SiteBranding>('/settings/branding', 'PUT', values);
      setValues(result);
      refreshData();
      toast('网站名称与图标已保存，左上角、登录页和浏览器标签同步更新', 'success');
    } catch (error) { toast((error as Error).message, 'error'); }
    finally { setBusy(false); }
  };
  return <form onSubmit={save}>
    <div className="settings-section">
      <h3>网站名称与图标</h3>
      <div className="appearance-preview"><SealMark size="large" text={values.site_icon_text.trim() || DEFAULT_BRANDING.site_icon_text} /><div><strong>{values.site_name.trim() || DEFAULT_BRANDING.site_name}</strong><p>工作空间外观预览 · v{__APP_VERSION__}</p></div></div>
      <label className="field">网站名称<input value={values.site_name} onChange={event => setValues(previous => ({ ...previous, site_name: event.target.value }))} maxLength={40} required disabled={busy} placeholder="例如：我的签到工作空间" /></label>
      <label className="field">图标文字<input value={values.site_icon_text} onChange={event => setValues(previous => ({ ...previous, site_icon_text: event.target.value }))} maxLength={2} required disabled={busy} placeholder="签" /></label>
      <p className="form-hint">图标支持 1–2 个文字，浏览器标签与左上角使用同一标识。名称同步用于登录页、标签标题与页脚，不会修改管理员登录账号。</p>
      <div className="notice"><p>全站正文采用 Apple 系统字体栈：Mac / iPhone 优先使用 SF Pro 和苹方，Windows 自动使用本机可用的对应字体，无需加载第三方字体。外观设置随账号备份一起保存和恢复。</p></div>
    </div>
    <div className="settings-save"><Button disabled={busy} onClick={() => { setValues(DEFAULT_BRANDING); toast('已填入默认名称与图标，保存后生效'); }}><RotateCcw size={15} />恢复默认</Button><Button type="submit" variant="primary" busy={busy}><Save size={15} />保存外观</Button></div>
  </form>;
}
