"""Time zones shared across the API.

IST (UTC+05:30, no daylight saving) is the creators' business day: dashboard revenue buckets and exports
(app.dashboard) and the times shown in bot messages (app.bots.format). Timestamps are stored and returned in UTC;
IST is only used to choose day boundaries and to format text for people.
"""

from datetime import timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")
