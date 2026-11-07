import React, { useRef, useEffect } from 'react'
import { Search, X, Brain, AlignLeft } from 'lucide-react'
import './SearchBar.css'

export default function SearchBar({ query, onQueryChange, mode, onModeChange, onSearch, loading }) {
  const inputRef = useRef(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  function handleKey(e) {
    if (e.key === 'Enter') onSearch()
  }

  return (
    <div className="search-section">
      <div className="container">
        {/* Mode tabs */}
        <div className="mode-tabs">
          <button
            id="tab-basic"
            className={`mode-tab ${mode === 'basic' ? 'active' : ''}`}
            onClick={() => onModeChange('basic')}
          >
            <AlignLeft size={20} strokeWidth={2} />
            <span>
              <strong>Basic Search</strong>
              <small>Title, filename, link text</small>
            </span>
          </button>
          <button
            id="tab-semantic"
            className={`mode-tab ${mode === 'semantic' ? 'active' : ''}`}
            onClick={() => onModeChange('semantic')}
          >
            <Brain size={20} strokeWidth={2} />
            <span>
              <strong>Semantic Search</strong>
              <small>Search by meaning across full documents</small>
            </span>
          </button>
        </div>

        {/* Search input */}
        <div className="search-input-row">
          <div className="search-input-wrap">
            <input
              id="search-input"
              ref={inputRef}
              className="input search-input"
              type="text"
              placeholder={
                mode === 'semantic'
                  ? 'Describe what you\'re looking for, e.g. child nutrition scheme eligibility'
                  : 'Search document titles, filenames or circular numbers'
              }
              value={query}
              onChange={e => onQueryChange(e.target.value)}
              onKeyDown={handleKey}
              aria-label="Search documents"
            />
            {query && (
              <button
                className="search-clear"
                onClick={() => onQueryChange('')}
                aria-label="Clear search"
              >
                <X size={14} />
              </button>
            )}
          </div>
          <button
            id="search-btn"
            className="btn btn-primary search-btn"
            onClick={onSearch}
            disabled={loading || !query.trim()}
          >
            {loading ? <span className="spinner" /> : <Search size={16} strokeWidth={2.5} />}
            Search
          </button>
        </div>
      </div>
    </div>
  )
}
