import React, { useState, useEffect, useCallback } from 'react'
import { Search, Info, List, ChevronLeft, ChevronRight } from 'lucide-react'
import { api } from './api/client'

import Navbar from './components/Navbar'
import HeroSection from './components/HeroSection'
import SearchBar from './components/SearchBar'
import FilterSidebar from './components/FilterSidebar'
import ResultCard from './components/ResultCard'
import CoverageTable from './components/CoverageTable'
import StatusBadge from './components/StatusBadge'

import './App.css'

export default function App() {
  const [activeTab, setActiveTab] = useState('search')

  // Metadata loaded on mount
  const [states, setStates] = useState([])
  const [categories, setCategories] = useState([])
  const [dateRange, setDateRange] = useState(null)
  const [ollamaStatus, setOllamaStatus] = useState(null)
  const [indexStatus, setIndexStatus] = useState(null)
  const [coverage, setCoverage] = useState({})

  // Search state
  const [query, setQuery] = useState('')
  const [mode, setMode] = useState('basic')
  const [filters, setFilters] = useState({ state: null, category: null, date_from: null, date_to: null })
  const [scanBody, setScanBody] = useState(false)

  // Result state — three modes: 'idle' | 'search' | 'browse'
  const [viewMode, setViewMode] = useState('idle')
  const [results, setResults] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  // Browse-all pagination
  const [browseTotal, setBrowseTotal] = useState(0)
  const [browsePage, setBrowsePage] = useState(1)
  const [browseTotalPages, setBrowseTotalPages] = useState(1)
  const PAGE_SIZE = 50

  useEffect(() => {
    Promise.all([
      api.states().then(d => setStates(d.states)).catch(() => {}),
      api.categories().then(d => setCategories(d.categories)).catch(() => {}),
      api.dateRange().then(setDateRange).catch(() => {}),
      api.ollamaStatus().then(setOllamaStatus).catch(() => {}),
      api.indexStatus().then(setIndexStatus).catch(() => {}),
      api.status().then(d => setCoverage(d.coverage)).catch(() => {}),
    ])
  }, [])

  // ── Search ──────────────────────────────────────────────────────────────────
  const handleSearch = useCallback(async () => {
    const q = query.trim()
    if (!q) return

    setLoading(true)
    setError(null)
    setViewMode('search')

    try {
      let data
      if (mode === 'basic') {
        data = await api.basicSearch({
          query: q,
          state: filters.state,
          category: filters.category,
          date_from: filters.date_from,
          date_to: filters.date_to,
          scan_body: scanBody,
        })
      } else {
        data = await api.semanticSearch({
          query: q,
          state: filters.state,
          category: filters.category,
          date_from: filters.date_from,
          date_to: filters.date_to,
        })
      }
      setResults(data.results || [])
    } catch (err) {
      setError(err.message)
      setResults([])
    } finally {
      setLoading(false)
    }
  }, [query, mode, filters, scanBody])

  // ── Browse all ──────────────────────────────────────────────────────────────
  const handleBrowse = useCallback(async (page = 1, currentFilters = filters) => {
    setLoading(true)
    setError(null)
    setViewMode('browse')
    setBrowsePage(page)

    try {
      const data = await api.listDocuments({
        state: currentFilters.state,
        category: currentFilters.category,
        date_from: currentFilters.date_from,
        date_to: currentFilters.date_to,
        page,
        page_size: PAGE_SIZE,
      })
      setResults(data.results || [])
      setBrowseTotal(data.total || 0)
      setBrowseTotalPages(data.total_pages || 1)
    } catch (err) {
      setError(err.message)
      setResults([])
    } finally {
      setLoading(false)
    }
  }, [filters])

  // Re-run browse when filters change while in browse mode
  useEffect(() => {
    if (viewMode === 'browse') {
      handleBrowse(1, filters)
    }
  }, [filters]) // eslint-disable-line react-hooks/exhaustive-deps

  function handleModeChange(m) {
    setMode(m)
    setViewMode('idle')
    setResults([])
    setError(null)
  }

  function handleQueryChange(q) {
    setQuery(q)
    if (!q && viewMode === 'search') {
      setViewMode('idle')
      setResults([])
    }
  }

  // ── Render ──────────────────────────────────────────────────────────────────
  const totalDocs = Object.values(coverage).reduce((s, v) => s + (v.processed || 0), 0)

  return (
    <div className="app">
      <Navbar activeTab={activeTab} onTabChange={setActiveTab} />

      {activeTab === 'search' && (
        <>
          <HeroSection ollamaStatus={ollamaStatus} indexStatus={indexStatus} />

          <SearchBar
            query={query}
            onQueryChange={handleQueryChange}
            mode={mode}
            onModeChange={handleModeChange}
            onSearch={handleSearch}
            loading={loading && viewMode === 'search'}
          />

          <div className="container search-layout">
            <FilterSidebar
              states={states}
              categories={categories}
              dateRange={dateRange}
              filters={filters}
              onFilterChange={f => setFilters(f)}
              scanBody={scanBody}
              onScanBodyChange={setScanBody}
              mode={mode}
            />

            <main className="results-area">
              <div className="status-row">
                <StatusBadge ollamaStatus={ollamaStatus} indexStatus={indexStatus} />
              </div>

              {/* Idle state: welcome notice + Browse All button */}
              {viewMode === 'idle' && (
                <div className="idle-state">
                  <div className="alert alert-info welcome-notice">
                    <Info size={18} strokeWidth={2} style={{ flexShrink: 0 }} />
                    <div>
                      <strong>Search runs on the original Hindi text.</strong>{' '}
                      English translations are generated only when you request them, per document,
                      and are quality-checked before being displayed.
                      Always verify against the source PDF for official use.
                    </div>
                  </div>

                  {totalDocs > 0 && (
                    <div className="browse-all-cta">
                      <p className="browse-all-desc">
                        {totalDocs.toLocaleString('en-IN')} documents available across{' '}
                        {Object.keys(coverage).length} states. No search query needed.
                      </p>
                      <button
                        id="browse-all-btn"
                        className="btn btn-secondary"
                        onClick={() => handleBrowse(1)}
                        disabled={loading}
                      >
                        <List size={16} strokeWidth={2} />
                        Browse All Documents
                      </button>
                    </div>
                  )}
                </div>
              )}

              {/* Error */}
              {error && (
                <div className="alert alert-error">
                  <Search size={16} strokeWidth={2} style={{ flexShrink: 0 }} />
                  {error}
                </div>
              )}

              {/* Loading */}
              {loading && (
                <div className="results-loading">
                  <span className="spinner" style={{ width: 24, height: 24 }} />
                  <span>
                    {viewMode === 'browse' ? 'Loading documents...' : 'Searching...'}
                  </span>
                </div>
              )}

              {/* Results header */}
              {!loading && (viewMode === 'search' || viewMode === 'browse') && (
                <>
                  <div className="results-header">
                    <h2 className="results-count">
                      {viewMode === 'browse'
                        ? browseTotal === 0
                          ? 'No documents found'
                          : `${browseTotal.toLocaleString('en-IN')} document${browseTotal !== 1 ? 's' : ''}`
                        : results.length === 0
                          ? 'No results found'
                          : `${results.length} document${results.length !== 1 ? 's' : ''} found`
                      }
                    </h2>
                    {viewMode === 'browse' && browseTotalPages > 1 && (
                      <span className="results-meta">
                        Page {browsePage} of {browseTotalPages}
                      </span>
                    )}
                    {viewMode === 'search' && mode === 'semantic' && indexStatus?.chunk_count > 0 && (
                      <span className="results-meta">
                        across {indexStatus.chunk_count?.toLocaleString('en-IN')} indexed chunks
                      </span>
                    )}
                  </div>

                  {/* Empty state */}
                  {results.length === 0 && !error && (
                    <div className="empty-state">
                      <div className="empty-icon">
                        <Search size={40} strokeWidth={1.5} color="var(--text-muted)" />
                      </div>
                      {viewMode === 'search' ? (
                        <>
                          <p>No documents matched <strong>"{query}"</strong> with the current filters.</p>
                          <p className="empty-hint">Try broader terms, different filters, or switch to Semantic Search.</p>
                        </>
                      ) : (
                        <>
                          <p>No documents match the selected filters.</p>
                          <p className="empty-hint">Try clearing some filters to see more results.</p>
                        </>
                      )}
                    </div>
                  )}

                  {/* Result cards */}
                  <div className="results-list">
                    {results.map((doc, i) => (
                      <ResultCard
                        key={doc.filename || doc.id || i}
                        doc={doc}
                        index={i}
                        ollamaAvailable={ollamaStatus?.available}
                      />
                    ))}
                  </div>

                  {/* Pagination (browse mode only) */}
                  {viewMode === 'browse' && browseTotalPages > 1 && (
                    <div className="pagination">
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => handleBrowse(browsePage - 1)}
                        disabled={browsePage <= 1 || loading}
                      >
                        <ChevronLeft size={16} strokeWidth={2} />
                        Previous
                      </button>
                      <span className="pagination-info">
                        Page {browsePage} of {browseTotalPages}
                      </span>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => handleBrowse(browsePage + 1)}
                        disabled={browsePage >= browseTotalPages || loading}
                      >
                        Next
                        <ChevronRight size={16} strokeWidth={2} />
                      </button>
                    </div>
                  )}
                </>
              )}
            </main>
          </div>
        </>
      )}

      {activeTab === 'coverage' && (
        <div className="container" style={{ paddingTop: 40, paddingBottom: 80 }}>
          <CoverageTable coverage={coverage} />
        </div>
      )}

      <footer className="app-footer">
        <div className="container footer-inner">
          <div className="footer-brand">
            <strong>Enfold India</strong> Child Rights and Policy Document Database
          </div>
          <div className="footer-note">
            A research tool for policy advocates. Hindi is the source of truth.
            All translations are machine-generated and unverified.
          </div>
        </div>
      </footer>
    </div>
  )
}
