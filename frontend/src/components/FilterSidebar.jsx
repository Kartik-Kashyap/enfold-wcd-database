import React, { useState } from 'react'
import { SlidersHorizontal, MapPin, Tag, Calendar, RotateCcw, FileSearch } from 'lucide-react'
import './FilterSidebar.css'

export default function FilterSidebar({
  states, categories, dateRange,
  filters, onFilterChange,
  scanBody, onScanBodyChange,
  mode,
}) {
  const [dateEnabled, setDateEnabled] = useState(false)

  function set(key, val) {
    onFilterChange({ ...filters, [key]: val })
  }

  function toggleDate(e) {
    setDateEnabled(e.target.checked)
    if (!e.target.checked) {
      onFilterChange({ ...filters, date_from: null, date_to: null })
    } else if (dateRange?.min) {
      onFilterChange({ ...filters, date_from: dateRange.min, date_to: dateRange.max })
    }
  }

  return (
    <aside className="filter-sidebar">
      <div className="filter-sidebar-header">
        <SlidersHorizontal size={16} strokeWidth={2} color="var(--brand-magenta)" />
        <h2 className="filter-sidebar-title">Filters</h2>
      </div>

      {/* State filter */}
      <div className="filter-group">
        <label className="filter-label" htmlFor="filter-state">
          <MapPin size={13} strokeWidth={2} />
          State / UT
        </label>
        <select
          id="filter-state"
          className="input select filter-select"
          value={filters.state || ''}
          onChange={e => set('state', e.target.value || null)}
        >
          <option value="">All States</option>
          {(states || []).map(s => (
            <option key={s.name} value={s.name}>
              {s.name} ({s.count})
            </option>
          ))}
        </select>
      </div>

      {/* Category filter */}
      <div className="filter-group">
        <label className="filter-label" htmlFor="filter-cat">
          <Tag size={13} strokeWidth={2} />
          Category
        </label>
        <select
          id="filter-cat"
          className="input select filter-select"
          value={filters.category || ''}
          onChange={e => set('category', e.target.value || null)}
        >
          <option value="">All Categories</option>
          {(categories || []).map(c => (
            <option key={c.name} value={c.name}>
              {c.name} ({c.count})
            </option>
          ))}
        </select>
      </div>

      {/* Date filter */}
      <div className="filter-group">
        <label className="filter-label">
          <Calendar size={13} strokeWidth={2} />
          Document Date
        </label>
        {dateRange?.min ? (
          <>
            <label className="filter-checkbox-row">
              <input
                id="date-enable"
                type="checkbox"
                checked={dateEnabled}
                onChange={toggleDate}
              />
              <span>Filter by date</span>
            </label>
            {dateEnabled && (
              <div className="date-range-inputs">
                <div>
                  <label className="filter-sublabel">From</label>
                  <input
                    id="date-from"
                    type="date"
                    className="input"
                    min={dateRange.min}
                    max={dateRange.max}
                    value={filters.date_from || dateRange.min}
                    onChange={e => set('date_from', e.target.value)}
                  />
                </div>
                <div>
                  <label className="filter-sublabel">To</label>
                  <input
                    id="date-to"
                    type="date"
                    className="input"
                    min={dateRange.min}
                    max={dateRange.max}
                    value={filters.date_to || dateRange.max}
                    onChange={e => set('date_to', e.target.value)}
                  />
                </div>
                <p className="filter-caption">
                  {dateRange.dated_count} of {dateRange.total} documents have a detected date.
                </p>
              </div>
            )}
          </>
        ) : (
          <p className="filter-caption">No document dates detected yet in the database.</p>
        )}
      </div>

      {/* Scan body (basic mode only) */}
      {mode === 'basic' && (
        <div className="filter-group">
          <label className="filter-label">
            <FileSearch size={13} strokeWidth={2} />
            Search Scope
          </label>
          <label className="filter-checkbox-row">
            <input
              id="scan-body"
              type="checkbox"
              checked={scanBody}
              onChange={e => onScanBodyChange(e.target.checked)}
            />
            <span>Include full document text <small>(slower)</small></span>
          </label>
        </div>
      )}

      {/* Reset */}
      <button
        className="btn btn-ghost btn-sm filter-reset"
        onClick={() => {
          onFilterChange({ state: null, category: null, date_from: null, date_to: null })
          setDateEnabled(false)
        }}
      >
        <RotateCcw size={13} />
        Reset filters
      </button>
    </aside>
  )
}
