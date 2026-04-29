import { Link, Outlet, useLocation } from "react-router-dom";

export default function App() {
  const loc = useLocation();
  return (
    <div className="app">
      <header>
        <h1>Metadata Gap Fixer</h1>
        <nav>
          <Link to="/upload" className={loc.pathname.startsWith("/upload") ? "active" : ""}>
            Upload
          </Link>
        </nav>
      </header>
      <main>
        <Outlet />
      </main>
    </div>
  );
}
