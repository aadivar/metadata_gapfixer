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
            Metadata Gap Fixer
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
