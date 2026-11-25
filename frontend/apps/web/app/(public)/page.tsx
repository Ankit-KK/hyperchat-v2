import { serverApi } from "../../lib/api";

/** The landing page. GET /api/v1/metrics/showcase is anonymous; when the API is unreachable the figures are hidden. */
export default async function Landing() {
  const showcase = await serverApi().get("/api/v1/metrics/showcase");
  return (
    <main className="mx-auto max-w-5xl px-4 py-12">
      <h1 className="text-3xl font-semibold">Interactive donations for your stream</h1>
      <p className="mt-3 max-w-2xl text-muted-foreground">
        Viewers send messages, voice and sounds; your overlay plays them live.
      </p>
      {showcase.ok && (
        <dl className="mt-10 grid max-w-md grid-cols-2 gap-6" aria-label="Platform activity">
          <div>
            <dt className="text-sm text-muted-foreground">Creators live</dt>
            <dd className="text-2xl font-semibold">{showcase.data.streamers_live.toLocaleString("en-IN")}</dd>
          </div>
          <div>
            <dt className="text-sm text-muted-foreground">Alerts in the last 24 hours</dt>
            <dd className="text-2xl font-semibold">
              {showcase.data.alerts_last_24h.toLocaleString("en-IN")}
            </dd>
          </div>
        </dl>
      )}
    </main>
  );
}
