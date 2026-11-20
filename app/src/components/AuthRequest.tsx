"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { AuthError } from "@/lib/api";
import { nOf } from "@/lib/format";
const UNAVAILABLE = "Cererea nu a fost confirmată. Încearcă din nou.";

export function useRequest(unconfirmed = UNAVAILABLE) {
  const running = useRef(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [delay, setDelay] = useState(0);
  useEffect(() => { if (delay) { const t = setTimeout(() => setDelay(n => Math.max(0, n - 1)), 1000); return () => clearTimeout(t); } }, [delay]);
  const defer = useCallback((error: unknown) => { if (error instanceof AuthError) setDelay(error.retryAfter); }, []);
  async function run<T>(request: () => Promise<T>): Promise<T | null> {
    if (running.current || busy || delay) return null;
    running.current = true; setBusy(true); setError("");
    try { return await request(); }
    catch (e) { setError(e instanceof AuthError ? e.message : unconfirmed); defer(e); return null; }
    finally { running.current = false; setBusy(false); }
  }
  return { run, defer, clear: () => setError(""), busy: busy || delay > 0, feedback: <>{error && <p role="alert">{error}</p>}{delay > 0 && <p role="status">Încearcă din nou peste {nOf(delay, "o secundă", "secunde")}.</p>}</> };
}
