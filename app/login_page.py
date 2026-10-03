"""Public login presentation, independent of private data and application assets."""
from fastapi.responses import HTMLResponse, Response

STYLE = '''
:root{color-scheme:light;--paper:#f5f4ef;--panel:#fffefb;--line:#e4e2da;--edge:#d2d0c7;--ink:#1e2330;--muted:#7c8290;--secondary:#4b5161;--accent:#3a4a78;--soft:#e9ecf5;--seal:#c4432e;--on-accent:#fff;--shadow:0 12px 38px #1e233010;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
:root[data-theme=dark]{color-scheme:dark;--paper:#17191d;--panel:#1f2227;--line:#30343c;--edge:#3d424b;--ink:#ebeae4;--muted:#969caa;--secondary:#c3c6ce;--accent:#a7b6e6;--soft:#2a3147;--seal:#e2664e;--on-accent:#15181f;--shadow:0 12px 38px #0003}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;--paper:#17191d;--panel:#1f2227;--line:#30343c;--edge:#3d424b;--ink:#ebeae4;--muted:#969caa;--secondary:#c3c6ce;--accent:#a7b6e6;--soft:#2a3147;--seal:#e2664e;--on-accent:#15181f;--shadow:0 12px 38px #0003}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-size:14px;line-height:1.6}button,input{font:inherit}button{cursor:pointer}button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:3px}svg{flex-shrink:0;vertical-align:middle}h1,h2,p{margin:0}
.layout{min-height:100dvh;max-width:1240px;margin:auto;padding:0 56px;display:flex;flex-direction:column}.header{height:96px;display:flex;align-items:center;justify-content:space-between;gap:16px}.brand{display:flex;align-items:center;gap:12px;font-size:16px;font-weight:650;letter-spacing:-.3px}.seal{display:grid;place-items:center;width:35px;height:35px;background:var(--seal);color:#fff;border:1px solid #ffffff40;border-radius:9px;box-shadow:inset 0 0 0 3px #ffffff17;font-family:"Songti SC",SimSun,serif;font-size:22px;transform:rotate(-5deg)}
.theme-toggle{display:grid;place-items:center;width:38px;height:38px;border:1px solid transparent;border-radius:10px;background:transparent;color:var(--muted)}.theme-toggle:hover{background:var(--panel);border-color:var(--line);color:var(--ink)}.sun{display:none}:root[data-theme=dark] .sun{display:block}:root[data-theme=dark] .moon{display:none}
.main{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(320px,420px);gap:84px;align-items:center;flex:1;padding:44px 0 72px}.intro{min-width:0}.intro h1{font-size:44px;line-height:1.35;font-weight:650;letter-spacing:-1.4px}.intro h1 span{color:var(--accent)}.intro>p{color:var(--secondary);font-size:14px;line-height:1.95;max-width:440px;margin-top:23px}.steps{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-top:28px;color:var(--muted);font-size:12px}.steps span{display:flex;gap:7px;align-items:center}.steps span svg{color:var(--accent)}.step-arrow{opacity:.5}
.week{display:flex;gap:9px;margin-bottom:40px}.day{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:7px;width:54px;height:69px;background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:0 2px 3px #00000003}.day small{font-size:10px;color:var(--muted)}.stamp{display:grid;place-items:center;width:27px;height:27px;border:1px dotted var(--edge);border-radius:6px;color:transparent;font:600 16px "Songti SC",SimSun,serif}.day.stamped .stamp{color:var(--seal);border:1.5px solid var(--seal);transform:rotate(-8deg)}.day.today{border-color:var(--accent);box-shadow:0 0 0 3px var(--soft)}.day.today small{color:var(--accent);font-weight:600}.day.today .stamp{border-color:var(--accent);border-style:dashed}
.card{padding:34px 32px 30px;background:var(--panel);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow)}.login-symbol{display:grid;place-items:center;width:46px;height:46px;color:var(--accent);background:var(--soft);border-radius:12px;margin-bottom:22px}.card h2{font-size:25px;letter-spacing:-.6px;font-weight:650}.card>p{font-size:12px;line-height:1.85;color:var(--muted);margin:8px 0 25px}.field{display:block;font-size:12px;font-weight:550;margin:18px 0 8px}input{width:100%;padding:11px 13px;min-width:0;border:1px solid var(--edge);border-radius:9px;background:var(--panel);color:var(--ink);font-size:14px;transition:border-color .15s,box-shadow .15s}input::placeholder{color:var(--muted);opacity:.8}input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--soft)}.submit{display:flex;justify-content:space-between;align-items:center;gap:12px;width:100%;margin-top:24px;padding:11px 15px;min-height:44px;border:0;border-radius:9px;color:var(--on-accent);background:var(--accent);font-size:14px;font-weight:550;transition:filter .15s}.submit:hover{filter:brightness(1.08)}.submit:disabled{opacity:.65;cursor:wait}.message{font-size:12px;line-height:1.7;color:var(--seal);overflow-wrap:anywhere}.message:not(:empty){margin-top:14px}.card-note{display:flex;align-items:flex-start;gap:8px;border-top:1px solid var(--line);padding-top:18px;margin-top:22px;font-size:11px;line-height:1.8;color:var(--muted)}.card-note svg{margin-top:2px}.footer{display:flex;justify-content:space-between;gap:20px;align-items:center;border-top:1px solid var(--line);padding:21px 0 26px;color:var(--muted);font-size:11px}.footer span{display:flex;align-items:center;gap:7px}
@media(max-width:1000px){.layout{padding:0 40px}.main{gap:42px}.intro h1{font-size:38px}.day{width:45px;height:63px}.week{gap:7px}.steps{gap:10px}}
@media(max-width:760px){.layout{padding:0 28px}.main{grid-template-columns:minmax(0,1fr);gap:35px;max-width:480px;width:100%;margin:auto;padding:26px 0 48px}.intro h1{font-size:36px}.week{margin-bottom:25px}.day{flex:1;width:auto}.intro>p{margin-top:15px}.steps{margin-top:18px}.card{padding:28px}.header{height:82px}.footer{max-width:480px;width:100%;margin:auto;flex-wrap:wrap;gap:9px}}
@media(max-width:420px){.layout{padding:0 20px}.header{height:75px}.brand{font-size:15px}.intro h1{font-size:32px;letter-spacing:-.8px}.intro h1 br{display:none}.intro h1 span{margin-left:5px}.intro>p{font-size:13px}.day{height:59px}.week{gap:6px}.stamp{width:24px;height:24px;font-size:15px}.main{padding-top:15px;gap:27px}.card{padding:25px 22px}input{font-size:16px}.steps{gap:9px;font-size:11px}.steps svg{width:14px}.footer{font-size:10px}.card-note{font-size:11px}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
'''


def icon(paths, size=18, css=''):
    return f'<svg class="{css}" width="{size}" height="{size}" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{paths}</svg>'


KEY = icon('<circle cx="8" cy="8" r="5"/><path d="m12 12 8 8m-2-2 3-3m-6 0 3-3"/>')
SHIELD = icon('<path d="M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6z"/><path d="m8 12 3 3 5-6"/>', 15)
CALENDAR = icon('<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 11h18m-13 5 3 3 5-5"/>')
ARROW = icon('<path d="M5 12h14m-5-5 5 5-5 5"/>', 18)
CHEVRON = icon('<path d="m9 6 6 6-6 6"/>', 13, 'step-arrow')
MOON = icon('<path d="M20 13A8 8 0 0 1 11 4 8.5 8.5 0 1 0 20 13Z"/>', 19, 'moon')
SUN = icon('<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>', 19, 'sun')


def login_page(secure=True):
    week = ''.join(f'<span class="day" data-day="{index}"><span class="stamp">签</span><small>周{day}</small></span>' for index, day in enumerate('一二三四五六日'))
    content = ('''<h2 id="login-title">登录</h2><p>使用管理员账号和密码，进入你的签到工作空间。</p>
<form id="login" aria-label="管理员登录"><label class="field" for="username">管理员账号</label><input id="username" name="username" autocomplete="username" placeholder="输入管理员账号" maxlength="64" required>
<label class="field" for="password">管理员密码</label><input id="password" name="password" type="password" autocomplete="current-password" placeholder="输入管理员密码" maxlength="1024" required>
<button class="submit" id="submit" type="submit"><span id="submit-label">登录</span>''' + ARROW + '''</button><p class="message" id="message" role="alert"></p></form>
<div class="card-note">''' + SHIELD + '<span>登录后方可查看账号与数据。<br>连续输错 10 次后，请等待 15 分钟再试。</span></div>'
               if secure else '<h2 id="login-title">需要安全连接</h2><p>请使用配置好的 HTTPS 地址访问，或通过本机 SSH 隧道连接。当前连接不接受账号、密码或密钥。</p>')
    return HTMLResponse('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                        '<meta name="robots" content="noindex,nofollow"><title>管理员登录</title><style>' + STYLE + '</style><script src="/login.js" defer></script></head>'
                        '<body><div class="layout"><header class="header"><div class="brand"><span class="seal">签</span><span>any签到助手</span></div>'
                        '<button class="theme-toggle" id="theme-toggle" type="button" aria-label="切换深色主题" title="明暗切换">' + MOON + SUN + '</button></header>'
                        '<main class="main"><section class="intro"><div class="week" aria-hidden="true">' + week + '</div><h1>提取一次，<br><span>天天签到。</span></h1>'
                        '<p>提取登录凭证，检测账号状态，按计划自动签到。<br>把每天的重复操作，交给自己的签到工作空间。</p>'
                        '<div class="steps"><span>' + KEY + '提取凭证</span>' + CHEVRON + '<span>' + SHIELD + '检测有效</span>' + CHEVRON + '<span>' + CALENDAR + '定时签到</span></div></section>'
                        '<section class="card" aria-labelledby="login-title"><div class="login-symbol">' + KEY + '</div>' + content + '</section></main>'
                        '<footer class="footer"><span>any签到助手 · 让日常更简单</span><span>' + SHIELD + '私有部署 · 数据由你掌握</span></footer></div></body></html>', status_code=200 if secure else 426)


def login_script():
    return Response('''"use strict";
const root=document.documentElement, toggle=document.getElementById("theme-toggle"), media=matchMedia("(prefers-color-scheme: dark)");
function savedTheme(){try{return localStorage.getItem("any-theme");}catch{return null;}}
function theme(value){root.dataset.theme=value;toggle.setAttribute("aria-label",value==="dark"?"切换浅色主题":"切换深色主题");}
const saved=savedTheme();theme(saved==="dark"||saved==="light"?saved:media.matches?"dark":"light");
toggle.addEventListener("click",()=>{const value=root.dataset.theme==="dark"?"light":"dark";theme(value);try{localStorage.setItem("any-theme",value);}catch{}});
media.addEventListener("change",()=>{if(!["light","dark"].includes(savedTheme()))theme(media.matches?"dark":"light");});
// Calendar decoration reflects the local weekday, never account activity.
const today=(new Date().getDay()+6)%7;
document.querySelectorAll(".day").forEach((day,index)=>{day.classList.toggle("stamped",index<today);day.classList.toggle("today",index===today);});
const form=document.getElementById("login"), button=document.getElementById("submit"), label=document.getElementById("submit-label"), message=document.getElementById("message");
if(form)form.addEventListener("submit",async event=>{
 event.preventDefault();button.disabled=true;button.setAttribute("aria-busy","true");label.textContent="正在登录…";message.textContent="";
 try {
  if(location.protocol!=="https:" && !["127.0.0.1","localhost","[::1]"].includes(location.hostname)) throw Error("请使用 HTTPS 连接");
  const response=await fetch("/api/v1/auth/login",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({username:form.username.value,password:form.password.value})});
  form.password.value="";
  const result=await response.json();
  if(!response.ok) throw Error(typeof result.detail==="string"?result.detail:"登录未完成");
  location.replace("/");
 }catch(error){form.password.value="";message.textContent=error.message||"连接失败，请重试";button.disabled=false;button.removeAttribute("aria-busy");label.textContent="登录";}
});
addEventListener("pageshow",event=>{if(event.persisted) location.reload();});
''', media_type='text/javascript')
