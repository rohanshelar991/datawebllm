import { useEffect, useMemo, useState } from "react";

const isLocalHost = typeof window !== "undefined" && ["localhost", "127.0.0.1"].includes(window.location.hostname);
const API_BASE = import.meta.env.VITE_API_BASE_URL || (isLocalHost ? "http://127.0.0.1:8000/api" : "/api");

async function apiFetch(path, { token, ...options } = {}) {
  const headers = new Headers(options.headers || {});
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (!(options.body instanceof FormData) && !headers.has("Content-Type") && options.body) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `Request failed with status ${response.status}`);
  }
  return response.json();
}

function MetricCard({ label, value, hint }) {
  return (
    <div className="metric-card">
      <span className="metric-label">{label}</span>
      <strong className="metric-value">{value}</strong>
      {hint ? <span className="metric-hint">{hint}</span> : null}
    </div>
  );
}

function ResultTable({ rows }) {
  if (!rows?.length) return <p className="empty-text">No rows available.</p>;
  const columns = Object.keys(rows[0]);
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <td key={`${index}-${column}`}>{String(row[column] ?? "")}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function rowsToCsv(rows) {
  if (!rows?.length) return "";
  const columns = Object.keys(rows[0]);
  const escapeValue = (value) => {
    const text = String(value ?? "");
    return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
  };
  return [columns.join(","), ...rows.map((row) => columns.map((column) => escapeValue(row[column])).join(","))].join("\n");
}

function downloadText(filename, text, mimeType = "text/plain") {
  const blob = new Blob([text], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export default function App() {
  const [health, setHealth] = useState(null);
  const [token, setToken] = useState(() => window.localStorage.getItem("session-token") || "");
  const [currentUser, setCurrentUser] = useState(null);
  const [authMode, setAuthMode] = useState("login");
  const [authForm, setAuthForm] = useState({ fullName: "", email: "", password: "" });
  const [datasets, setDatasets] = useState([]);
  const [dataset, setDataset] = useState(null);
  const [query, setQuery] = useState("");
  const [remoteUrl, setRemoteUrl] = useState("");
  const [dataView, setDataView] = useState("preview");
  const [result, setResult] = useState(null);
  const [history, setHistory] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const readyForActions = useMemo(() => Boolean(dataset?.dataset_id), [dataset]);

  useEffect(() => {
    apiFetch("/health")
      .then(setHealth)
      .catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    if (!token) return;
    apiFetch("/auth/me", { token })
      .then((payload) => {
        setCurrentUser(payload.user);
        return apiFetch("/datasets", { token });
      })
      .then(setDatasets)
      .catch((err) => {
        setError(err.message);
        setToken("");
        setCurrentUser(null);
        window.localStorage.removeItem("session-token");
      });
  }, [token]);

  async function handleAuthSubmit(event) {
    event.preventDefault();
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const path = authMode === "register" ? "/auth/register" : "/auth/login";
      const payload = await apiFetch(path, {
        method: "POST",
        body: JSON.stringify({
          email: authForm.email,
          password: authForm.password,
          full_name: authForm.fullName
        })
      });
      setToken(payload.token);
      setCurrentUser(payload.user);
      window.localStorage.setItem("session-token", payload.token);
      setNotice(`Signed in as ${payload.user.email}.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function refreshDatasets(activeDatasetId = null) {
    const listing = await apiFetch("/datasets", { token });
    setDatasets(listing);
    if (activeDatasetId) {
      const selected = await apiFetch(`/datasets/${activeDatasetId}`, { token });
      setDataset(selected);
    }
  }

  async function handleUpload(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const payload = await apiFetch("/datasets/upload", {
        method: "POST",
        token,
        body
      });
      setDataset(payload);
      setResult(null);
      await refreshDatasets(payload.dataset_id);
      setNotice(`Loaded ${payload.source_label} with ${payload.row_count.toLocaleString()} rows.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleDatasetSelect(datasetId) {
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const payload = await apiFetch(`/datasets/${datasetId}`, { token });
      setDataset(payload);
      setResult(null);
      setNotice(`Selected ${payload.source_label}.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleSampleLoad() {
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const payload = await apiFetch("/datasets/sample", { method: "POST", token });
      setDataset(payload);
      setResult(null);
      await refreshDatasets(payload.dataset_id);
      setNotice("Sample churn dataset loaded.");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleRemoteLoad(event) {
    event.preventDefault();
    if (!remoteUrl.trim()) return;
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const payload = await apiFetch("/datasets/from-url", {
        method: "POST",
        token,
        body: JSON.stringify({ url: remoteUrl })
      });
      setDataset(payload);
      setResult(null);
      setRemoteUrl("");
      await refreshDatasets(payload.dataset_id);
      setNotice(`Imported remote dataset with ${payload.row_count.toLocaleString()} rows.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleAsk(event) {
    event.preventDefault();
    if (!dataset?.dataset_id || !query.trim()) return;
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const payload = await apiFetch(`/datasets/${dataset.dataset_id}/query`, {
        method: "POST",
        token,
        body: JSON.stringify({ question: query })
      });
      setResult(payload);
      setHistory((current) => [payload, ...current].slice(0, 8));
      setNotice(`Analysis returned ${payload.rows_returned.toLocaleString()} rows in ${payload.runtime_ms} ms.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleCopySql() {
    if (!result?.sql) return;
    await navigator.clipboard.writeText(result.sql);
    setNotice("SQL copied to clipboard.");
  }

  function handleExportResults() {
    if (!result?.result?.length) return;
    downloadText("analysis-results.csv", rowsToCsv(result.result), "text/csv");
    setNotice("Result CSV downloaded.");
  }

  function handleExportProfile() {
    if (!dataset) return;
    downloadText(
      "dataset-profile.json",
      JSON.stringify(
        {
          source_label: dataset.source_label,
          row_count: dataset.row_count,
          column_count: dataset.column_count,
          health: dataset.health,
          columns: dataset.columns
        },
        null,
        2
      ),
      "application/json"
    );
    setNotice("Dataset profile downloaded.");
  }

  async function handleLogout() {
    try {
      await apiFetch("/auth/logout", { method: "POST", token });
    } catch (_) {
      // Best effort logout.
    }
    setToken("");
    setCurrentUser(null);
    setDataset(null);
    setDatasets([]);
    setHistory([]);
    setResult(null);
    window.localStorage.removeItem("session-token");
  }

  if (!health) {
    return <div className="page-shell"><div className="loading-panel">Loading platform status…</div></div>;
  }

  if (!token || !currentUser) {
    return (
      <div className="page-shell auth-shell">
        <div className="auth-card">
          <span className="eyebrow">Production Web App</span>
          <h1>Secure analyst workspace</h1>
          <p>Create an account or sign in to work with persistent datasets and account-based sessions.</p>
          <div className="auth-tabs">
            <button className={authMode === "login" ? "auth-tab active" : "auth-tab"} onClick={() => setAuthMode("login")}>
              Sign in
            </button>
            <button className={authMode === "register" ? "auth-tab active" : "auth-tab"} onClick={() => setAuthMode("register")}>
              Create account
            </button>
          </div>
          <form onSubmit={handleAuthSubmit} className="auth-form">
            {authMode === "register" ? (
              <input
                value={authForm.fullName}
                onChange={(event) => setAuthForm((current) => ({ ...current, fullName: event.target.value }))}
                placeholder="Full name"
              />
            ) : null}
            <input
              value={authForm.email}
              onChange={(event) => setAuthForm((current) => ({ ...current, email: event.target.value }))}
              placeholder="Email address"
              type="email"
            />
            <input
              value={authForm.password}
              onChange={(event) => setAuthForm((current) => ({ ...current, password: event.target.value }))}
              placeholder="Password"
              type="password"
            />
            <button type="submit" disabled={loading}>
              {loading ? "Working…" : authMode === "register" ? "Create account" : "Sign in"}
            </button>
          </form>
          <p className="muted-line">
            Auth mode: {health.auth_mode}. {health.allow_signups ? "Sign-ups are enabled." : "Sign-ups are restricted."}
          </p>
          {error ? <p className="error-banner">{error}</p> : null}
        </div>
      </div>
    );
  }

  return (
    <div className="page-shell">
      <header className="hero-card">
        <div>
          <span className="eyebrow">Industry-grade analytics website</span>
          <h1>Conversational Data Intelligence</h1>
          <p>Persistent datasets, account-backed sessions, and grounded NL-to-SQL analytics through a real web architecture.</p>
        </div>
        <div className="hero-pills">
          <span>{health.llm_configured ? "LLM ready" : "LLM missing"}</span>
          <span>{currentUser.full_name}</span>
          <button className="pill-button" onClick={handleLogout}>Sign out</button>
        </div>
      </header>

      {error ? <div className="error-banner">{error}</div> : null}
      {notice ? <div className="notice-banner">{notice}</div> : null}

      <main className="layout-grid">
        <section className="control-panel">
          <div className="section-header">
            <h2>Upload dataset</h2>
            <p>Upload your own CSV, XLSX, or JSON file to start analysis.</p>
          </div>

          <div className="stack">
            <label className="upload-card">
              <span>Upload CSV, XLSX, or JSON</span>
              <input type="file" accept=".csv,.xlsx,.xls,.json" onChange={handleUpload} />
            </label>
            <button type="button" className="secondary-button" onClick={handleSampleLoad} disabled={loading}>
              Load sample dataset
            </button>
            <form className="url-form" onSubmit={handleRemoteLoad}>
              <input
                value={remoteUrl}
                onChange={(event) => setRemoteUrl(event.target.value)}
                placeholder="Paste a raw CSV/JSON or GitHub blob URL"
                disabled={loading}
              />
              <button type="submit" className="secondary-button" disabled={loading || !remoteUrl.trim()}>
                Import URL
              </button>
            </form>
          </div>

          <div className="section-header top-gap">
            <h2>Saved datasets</h2>
          </div>
          <div className="history-list">
            {datasets.length ? datasets.map((item) => (
              <button
                key={item.id}
                className={dataset?.dataset_id === item.id ? "history-item active" : "history-item"}
                onClick={() => handleDatasetSelect(item.id)}
              >
                <strong>{item.source_label}</strong>
                <span>{item.row_count.toLocaleString()} rows · {item.column_count.toLocaleString()} columns</span>
              </button>
            )) : <p className="empty-text">No saved datasets yet.</p>}
          </div>

          <div className="section-header top-gap">
            <h2>Ask the dataset</h2>
            <p>Questions turn into validated DuckDB SQL executed only on the selected dataset.</p>
          </div>
          <form className="stack" onSubmit={handleAsk}>
            <textarea
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="What is the churn rate by gender?"
              rows={5}
              disabled={!readyForActions || loading}
            />
            <button type="submit" className="primary-button" disabled={!readyForActions || loading}>
              {loading ? "Working…" : "Run analysis"}
            </button>
            {dataset ? (
              <button type="button" className="secondary-button" onClick={handleExportProfile}>
                Export profile JSON
              </button>
            ) : null}
          </form>
        </section>

        <section className="workspace-panel">
          <div className="metrics-grid">
            <MetricCard label="Dataset rows" value={dataset?.row_count?.toLocaleString?.() || "—"} />
            <MetricCard label="Dataset columns" value={dataset?.column_count?.toLocaleString?.() || "—"} />
            <MetricCard label="Quality score" value={dataset?.health?.quality_score ? `${dataset.health.quality_score}/100` : "—"} />
            <MetricCard label="Datasets saved" value={String(datasets.length)} hint={currentUser.email} />
          </div>

          <div className="workspace-card">
            <div className="section-header">
              <h2>Dataset intelligence</h2>
              <p>Schema preview, quality signals, and suggested business prompts.</p>
            </div>
            {dataset ? (
              <div className="workspace-columns">
                <div>
                  <div className="dataset-title">
                    <span className="eyebrow">Active dataset</span>
                    <h3>{dataset.source_label}</h3>
                  </div>
                  <h3>Suggested questions</h3>
                  <div className="chip-list">
                    {(dataset.suggested_questions || []).map((item) => (
                      <button key={item} className="chip" onClick={() => setQuery(item)}>{item}</button>
                    ))}
                  </div>
                  <h3>Quality issues</h3>
                  <ul className="bullet-list">
                    {dataset.health?.issues?.length ? dataset.health.issues.map((item) => <li key={item}>{item}</li>) : <li>No major quality issues detected.</li>}
                  </ul>
                </div>
                <div>
                  <div className="view-toggle">
                    <button className={dataView === "preview" ? "active" : ""} onClick={() => setDataView("preview")}>Preview</button>
                    <button className={dataView === "columns" ? "active" : ""} onClick={() => setDataView("columns")}>Columns</button>
                    <button className={dataView === "schema" ? "active" : ""} onClick={() => setDataView("schema")}>Schema</button>
                  </div>
                  {dataView === "preview" ? <ResultTable rows={dataset.preview} /> : null}
                  {dataView === "columns" ? <ResultTable rows={dataset.columns} /> : null}
                  {dataView === "schema" ? <pre className="schema-block">{dataset.schema_text}</pre> : null}
                </div>
              </div>
            ) : <p className="empty-text">Upload a dataset or select one you already saved to begin.</p>}
          </div>

          <div className="workspace-card">
            <div className="section-header">
              <h2>Analysis output</h2>
              <p>Grounded answer, validated SQL, and exact returned rows.</p>
            </div>
            {result ? (
              <>
                <div className="analysis-grid">
                  <MetricCard label="Rows returned" value={String(result.rows_returned)} />
                  <MetricCard label="Attempts" value={String(result.attempts)} />
                  <MetricCard label="Runtime" value={`${result.runtime_ms} ms`} />
                </div>
                <div className="answer-block">
                  <h3>Answer</h3>
                  <p>{result.answer}</p>
                </div>
                <div className="sql-block">
                  <div className="block-heading">
                    <h3>Generated SQL</h3>
                    <button type="button" className="mini-button" onClick={handleCopySql}>Copy SQL</button>
                  </div>
                  <pre>{result.sql}</pre>
                </div>
                <div className="answer-block">
                  <h3>Explanation</h3>
                  <p>{result.explanation}</p>
                </div>
                <div className="toolbar-row">
                  <button type="button" className="secondary-button" onClick={handleExportResults} disabled={!result.result?.length}>
                    Export result CSV
                  </button>
                </div>
                <ResultTable rows={result.result} />
              </>
            ) : (
              <p className="empty-text">Run an analysis to populate the result workspace.</p>
            )}
          </div>

          <div className="workspace-card">
            <div className="section-header">
              <h2>Recent analyses</h2>
            </div>
            {history.length ? (
              <div className="history-list">
                {history.map((item) => (
                  <button key={`${item.question}-${item.runtime_ms}`} className="history-item" onClick={() => {
                    setQuery(item.question);
                    setResult(item);
                  }}>
                    <strong>{item.question}</strong>
                    <span>{item.rows_returned.toLocaleString()} rows · {item.runtime_ms} ms · {item.attempts} attempt{item.attempts === 1 ? "" : "s"}</span>
                  </button>
                ))}
              </div>
            ) : <p className="empty-text">No analysis history yet.</p>}
          </div>
        </section>
      </main>
    </div>
  );
}
