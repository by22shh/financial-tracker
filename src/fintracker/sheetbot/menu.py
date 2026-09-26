"""Persistent Telegram menu labels and their internal routes."""

from fintracker.sheetbot.models import ReportScope

TODAY = "📊 Сегодня"
WEEK = "🗓 За неделю"
PERIOD = "📈 За весь период"
CATEGORIES = "📋 Категории"
NEW_PERIOD = "📅 Новый период"
HELP = "❓ Помощь"
CANCEL = "✖️ Отменить ввод"

ROWS = ((TODAY, WEEK), (PERIOD, CATEGORIES), (NEW_PERIOD, HELP), (CANCEL,))
ACTIONS = {
    TODAY: "/today",
    WEEK: "/week",
    PERIOD: "/summary",
    CATEGORIES: "/categories",
    NEW_PERIOD: "/period",
    HELP: "/help",
    CANCEL: "/cancel",
}

REPORT_SCOPES: dict[str, ReportScope] = {"/today": "today", "/week": "week", "/summary": "period"}
