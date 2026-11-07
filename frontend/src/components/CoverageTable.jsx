import React from 'react'
import { BarChart3, FileText, HardDrive, AlertCircle } from 'lucide-react'
import './CoverageTable.css'

export default function CoverageTable({ coverage }) {
  if (!coverage || Object.keys(coverage).length === 0) {
    return (
      <div className="alert alert-warning">
        <AlertCircle size={16} strokeWidth={2} style={{ flexShrink: 0 }} />
        No coverage data available yet.
      </div>
    )
  }

  const rows = Object.entries(coverage).map(([name, info]) => ({
    name,
    key: info.key,
    crawled: info.crawled,
    processed: info.processed,
    pdfs_on_disk: info.pdfs_on_disk,
    pct: info.crawled > 0 ? Math.round((info.processed / info.crawled) * 100) : 0,
  }))

  return (
    <div className="coverage-table-wrap">
      <div className="coverage-header">
        <h2 className="coverage-title">Database Coverage</h2>
        <p className="coverage-subtitle">
          Shows how much of each state portal has been crawled and had its text extracted.
          Only documents with extracted text are searchable.
        </p>
      </div>

      <div className="coverage-cards">
        {rows.map(row => (
          <div key={row.key} className="coverage-card card">
            <div className="coverage-card-header">
              <div className="coverage-state-name">{row.name}</div>
              <span className={`badge ${row.pct === 100 ? 'badge-green' : row.pct > 0 ? 'badge-orange' : 'badge-neutral'}`}>
                {row.pct}% extracted
              </span>
            </div>

            <div className="coverage-progress-bar">
              <div
                className="coverage-progress-fill"
                style={{ width: `${row.pct}%` }}
                aria-label={`${row.pct}% processed`}
              />
            </div>

            <div className="coverage-stats">
              <div className="cov-stat">
                <BarChart3 size={14} strokeWidth={1.5} color="var(--text-muted)" />
                <div>
                  <span className="cov-stat-num">{row.crawled}</span>
                  <span className="cov-stat-label">Crawled</span>
                </div>
              </div>
              <div className="cov-stat">
                <HardDrive size={14} strokeWidth={1.5} color="var(--text-muted)" />
                <div>
                  <span className="cov-stat-num">{row.pdfs_on_disk}</span>
                  <span className="cov-stat-label">Downloaded</span>
                </div>
              </div>
              <div className="cov-stat">
                <FileText size={14} strokeWidth={1.5} color="var(--brand-magenta)" />
                <div>
                  <span className="cov-stat-num" style={{ color: 'var(--brand-magenta)' }}>{row.processed}</span>
                  <span className="cov-stat-label">Searchable</span>
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="coverage-footer-note">
        Coverage is updated as new documents are processed. States and Union Territories
        with incomplete WCD portals will be added as their data becomes available.
      </div>
    </div>
  )
}
