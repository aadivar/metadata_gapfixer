import { useEffect, useState } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { applyTheme, persistTheme, resolveInitial, Theme } from "./theme";

function NavIcon({ children }: { children: React.ReactNode }) {
  return <span className="nav-icon" aria-hidden>{children}</span>;
}

function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => resolveInitial());

  useEffect(() => {
    applyTheme(theme);
    persistTheme(theme);
  }, [theme]);

  const isDark = theme === "dark";
  return (
    <button
      className="theme-toggle"
      onClick={() => setTheme(isDark ? "light" : "dark")}
      aria-label={`Switch to ${isDark ? "light" : "dark"} theme`}
      title={`Switch to ${isDark ? "light" : "dark"} theme`}
    >
      {isDark ? (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
        </svg>
      ) : (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      )}
      <span>{isDark ? "Light mode" : "Dark mode"}</span>
    </button>
  );
}

export default function App() {
  return (
    <div className="shell">
      <aside className="sidebar">
        <Link to="/" className="brand" style={{ textDecoration: "none", color: "inherit" }}>
          <div className="brand-mark">M</div>
          <div className="brand-text">
            Metadata Gap Fixer
            <small>scholarly · Crossref</small>
          </div>
        </Link>

        <nav className="nav">
          <div className="nav-section">Workspace</div>
          <NavLink to="/upload" className={({ isActive }) => (isActive ? "active" : "")}>
            <NavIcon>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="17 8 12 3 7 8" />
                <line x1="12" y1="3" x2="12" y2="15" />
              </svg>
            </NavIcon>
            Submissions
          </NavLink>
        </nav>

        <div className="sidebar-footer">
          <ThemeToggle />
          <div className="muted small" style={{ marginTop: 10 }}>
            v0.1 · local pipeline
            <br />
            docling · gliner2 · llm
          </div>
        </div>
      </aside>

      <main className="main">
        <div className="main-inner">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
