/**
 * API client for the FastAPI backend.
 *
 * In development: Vite proxies /api/* to http://127.0.0.1:8000
 * In production:  Set VITE_API_BASE to your Oracle server URL,
 *                 e.g. https://your-oracle-ip.com  (no trailing slash)
 *
 * The frontend reads import.meta.env.VITE_API_BASE at build time.
 * If not set, it falls back to '' (relative path, works with Vite proxy in dev).
 */

const API_ORIGIN = import.meta.env.VITE_API_BASE || ''
const BASE = `${API_ORIGIN}/api`

async function request(method, path, body = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  }
  if (body !== null) opts.body = JSON.stringify(body)

  const res = await fetch(`${BASE}${path}`, opts)
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const json = await res.json()
      detail = json.detail || detail
    } catch {}
    throw new Error(detail)
  }
  return res.json()
}

export const api = {
  status:         ()        => request('GET', '/status'),
  ollamaStatus:   ()        => request('GET', '/ollama-status'),
  states:         ()        => request('GET', '/states'),
  categories:     ()        => request('GET', '/categories'),
  dateRange:      ()        => request('GET', '/date-range'),
  indexStatus:    ()        => request('GET', '/index-status'),

  // Browse all without search
  listDocuments: (params = {}) => {
    const qs = new URLSearchParams()
    if (params.state)     qs.set('state',     params.state)
    if (params.category)  qs.set('category',  params.category)
    if (params.date_from) qs.set('date_from', params.date_from)
    if (params.date_to)   qs.set('date_to',   params.date_to)
    if (params.page)      qs.set('page',      params.page)
    if (params.page_size) qs.set('page_size', params.page_size)
    const q = qs.toString()
    return request('GET', `/documents${q ? '?' + q : ''}`)
  },

  // Search
  basicSearch:    (body)    => request('POST', '/search/basic',    body),
  semanticSearch: (body)    => request('POST', '/search/semantic', body),

  // AI features
  translate:      (body)    => request('POST', '/translate',  body),
  summarise:      (body)    => request('POST', '/summarise',  body),

  // PDF download — points directly to the Oracle server in production
  pdfUrl: (stateKey, filename) =>
    `${BASE}/pdf/${encodeURIComponent(stateKey)}/${encodeURIComponent(filename)}`,
}
