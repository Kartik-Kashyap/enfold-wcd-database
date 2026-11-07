import React, { useState } from 'react'
import { Sparkles, AlertTriangle, RefreshCw } from 'lucide-react'
import { api } from '../api/client'

export default function SummaryPanel({ text, state, category, cardId }) {
  const [status, setStatus] = useState('idle')
  const [summary, setSummary] = useState('')
  const [error, setError] = useState('')

  async function doSummarise() {
    setStatus('loading')
    try {
      const res = await api.summarise({ text, state, category })
      setSummary(res.summary)
      setStatus('done')
    } catch (err) {
      setError(err.message)
      setStatus('error')
    }
  }

  if (status === 'idle') {
    return (
      <div style={{ marginTop: 12 }}>
        <button
          id={`summarise-btn-${cardId}`}
          className="btn btn-ghost btn-sm"
          onClick={doSummarise}
        >
          <Sparkles size={14} strokeWidth={2} />
          Generate AI Summary
        </button>
      </div>
    )
  }

  if (status === 'loading') {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 12, fontSize: 14, color: 'var(--text-secondary)' }}>
        <span className="spinner" />
        Generating summary, please wait...
      </div>
    )
  }

  if (status === 'error') {
    return (
      <div className="alert alert-error" style={{ marginTop: 12 }}>
        <AlertTriangle size={16} strokeWidth={2} style={{ flexShrink: 0 }} />
        <div>
          <strong>Could not generate summary.</strong>
          <br />
          <small>{error}</small>
        </div>
      </div>
    )
  }

  return (
    <div style={{ marginTop: 12 }}>
      <div className="alert alert-success" style={{ flexDirection: 'column', gap: 6, alignItems: 'flex-start' }}>
        <strong style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Sparkles size={14} strokeWidth={2} />
          AI Summary
        </strong>
        <div style={{ fontSize: 14, lineHeight: 1.7, whiteSpace: 'pre-wrap' }}>{summary}</div>
      </div>
      <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 6 }}>
        AI-generated summary. Not verified. Confirm against the source PDF before use.
      </p>
    </div>
  )
}
