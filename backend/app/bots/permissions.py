"""Which bot actions a moderator may take (legacy rules, checked at click time, D9).

  role owner     every action          role viewer   none
  role moderator its can_* flags (NULL = the column default: approve / reject / hide yes, ban no)
Bot buttons offer approve, reject, hide_message and ban_donor (legacy never offered replay from the bots).
"""

BOT_ACTIONS = ("approve", "reject", "hide_message", "ban_donor")
_FLAG = {"approve": ("can_approve", True), "reject": ("can_reject", True),
         "hide_message": ("can_hide_message", True), "ban_donor": ("can_ban", False)}


def can(moderator: dict, action: str) -> bool:
    if action not in _FLAG or not moderator.get("is_active", True):
        return False
    role = moderator.get("role") or "moderator"
    if role == "owner":
        return True
    if role == "viewer":
        return False
    column, default = _FLAG[action]
    value = moderator.get(column)
    return default if value is None else bool(value)
