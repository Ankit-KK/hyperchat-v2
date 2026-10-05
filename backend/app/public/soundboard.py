"""Public soundboard list (E4): merge platform defaults with a creator's overrides. Pure function.

Same rules as the legacy list-streamer-soundboard:
  * every platform sound is included unless the creator's override row hides it; override fields
    (name, audio, image, order) win over the platform defaults;
  * creator custom sounds (no platform slot) are included only when visible;
  * sounds without audio are skipped; result ordered by order index (missing order last).
"""

_LAST = 9999


def merge(platform_slots: list[dict], streamer_slots: list[dict]) -> list[dict]:
    overrides = {s["platform_slot_id"]: s for s in streamer_slots if s["platform_slot_id"]}
    items: list[tuple[int, int, dict]] = []
    for p in platform_slots:
        o = overrides.get(p["id"])
        if o is not None and o["is_visible"] is False:
            continue
        url = (o and o["audio_url"]) or p["audio_url"]
        if not url:
            continue
        order = o["order_index"] if o is not None and o["order_index"] is not None else p["default_order_index"]
        items.append((order if order is not None else _LAST, len(items), {
            "id": o["id"] if o is not None else p["id"],
            "display_name": (o and o["display_name"]) or p["display_name"] or "",
            "audio_url": url,
            "thumb_url": (o and o["image_url"]) or p["image_url"] or None,
        }))
    for s in streamer_slots:
        if s["platform_slot_id"] or s["is_visible"] is not True or not s["audio_url"]:
            continue
        items.append((s["order_index"] if s["order_index"] is not None else _LAST, len(items), {
            "id": s["id"], "display_name": s["display_name"] or "", "audio_url": s["audio_url"],
            "thumb_url": s["image_url"] or None,
        }))
    items.sort(key=lambda t: (t[0], t[1]))
    return [item for _, _, item in items]
