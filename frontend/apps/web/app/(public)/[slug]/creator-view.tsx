import { Button } from "@hyperchat/ui";
import { accentColor, formatAmount, imageSrc, type Creator, type Goal, type Pricing } from "./creator";

type Props = { creator: Creator; pricing: Pricing | null; goal: Goal | null };

/** The public creator page body. Pure: page.tsx loads the data, tests render this from fixtures. */
export function CreatorView({ creator, pricing, goal }: Props) {
  const accent = accentColor(creator.brand_color);
  const background = imageSrc(creator.background_url);
  const profile = imageSrc(creator.brand_logo_url);
  return (
    <main className="mx-auto max-w-5xl px-4 pb-12 sm:px-6">
      <div className="mt-4 aspect-video w-full overflow-hidden rounded-lg bg-muted sm:mt-6">
        {background ? (
          // eslint-disable-next-line @next/next/no-img-element -- remote creator images; width/height prevent layout shift
          <img
            src={background}
            alt=""
            width={1920}
            height={1080}
            fetchPriority="high"
            className="size-full object-cover"
          />
        ) : (
          <div
            data-testid="background-placeholder"
            className="size-full"
            style={{ borderTop: `4px solid ${accent}` }}
          />
        )}
      </div>

      <div className="-mt-10 flex flex-col items-start gap-4 px-2 sm:-mt-14 sm:flex-row sm:items-end sm:px-4">
        <div
          className="size-24 shrink-0 overflow-hidden rounded-full border-4 bg-muted sm:size-32"
          style={{ borderColor: accent }}
        >
          {profile ? (
            // eslint-disable-next-line @next/next/no-img-element -- as above
            <img src={profile} alt="" width={256} height={256} className="size-full object-cover" />
          ) : (
            <span
              data-testid="profile-placeholder"
              aria-hidden="true"
              className="flex size-full items-center justify-center font-display text-4xl text-foreground"
            >
              {creator.name.trim().charAt(0).toUpperCase()}
            </span>
          )}
        </div>
        <h1 className="text-3xl break-words sm:pb-2 sm:text-4xl">{creator.name}</h1>
      </div>

      <div className="mt-8 grid gap-6 md:grid-cols-[3fr_2fr]">
        <section aria-labelledby="send-heading" className="rounded-lg border border-border p-5">
          <h2 id="send-heading" className="text-2xl">
            Interact with {creator.name}
          </h2>
          <p className="mt-2 text-muted-foreground">
            Send a message, a voice note or a sound that plays live on stream.
          </p>
          <Button size="lg" className="mt-5 w-full sm:w-auto" disabled aria-describedby="send-soon">
            Send an interaction
          </Button>
          <p id="send-soon" className="mt-2 text-sm text-muted-foreground">
            Coming soon: sending interactions from this page is not open yet.
          </p>
        </section>

        <div className="flex flex-col gap-6">
          {goal && <GoalCard goal={goal} accent={accent} />}
          {pricing && <PricingCard pricing={pricing} creator={creator} />}
        </div>
      </div>
    </main>
  );
}

function GoalCard({ goal, accent }: { goal: Goal; accent: string }) {
  const progress = goal.progress_inr ?? 0;
  const target = goal.target_amount_inr;
  const percent = target ? Math.min(100, Math.round((progress / target) * 100)) : null;
  return (
    <section aria-labelledby="goal-heading" className="rounded-lg border border-border p-5">
      <h2 id="goal-heading" className="text-xl">
        {goal.name || "Stream goal"}
      </h2>
      <p className="mt-2 text-sm text-muted-foreground">
        {formatAmount(progress, "INR")}
        {target ? ` of ${formatAmount(target, "INR")}` : " raised"}
      </p>
      {percent !== null && (
        <div
          role="progressbar"
          aria-labelledby="goal-heading"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent}
          className="mt-3 h-2 overflow-hidden rounded-full bg-muted"
        >
          <div className="h-full rounded-full" style={{ width: `${percent}%`, backgroundColor: accent }} />
        </div>
      )}
    </section>
  );
}

function PricingCard({ pricing, creator }: { pricing: Pricing; creator: Creator }) {
  const rows: [string, number][] = [["Text message", pricing.min_text]];
  if (pricing.tts_enabled && creator.tts_enabled) rows.push(["Text-to-speech", pricing.min_tts]);
  rows.push(["Voice message", pricing.min_voice]);
  if (creator.soundboard_enabled) rows.push(["Sound", pricing.min_hypersound]);
  if (creator.media_upload_enabled) rows.push(["Image or GIF", pricing.min_media]);
  return (
    <section aria-labelledby="pricing-heading" className="rounded-lg border border-border p-5">
      <h2 id="pricing-heading" className="text-xl">
        Interactions start at
      </h2>
      <dl className="mt-3 grid grid-cols-[1fr_auto] gap-x-4 gap-y-2 text-sm">
        {rows.map(([label, amount]) => (
          <div key={label} className="contents">
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="text-right">{formatAmount(amount, pricing.currency)}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
