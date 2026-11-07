import React from 'react'
import './Navbar.css'

export default function Navbar({ activeTab, onTabChange }) {
  return (
    <header className="navbar">
      <div className="container navbar-inner">
        <div className="navbar-brand">
          <img
            src="/enfold-logo.png"
            alt="Enfold India"
            className="brand-logo-img"
          />
        </div>

        <nav className="navbar-nav">
          <button
            className={`nav-link ${activeTab === 'search' ? 'active' : ''}`}
            onClick={() => onTabChange('search')}
          >
            Search Documents
          </button>
          <button
            className={`nav-link ${activeTab === 'coverage' ? 'active' : ''}`}
            onClick={() => onTabChange('coverage')}
          >
            Database Coverage
          </button>
          <a
            href="https://enfoldindia.org"
            target="_blank"
            rel="noopener noreferrer"
            className="nav-link nav-link-ext"
          >
            enfoldindia.org
          </a>
        </nav>
      </div>
    </header>
  )
}
