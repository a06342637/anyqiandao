// Mirrors app/proxy.py parse_proxy so a pasted line fills the editor fields exactly as the backend would read it.
export interface ParsedProxy { scheme: 'auto' | 'http' | 'socks5' | 'ssh'; host: string; port: number; username: string; password: string; }

export function parseProxyLine(input: string): ParsedProxy | null {
  const text = input.trim();
  if (!text) return null;
  if (text.includes('://')) {
    try {
      const url = new URL(text);
      const raw = url.protocol.replace(':', '').toLowerCase();
      const scheme = ({ s5: 'socks5', socks: 'socks5', socks5: 'socks5', http: 'http', ssh: 'ssh' } as Record<string, ParsedProxy['scheme']>)[raw];
      if (!scheme || !url.hostname || (url.pathname && url.pathname !== '/') || url.search || url.hash) return null;
      const port = url.port ? Number(url.port) : { http: 8080, socks5: 1080, ssh: 22, auto: 0 }[scheme];
      return { scheme, host: url.hostname.replace(/^\[|\]$/g, ''), port, username: decodeURIComponent(url.username || ''), password: decodeURIComponent(url.password || '') };
    } catch { return null; }
  }
  let fields: string[];
  if (/\s/.test(text)) fields = text.split(/\s+/, 4);
  else if (text.startsWith('[')) {
    const matched = /^\[([^\]]+)\]:(\d+)(?::([^:]*):(.*))?$/.exec(text);
    if (!matched) return null;
    fields = [matched[1], matched[2], matched[3] ?? '', matched[4] ?? ''].slice(0, matched[3] === undefined ? 2 : 4);
  } else {
    // host:port:user:pass — split only at the first three colons so a password containing ':' survives.
    const parts = text.split(':');
    fields = parts.length <= 4 ? parts : [parts[0], parts[1], parts[2], parts.slice(3).join(':')];
  }
  if (fields.length !== 2 && fields.length !== 4) return null;
  const port = Number(fields[1]);
  if (!fields[0] || !Number.isInteger(port) || port < 1 || port > 65535 || /[\s/@?#]/.test(fields[0])) return null;
  return { scheme: 'auto', host: fields[0], port, username: fields[2] ?? '', password: fields[3] ?? '' };
}
