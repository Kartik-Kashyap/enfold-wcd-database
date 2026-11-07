import React, { useState } from 'react'
import { FileText, ChevronDown, Download, Sparkles, Calendar, Cpu, MapPin, Tag } from 'lucide-react'
import TranslationPanel from './TranslationPanel'
import SummaryPanel from './SummaryPanel'
import './ResultCard.css'
import { api } from '../api/client'

export default function ResultCard({ doc, index, ollamaAvailable }) {
  const [expanded, setExpanded] = useState(index < 3)
  const [showTranslation, setShowTranslation] = useState(false)
  const [showSummary, setShowSummary] = useState(false)

  const title = doc.title || doc.link_text || 'Untitled Document'
  const pdfUrl = doc.state_key && doc.filename
    ? api.pdfUrl(doc.state_key, doc.filename)
    : null
  const hasText = Boolean(doc.hindi_excerpt?.trim())

  return (
    <div className={`result-card card ${expanded ? 'expanded' : ''}`}>
      {/* Header */}
      <button
        className="result-card-header"
        onClick={() => setExpanded(e => !e)}
        aria-expanded={expanded}
      >
        <div className="result-card-header-left">
          <FileText size={20} strokeWidth={1.5} className="result-icon" />
          <div className="result-title-group">
            <h3 className="result-title">{title}</h3>
            <div className="result-badges">
              {doc.state && (
                <span className="badge badge-magenta">
                  <MapPin size={10} strokeWidth={2} />
                  {doc.state}
                </span>
              )}
              {doc.category && (
                <span className="badge badge-purple">
                  <Tag size={10} strokeWidth={2} />
                  {doc.category}
                </span>
              )}
              {doc.document_date && (
                <span className="badge badge-neutral">
                  <Calendar size={10} strokeWidth={2} />
                  {doc.document_date}
                </span>
              )}
              {doc.was_ocr_used && (
                <span className="badge badge-orange" title="Text extracted via OCR">
                  <Cpu size={10} strokeWidth={2} />
                  OCR
                </span>
              )}
              {doc.similarity !== undefined && doc.similarity !== null && (
                <span className="badge badge-green">
                  {Math.round(doc.similarity * 100)}% match
                </span>
              )}
            </div>
          </div>
        </div>
        <ChevronDown
          size={16}
          strokeWidth={2}
          className={`result-chevron ${expanded ? 'open' : ''}`}
        />
      </button>

      {/* Expanded content */}
      {expanded && (
        <div className="result-card-body">
          {doc.filename && (
            <p className="result-meta">
              <span className="result-meta-label">File:</span>
              <code>{doc.filename}</code>
            </p>
          )}
          {doc.link_text && doc.link_text !== title && (
            <p className="result-meta">
              <span className="result-meta-label">Link text:</span>
              {doc.link_text}
            </p>
          )}

          <hr className="divider" />

          {/* Hindi text tabs */}
          {hasText ? (
            <>
              <div className="result-tabs">
                <button
                  className={`result-tab ${!showTranslation ? 'active' : ''}`}
                  onClick={() => setShowTranslation(false)}
                >
                  Original Hindi
                </button>
                <button
                  className={`result-tab ${showTranslation ? 'active' : ''}`}
                  onClick={() => setShowTranslation(true)}
                >
                  English Translation
                </button>
              </div>

              {!showTranslation ? (
                <div className="hindi-text">{doc.hindi_excerpt}</div>
              ) : (
                <TranslationPanel
                  text={doc.hindi_excerpt}
                  cardId={`card-${index}`}
                  ollamaAvailable={ollamaAvailable}
                />
              )}
            </>
          ) : (
            <p className="result-no-text">No text has been extracted from this document yet.</p>
          )}

          {/* Action row */}
          <div className="result-actions">
            {pdfUrl ? (
              <a
                href={pdfUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="btn btn-secondary btn-sm"
                download={doc.filename}
              >
                <Download size={14} strokeWidth={2} />
                Download PDF
              </a>
            ) : (
              <span className="result-no-pdf">PDF not yet downloaded for this document.</span>
            )}

            {hasText && ollamaAvailable && (
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => setShowSummary(s => !s)}
              >
                <Sparkles size={14} strokeWidth={2} />
                {showSummary ? 'Hide Summary' : 'AI Summary'}
              </button>
            )}
          </div>

          {showSummary && hasText && (
            <SummaryPanel
              text={doc.hindi_excerpt}
              state={doc.state}
              category={doc.category}
              cardId={`card-${index}`}
            />
          )}
        </div>
      )}
    </div>
  )
}
