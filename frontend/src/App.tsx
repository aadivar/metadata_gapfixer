import { useEffect, useState } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { applyTheme, persistTheme, resolveInitial, Theme } from "./theme";
import { MoonIcon, SunIcon, UploadIcon } from "./icons";

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
      {isDark ? <SunIcon size={14} /> : <MoonIcon size={14} />}
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
            Metadata Generator
            <small>scholarly · Crossref</small>
          </div>
        </Link>

        <nav className="nav">
          <div className="nav-section">Workspace</div>
          <NavLink to="/upload" className={({ isActive }) => (isActive ? "active" : "")}>
            <NavIcon><UploadIcon size={16} /></NavIcon>
            Submissions
          </NavLink>
        </nav>

        <div className="sidebar-footer">
          <ThemeToggle />
          <div className="sidebar-credits">
            <div className="sidebar-credit-line">
              From the{" "}
              <a href="https://nexus-score.vercel.app/" target="_blank" rel="noreferrer">
                Nexus-score team
              </a>
            </div>
            <div className="sidebar-credit-line muted">
              <a href="https://github.com/aadivar/metadata_gapfixer" target="_blank" rel="noreferrer">
                Source on GitHub
              </a>
            </div>
            <div className="sidebar-credit-line muted">
              <a href="https://www.gnu.org/licenses/agpl-3.0.html" target="_blank" rel="noreferrer">
                AGPL-v3
              </a>
            </div>
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
