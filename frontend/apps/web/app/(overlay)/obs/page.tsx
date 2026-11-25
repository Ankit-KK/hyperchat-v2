"use client";

/**
 * overlay. host shell for OBS browser sources. It never uses the session: the alerts and goal overlays (Phase 7) read
 * an OBS token from the URL fragment and send it as x-obs-token (review §3.2 step 6).
 */
export default function OverlayShell() {
  return <main aria-label="HyperChat overlay" className="min-h-screen bg-transparent" />;
}
