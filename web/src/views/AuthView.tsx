import { ArrowRight } from "lucide-react";
import { useState } from "react";
import { Glass } from "../components/Glass";
import { api } from "../lib/api";

export function AuthView({ configured, onDone }: { configured: boolean; onDone: () => void }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setError("");
    if (!configured && password !== confirm) return setError("Passwords don't match.");
    setBusy(true);
    try {
      await api.post(configured ? "/api/auth/login" : "/api/auth/setup", { password });
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth">
      <Glass
        as="form"
        className="auth-card"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div className="brand-mark" style={{ width: 54, height: 54, borderRadius: 18, margin: "0 auto" }}>
          <svg width="30" height="30" viewBox="0 0 64 64" aria-hidden="true"><path d="M10 40c7-5 15-5 22 0s15 5 22 0" stroke="#111" strokeWidth="5" fill="none" strokeLinecap="round" /><circle cx="32" cy="23" r="8" fill="#111" /></svg>
        </div>
        <h1>
          <span className="dim">{configured ? "Welcome back to" : "This is"}</span>
          Ithaca
        </h1>
        <p className="muted" style={{ margin: "0 0 4px" }}>
          {configured ? "Enter your password to continue." : "Your private AI workspace. Choose a password to protect it."}
        </p>
        <input className="input" type="password" placeholder="Password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} autoComplete={configured ? "current-password" : "new-password"} />
        {!configured && <input className="input" type="password" placeholder="Confirm password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />}
        {error && <div style={{ color: "var(--danger)", fontSize: 13.5 }}>{error}</div>}
        <button className="btn primary" style={{ height: 46 }} disabled={busy || password.length < (configured ? 1 : 8)}>
          {configured ? "Sign in" : "Create password"} <ArrowRight size={17} />
        </button>
        {!configured && <small className="faint">At least 8 characters. Stored as a scrypt hash on this drive.</small>}
      </Glass>
    </div>
  );
}
