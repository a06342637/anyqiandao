import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { readFileSync } from 'node:fs';

export default defineConfig({
  plugins: [react()],
  define: { __APP_VERSION__: JSON.stringify(readFileSync(new URL('../VERSION', import.meta.url), 'utf8').trim()) },
  server: { proxy: { '/login': 'http://127.0.0.1:8000', '/api': 'http://127.0.0.1:8000', '/favicon.svg': 'http://127.0.0.1:8000' } },
});
