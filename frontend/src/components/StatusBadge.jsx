import React from 'react'
import { CheckCircle2, AlertCircle, Database } from 'lucide-react'

export default function StatusBadge({ ollamaStatus, indexStatus }) {
  const ollamaOk = ollamaStatus?.available
  const indexOk = indexStatus?.available && indexStatus?.chunk_count > 0

  return (
    <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
      <span
        className={`badge ${ollamaOk ? 'badge-green' : 'badge-orange'}`}
        title={ollamaStatus?.message || ''}
      >
        {ollamaOk
          ? <CheckCircle2 size={11} strokeWidth={2.5} />
          : <AlertCircle size={11} strokeWidth={2.5} />}
        AI Translation {ollamaOk ? 'Ready' : 'Unavailable'}
        {ollamaOk && ollamaStatus?.cached_excerpts > 0 && ` (${ollamaStatus.cached_excerpts} cached)`}
      </span>

      <span
        className={`badge ${indexOk ? 'badge-green' : 'badge-orange'}`}
        title={indexOk ? `${indexStatus.chunk_count} chunks indexed` : 'Index not built'}
      >
        {indexOk
          ? <CheckCircle2 size={11} strokeWidth={2.5} />
          : <AlertCircle size={11} strokeWidth={2.5} />}
        <Database size={11} strokeWidth={2} />
        {indexOk
          ? `${indexStatus.chunk_count?.toLocaleString('en-IN')} chunks indexed`
          : 'Search index not available'}
      </span>
    </div>
  )
}
