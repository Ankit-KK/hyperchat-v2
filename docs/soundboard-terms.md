# Custom Soundboard terms (D3)

Owner decisions, 2026-10-13: **keep the Custom Soundboard terms gate.** Implemented as checklist DASH-11
(`backend/app/soundboard/terms.py`; API in [`soundboard-api.md`](soundboard-api.md)).

- **Current version: `soundboard-2026-10-13`** (owned by the server; the accept request must name it exactly).
- **Rule:** a creator may use the Custom Soundboard only when (1) it is eligible (an admin granted it), (2) its
  **owner** accepted `soundboard-2026-10-13`, and (3) that acceptance is not revoked. Eligibility and acceptance are
  separate controls: granting eligibility never accepts, revoking it never deletes acceptances.
- **Who accepts:** the creator owner only. Linked/moderator accounts cannot, and an admin cannot on the owner's behalf.
- **Older acceptances do not count.** Legacy creators accepted `soundboard-2026-07-14` (the table default
  `soundboard-2026-07-13` is older); those rows are kept as history, and every currently enabled creator must accept
  `soundboard-2026-10-13` before the Custom Soundboard works again.

---

HyperChat Custom Soundboard – Terms & Conditions

1. Invitation-Only Access — The Custom Soundboard is an exclusive premium feature. It is not included with any
   subscription plan and is not currently separately purchasable. Access is provided only after acceptance of these
   terms.
2. Monthly Content Requirement — The streamer must publish at least one reel per calendar month featuring HyperChat.
3. Tag Requirement — Every qualifying reel must tag HyperChat's official Instagram account: `@hype.rchat`.
4. Content Authenticity — The reel must genuinely showcase or promote HyperChat. Low-effort, misleading, spam,
   AI-generated spam, or unrelated content does not count.
5. Content Rights — The streamer confirms they own or have the necessary rights to all audio, video, images, music,
   and other content used in the reel and uploaded to the Custom Soundboard.
6. Compliance — The streamer must comply with HyperChat's Terms of Service, Community Guidelines, and applicable
   platform policies.
7. Revocation — HyperChat may revoke access if the streamer fails the monthly requirement, removes the required tag,
   violates the terms, or misuses the feature.
8. Administrative Penalty — Failure to comply after receiving access may result in an administrative penalty starting
   at ₹250. The final amount may increase depending on severity or repeated violations. Payment does not guarantee
   access restoration.
9. No Guaranteed Availability — HyperChat may modify, suspend, discontinue, or change eligibility requirements at its
   discretion.
10. Acceptance — The streamer acknowledges they have read, understood, and accepted the terms. Access is granted after
    acceptance and may later be revoked.
