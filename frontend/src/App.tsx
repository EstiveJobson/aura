import './app.css';

const foundations = [
  'FastAPI service boundary',
  'PostgreSQL infrastructure',
  'Automated quality gates',
];

const apiDocsUrl = (
  import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000/api'
).replace(/\/api\/?$/, '/docs');

export function App() {
  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="AURA home">
          <span className="brand-mark">A</span>
          <span>AURA</span>
        </a>
        <span className="phase-badge">Phase 0</span>
      </header>

      <main className="foundation-panel">
        <section className="intro" aria-labelledby="foundation-title">
          <p className="eyebrow">Foundation online</p>
          <h1 id="foundation-title">The workspace starts here.</h1>
          <p className="summary">
            AURA now has the stable application and infrastructure boundaries
            required for its first deterministic agent workflow.
          </p>
          <a className="api-link" href={apiDocsUrl}>
            Open API documentation
            <span aria-hidden="true">↗</span>
          </a>
        </section>

        <section className="status-card" aria-labelledby="status-title">
          <div className="status-heading">
            <div>
              <p className="card-label">Current milestone</p>
              <h2 id="status-title">Ready for Phase 1</h2>
            </div>
            <span className="status-dot" aria-label="Foundation status ready" />
          </div>
          <ul>
            {foundations.map((foundation) => (
              <li key={foundation}>
                <span aria-hidden="true">✓</span>
                {foundation}
              </li>
            ))}
          </ul>
          <p className="scope-note">
            Agent execution and task workflows are intentionally not active in
            this phase.
          </p>
        </section>
      </main>
    </div>
  );
}

export default App;
