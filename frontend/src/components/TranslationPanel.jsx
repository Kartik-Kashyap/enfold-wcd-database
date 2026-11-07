import React, { useState, useEffect } from 'react'
import { Globe, AlertTriangle, CheckCircle, RefreshCw } from 'lucide-react'
import { api } from '../api/client'
import './TranslationPanel.css'

const _cache = new Map()

export default function TranslationPanel({ text, cardId, ollamaAvailable }) {
  const [state, setState] = useState('idle')
  const [result, setResult] = useState(null)

  const cacheKey = `${cardId}::${text?.slice(0, 80)}`

  useEffect(() => {
    if (_cache.has(cacheKey)) {
      const cached = _cache.get(cacheKey)
      setResult(cached)
      setState(cached.ok ? 'ok' : cached.status === 'unavailable' ? 'unavailable' : 'failed')
    }
  }, [cacheKey])

  async function doTranslate() {
    setState('loading')
    try {
      const res = await api.translate({ text })
      _cache.set(cacheKey, res)
      setResult(res)
      setState(res.ok ? 'ok' : (res.status === 'unavailable' ? 'unavailable' : 'failed'))
    } catch (err) {
      const res = { ok: false, reasons: [err.message], status: 'unavailable' }
      setResult(res)
      setState('unavailable')
    }
  }

  if (state === 'idle' && !result) {
    return (
      <div className="translation-panel">
        <button
          id={`translate-btn-${cardId}`}
          className="btn btn-secondary btn-sm"
          onClick={doTranslate}
          disabled={!ollamaAvailable}
        >
          <Globe size={14} strokeWidth={2} />
          Translate to English
        </button>
        {!ollamaAvailable && (
          <p className="translation-hint">
            <AlertTriangle size={13} strokeWidth={2} style={{ flexShrink: 0 }} />
            AI translation is currently unavailable. Contact your system administrator.
          </p>
        )}
        {ollamaAvailable && (
          <p className="translation-hint">
            Translation runs locally and privately. Nothing is sent to any external service.
          </p>
        )}
      </div>
    )
  }

  if (state === 'loading') {
    return (
      <div className="translation-panel loading">
        <span className="spinner" />
        <span>Translating locally, this may take a moment...</span>
      </div>
    )
  }

  if (state === 'ok' && result?.english) {
    return (
      <div className="translation-panel">
        <div className="alert alert-success translation-result">
          {result.english}
        </div>
        <p className="translation-caption">
          Machine translation{result.cached ? ', retrieved from cache' : ''}. Not human-verified.
          Always check the Hindi original before relying on this.
        </p>
      </div>
    )
  }

  if (state === 'failed' && result) {
    return (
      <div className="translation-panel">
        <div className="alert alert-error">
          <AlertTriangle size={16} strokeWidth={2} style={{ flexShrink: 0 }} />
          <div>
            <strong>Translation could not be verified and was not shown.</strong>
            <br />
            <small>Please read the Hindi original directly.</small>
          </div>
        </div>
        <button className="btn btn-ghost btn-sm" style={{ marginTop: 8 }} onClick={doTranslate}>
          <RefreshCw size={13} strokeWidth={2} />
          Try again
        </button>
      </div>
    )
  }

  return (
    <div className="translation-panel">
      <div className="alert alert-warning">
        <AlertTriangle size={16} strokeWidth={2} style={{ flexShrink: 0 }} />
        <span>Translation currently unavailable. Please try again later.</span>
      </div>
    </div>
  )
}
