import React from 'react'
import { CheckCircle2, AlertCircle, Database } from 'lucide-react'
import './HeroSection.css'

export default function HeroSection({ ollamaStatus, indexStatus }) {
  const ollamaOk = ollamaStatus?.available

  return (
    <section className="hero">
      <div className="container hero-inner">
        <div className="hero-text">
          <div className="hero-eyebrow">Child Rights and Policy Research Tool</div>
          <h1 className="hero-title">
            Search India's WCD<br />
            <span className="hero-highlight">Policy Documents</span>
          </h1>
          <p className="hero-desc">
            Acts, Circulars, Rules and SOPs from Indian state Women and Child
            Development portals, crawled, OCR-processed, and indexed for
            semantic search. Hindi is the source of truth; English translations
            are generated on demand.
          </p>

          <div className="hero-stats">
            <div className="hero-stat">
              <span className="stat-number">38</span>
              <span className="stat-label">States and UTs targeted</span>
            </div>
            <div className="hero-stat-sep" />
            <div className="hero-stat">
              <span className="stat-number">14</span>
              <span className="stat-label">Portals fully ready</span>
            </div>
            <div className="hero-stat-sep" />
            <div className="hero-stat">
              <span className="stat-number stat-dot">
                {indexStatus?.chunk_count
                  ? indexStatus.chunk_count.toLocaleString('en-IN')
                  : 'Building'}
              </span>
              <span className="stat-label">Indexed chunks</span>
            </div>
            <div className="hero-stat-sep" />
            <div className="hero-stat">
              <span className={`stat-number stat-dot ${ollamaOk ? 'green' : 'orange'}`}>
                {ollamaOk ? 'Live' : 'Offline'}
              </span>
              <span className="stat-label">AI translation</span>
            </div>
          </div>
        </div>

        <div className="hero-visual" aria-hidden="true">
          <div className="hero-doc-stack">
            <div className="hero-doc hero-doc-3">
              <div className="doc-line" />
              <div className="doc-line short" />
              <div className="doc-line" />
              <div className="doc-stamp">Notification</div>
            </div>
            <div className="hero-doc hero-doc-2">
              <div className="doc-line" />
              <div className="doc-line" />
              <div className="doc-line short" />
              <div className="doc-stamp">Circular</div>
            </div>
            <div className="hero-doc hero-doc-1">
              <div className="doc-line" />
              <div className="doc-line short" />
              <div className="doc-line" />
              <div className="doc-stamp">Order</div>
            </div>
          </div>
        </div>
      </div>

      <div className="hero-wave" aria-hidden="true">
        <svg viewBox="0 0 1440 60" preserveAspectRatio="none">
          <path d="M0,30 C360,60 1080,0 1440,30 L1440,60 L0,60 Z" fill="#FAF7F0" />
        </svg>
      </div>
    </section>
  )
}
