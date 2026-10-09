"""Notification texts, buttons and escaping (legacy layout, D8).

Every piece of donor text (name, message) is escaped for its target: Telegram HTML (parse_mode HTML) and Discord
markdown; Discord messages also carry allowed_mentions limited to the moderators the bot means to ping, so donor
text can never ping @everyone, a role or a user. Messages are cut to 200 (Telegram) / 1024 (Discord embed) chars.
Link buttons use only allow-listed objects of this environment's bucket (app.obs.media rules / Phase 6 keys) and the
dashboard URL built from HYPERCHAT_FRONTEND_URL.
"""

import html
import re
from datetime import datetime
from decimal import Decimal

from app.core.errors import ApiError
from app.core.timezones import IST
from app.payments import rules as payment_rules

CURRENCY = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "AED": "د.إ", "AUD": "A$"}
TELEGRAM_EXCERPT, DISCORD_EXCERPT = 200, 1024
_MD = re.compile(r"([\\*_~`|>#\[\]()<@!:-])")
LABELS = {"approve": "Approved", "reject": "Rejected", "hide_message": "Message hidden", "ban_donor": "Donor banned"}
EMOJI = {"approve": "✅", "reject": "❌", "hide_message": "🙈", "ban_donor": "🚫"}
BUTTON = {"approve": "✅ Approve", "reject": "❌ Reject", "hide_message": "🙈 Hide Msg", "ban_donor": "🚫 Ban"}


def esc_html(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def esc_md(text) -> str:
    return _MD.sub(r"\\\1", str(text if text is not None else ""))


def excerpt(text: str | None, limit: int) -> str:
    text = text or ""
    return text[:limit] + ("..." if len(text) > limit else "")


def amount(d: dict) -> str:
    value = Decimal(str(d["amount"]))
    shown = f"{value:.0f}" if value == value.to_integral_value() else f"{value:.2f}"
    currency = d.get("currency") or "INR"
    return f"{CURRENCY.get(currency, currency)}{shown} {currency}"


def when(value: datetime) -> str:
    return value.astimezone(IST).strftime("%d/%m/%Y, %I:%M:%S %p")


def links(d: dict, public_base: str | None, frontend_url: str) -> list[tuple[str, str]]:
    """(label, url) link buttons: voice / media only when they are this creator's uploads in this environment's
    bucket; the dashboard always."""
    out = []
    for kind, label, column in (("voice", "🎵 Listen Voice", "voice_message_url"),
                                ("media", "📎 View Media", "media_url")):
        url = d.get(column)
        if url and public_base:
            try:
                payment_rules.upload_key(url, public_base, d["streamer_slug"], kind)
                out.append((label, url))
            except ApiError:
                pass
    out.append(("📊 Dashboard", f"{frontend_url.rstrip('/')}/dashboard/{d['streamer_slug']}"))
    return out


def telegram_text(d: dict) -> str:
    pending = d["moderation_status"] == "pending"
    lines = ["🎁 <b>New Donation - Needs Approval</b> 🎁" if pending else "🎁 <b>New Donation Received!</b> 🎁", "",
             f"💰 <b>Amount:</b> {esc_html(amount(d))}", f"👤 <b>From:</b> {esc_html(d['donor_name'])}",
             f"📅 <b>Time:</b> {esc_html(when(d['created_at']))}"]
    if d.get("message") and d.get("message_visible", True):
        lines.append(f"💬 <b>Message:</b> {esc_html(excerpt(d['message'], TELEGRAM_EXCERPT))}")
    if d.get("voice_message_url"):
        lines.append("🎵 <b>Voice:</b> Available")
    if d.get("media_url"):
        lines.append(f"📎 <b>Media:</b> {esc_html((d.get('media_type') or 'media').capitalize())} attached")
    lines += ["", "⏳ <i>Pending Approval</i>" if pending else "✅ <i>Auto-Approved</i>"]
    return "\n".join(lines)


def telegram_keyboard(actions: dict[str, str], link_buttons: list[tuple[str, str]]) -> list:
    """actions: action -> callback_data ("m:<id>"), already filtered by the moderator's permissions."""
    keyboard = []
    for group in (("approve", "reject"), ("hide_message", "ban_donor")):
        row = [{"text": BUTTON[a], "callback_data": actions[a]} for a in group if a in actions]
        if row:
            keyboard.append(row)
    keyboard += [[{"text": label, "url": url}] for label, url in link_buttons]
    return keyboard


def discord_payload(d: dict, actions: dict[str, str], link_buttons: list[tuple[str, str]],
                    mention_ids: list[str]) -> dict:
    pending = d["moderation_status"] == "pending"
    fields = [{"name": "💰 Amount", "value": esc_md(amount(d)), "inline": True},
              {"name": "👤 From", "value": esc_md(d["donor_name"])[:1024] or "-", "inline": True},
              {"name": "📅 Time", "value": when(d["created_at"]), "inline": True}]
    if d.get("message") and d.get("message_visible", True):
        fields.append({"name": "💬 Message", "value": esc_md(excerpt(d["message"], 900))[:DISCORD_EXCERPT],
                       "inline": False})
    if d.get("voice_message_url"):
        fields.append({"name": "🎵 Voice", "value": "Available", "inline": True})
    if d.get("media_url"):
        fields.append({"name": "📎 Media", "value": f"{(d.get('media_type') or 'media').capitalize()} attached",
                       "inline": True})
    embed = {"title": "🎁 New Donation - Needs Approval" if pending else "🎁 New Donation Received!",
             "color": 0xFFA500 if pending else 0x00FF00, "fields": fields,
             "footer": {"text": "⏳ Pending Approval" if pending else "✅ Auto-Approved"}}
    components = []
    styles = {"approve": 3, "reject": 4, "hide_message": 2, "ban_donor": 4}
    for group in (("approve", "reject"), ("hide_message", "ban_donor")):
        row = [{"type": 2, "style": styles[a], "label": BUTTON[a], "custom_id": actions[a]}
               for a in group if a in actions]
        if row:
            components.append({"type": 1, "components": row})
    for label, url in link_buttons:
        components.append({"type": 1, "components": [{"type": 2, "style": 5, "label": label, "url": url}]})
    mentions = mention_ids[:5] if pending else []
    payload = {"embeds": [embed], "components": components,
               "allowed_mentions": {"parse": [], "users": mentions}}
    if mentions:
        payload["content"] = " ".join(f"<@{m}>" for m in mentions) + " 🎟️ Action needed:"
    return payload


def result_line(action: str, applied: bool, moderator_name: str, new_status: str) -> str:
    if not applied:
        return f"ℹ️ No change (already {new_status.replace('_', ' ')})"
    return f"{EMOJI[action]} {LABELS[action]} by {moderator_name}"
