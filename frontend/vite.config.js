import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dev/preview proxies mirror nginx (used by the local no-Docker harness):
//   /api, /graphql -> Django ; /otlp/* -> collector (or tools/mock_sink.py), prefix stripped
const API = process.env.VITE_PROXY_API || 'http://localhost:8000';
const OTLP = process.env.VITE_PROXY_OTLP || 'http://localhost:9999';
const proxy = {
  '/api': { target: API, changeOrigin: true },
  '/graphql': { target: API, changeOrigin: true },
  '/otlp': { target: OTLP, changeOrigin: true, rewrite: p => p.replace(/^\/otlp/, '') },
};

export default defineConfig({
  plugins: [react()],
  build: { sourcemap: true },
  server: { port: 5173, proxy },
  preview: { port: 4173, proxy },
});
