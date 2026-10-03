"""Small public login document, independent of the private application bundle."""
from fastapi.responses import HTMLResponse, Response

STYLE = '''
:root{color-scheme:light dark;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;min-height:100dvh;display:grid;place-items:center;background:#f4f6f8;color:#182335;padding:24px}
main{width:min(100%,380px);padding:34px;border:1px solid #dde3eb;border-radius:20px;background:#fff;box-shadow:0 12px 40px #1823350a}
.lock{width:42px;height:42px;padding:10px;border-radius:12px;background:#edf4f3;color:#147d73}
h1{font-size:23px;margin:22px 0 8px}p{font-size:13px;color:#697586;line-height:1.7;margin:0 0 24px}
label{display:block;font-size:13px;margin:18px 0 7px}input,button{width:100%;font:inherit;border-radius:9px;padding:12px;border:1px solid #d7dfe8}
input{background:#fff;color:inherit}input:focus{outline:2px solid #147d7360;outline-offset:1px}button{margin-top:24px;background:#147d73;color:#fff;border:0;cursor:pointer}
button:disabled{opacity:.6;cursor:wait}#message{color:#b44337;margin:14px 0 0;min-height:20px}small{display:block;font-size:11px;color:#768394;margin-top:20px}
@media(prefers-color-scheme:dark){body{background:#10151c;color:#ecf0f5}main{background:#191f28;border-color:#303946}input{background:#121821;border-color:#3d495a}p,small{color:#a2afbf}}
'''
LOCK = '<svg class="lock" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="4" y="10" width="16" height="12" rx="3"/><path d="M8 10V6a4 4 0 0 1 8 0v4"/></svg>'


def login_page(secure=True):
    content = ('''<h1>管理员登录</h1><p>请登录后继续。</p>
<form id="login"><label for="username">管理员账号</label><input id="username" name="username" autocomplete="username" maxlength="64" required>
<label for="password">管理员密码</label><input id="password" name="password" type="password" autocomplete="current-password" maxlength="1024" required>
<button id="submit" type="submit">登录</button><p id="message" role="alert"></p></form><small>登录后方可访问。</small><script src="/login.js" defer></script>'''
               if secure else '<h1>需要安全连接</h1><p>请使用配置好的 HTTPS 地址访问，或通过本机 SSH 隧道连接。当前连接不接受账号、密码或密钥。</p>')
    return HTMLResponse('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                        '<meta name="robots" content="noindex,nofollow"><title>管理员登录</title><style>' + STYLE + '</style></head><body><main>'
                        + LOCK + content + '</main></body></html>', status_code=200 if secure else 426)


def login_script():
    return Response('''"use strict";
const form=document.getElementById("login"), button=document.getElementById("submit"), message=document.getElementById("message");
form.addEventListener("submit",async event=>{
 event.preventDefault();button.disabled=true;message.textContent="";
 try {
  if(location.protocol!=="https:" && !["127.0.0.1","localhost","[::1]"].includes(location.hostname)) throw Error("请使用 HTTPS 连接");
  const response=await fetch("/api/v1/auth/login",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({username:form.username.value,password:form.password.value})});
  form.password.value="";
  const result=await response.json();
  if(!response.ok) throw Error(typeof result.detail==="string"?result.detail:"登录未完成");
  location.replace("/");
 }catch(error){form.password.value="";message.textContent=error.message||"连接失败，请重试";button.disabled=false;}
});
addEventListener("pageshow",event=>{if(event.persisted) location.reload();});
''', media_type='text/javascript')
