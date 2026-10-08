"""Moderation bots (Phase 11): Telegram and Discord.

  clients.py     Telegram Bot API / Discord API v10 clients (httpx), Discord Ed25519 verification
  format.py      notification texts, buttons and escaping (Telegram HTML, Discord markdown, mentions)
  repository.py  SQL: notification context, recipients, deliveries, callback rows, moderator lookups, commands
  notify.py      the moderator_notify job handler (worker)
  telegram.py    the Telegram webhook: commands and button presses
  discord.py     the Discord interactions endpoint: button presses
  permissions.py which bot actions a moderator may take

Moderation itself goes through the shared core (app.dashboard.repository.DashboardRepository.moderate with a
BotActor): no HTTP hop. Outside production only allow-listed recipient ids are ever messaged (D13).
"""
