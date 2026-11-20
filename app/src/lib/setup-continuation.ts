// Only the one-use capability survives a reload, never passwords, factors or sessions.
const KEY = "fiscalm:setup-continuation";
const LIFETIME = 30 * 60 * 1000;
type Continuation = { link: string; until: number; session: string | null };

export function clearSetupContinuation() {
  try { sessionStorage.removeItem(KEY); } catch { /* Storage may be disabled. */ }
}

export function setupContinuation(sessionKey: string): string | null {
  if (location.pathname !== "/setup") { clearSetupContinuation(); return null; }
  const link = location.hash.slice(1);
  try {
    const session = localStorage.getItem(sessionKey);
    if (link) {
      clearSetupContinuation();
      // Invalid supplied links still reach inspect, but are never retained.
      if (/^[0-9a-f]{32}\.[0-9a-f]{32}$/.test(link)) {
        sessionStorage.setItem(KEY, JSON.stringify({ link, until: Date.now() + LIFETIME, session }));
      }
      history.replaceState(history.state, "", "/setup");
      return link;
    }
    const saved: Continuation | null = JSON.parse(sessionStorage.getItem(KEY) || "null");
    // A copied tab or a fresh navigation must not silently resume an account flow.
    const reload = (performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined)?.type === "reload";
    if (reload && saved && /^[0-9a-f]{32}\.[0-9a-f]{32}$/.test(saved.link)
      && saved.until > Date.now() && saved.until <= Date.now() + LIFETIME && saved.session === session) return saved.link;
  } catch { /* Without storage, keep the fragment so a reload can still use it. */
    if (link) return link;
  }
  clearSetupContinuation();
  return null;
}
