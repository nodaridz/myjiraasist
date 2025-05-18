# jira_ai_assist_main.py
"""Telegram‑бот‑ассистент для Jira (один файл)

Версия: 17 мая 2025 г.  
Изменения:
• В карточке показывается автор последнего комментария.  
• Команда /refresh (и кнопка 🔄 Обновить) — мгновенно перечитывает тикеты arif.nagiyev@uninet.az.  
• Команда /find <KEY> — присылает карточку по указанному ключу (поиск тикета).  
• ganclik сдвигает дату +1 день.  
• Переходы ищутся по имени.

requirements.txt без изменений.
"""
from __future__ import annotations

import json, logging, logging.handlers, os, random, re
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Set

import httpx
from dotenv import load_dotenv
from jira import JIRA, JIRAError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          MessageHandler, ContextTypes, filters)

# ───── 1. Конфиг ───────────────────────────────────────────────────────────
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
JIRA_SERVER  = os.getenv("JIRA_SERVER")
JIRA_USER    = os.getenv("JIRA_USER")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
CHAT_ID = int(os.getenv("OPERATOR_CHAT_ID", "0")) or None
GANCLIK_ASSIGNEE = os.getenv("GANCLIK", "ganclikoffice@uninet.az")
CDC_CC_ASSIGNEE  = os.getenv("CDC_CC", "cdc_cc_inbound_operations@azerconnect.az")
CF_ONHOLD_REASON = os.getenv("CF_ONHOLD_REASON", "customfield_12709")
CF_CONNECT_DATE  = os.getenv("CF_CONNECT_DATE", "customfield_11512")
NAME_IN_PROGRESS = os.getenv("NAME_IN_PROGRESS", "In Progress")
NAME_ON_HOLD     = os.getenv("NAME_ON_HOLD", "On Hold")
JQL = ('assignee = "arif.nagiyev@uninet.az" '
       'AND resolution = Unresolved AND project = ISP')
MEMORY_FILE = Path("memory.json")
MEMORY_EXAMPLES = 5
CHECK_INTERVAL = 900  # 15 мин
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[logging.StreamHandler(),
        logging.handlers.RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=3)])
log = logging.getLogger("jira-assistant")

# ───── 2. Память ───────────────────────────────────────────────────────────
def _load_mem():
    if MEMORY_FILE.exists():
        with suppress(json.JSONDecodeError):
            return json.loads(MEMORY_FILE.read_text("utf-8"))
    return []

def _save_mem(m):
    MEMORY_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2), "utf-8")

def remember(key: str, act: str):
    m = _load_mem(); m.append({"key": key, "action": act, "ts": datetime.now(timezone.utc).isoformat()}); _save_mem(m[-1000:])

def examples(n: int):
    m = _load_mem(); return random.sample(m, min(len(m), n))

# ───── 3. Jira wrapper ─────────────────────────────────────────────────────
class Jira:
    def __init__(self):
        self.api = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_USER, JIRA_API_TOKEN))
    def search(self, jql: str):
        return self.api.search_issues(jql, maxResults=50)
    def transition(self, key: str, name: str, fields: Dict[str, Any] | None = None):
        t = next((t for t in self.api.transitions(key) if t["name"].casefold().replace(" ","") == name.casefold().replace(" ","")), None)
        if not t:
            log.warning("Transition '%s' недоступен для %s", name, key); return
        try: self.api.transition_issue(key, t["id"], fields=fields or {})
        except JIRAError as e: log.error("Transition error %s→%s: %s", key, name, e)
    def assign(self, key: str, who: str):
        self.api.assign_issue(key, who)
    def update(self, key: str, f):
        self.api.issue(key).update(fields=f)
    def get(self, key: str):
        return self.api.issue(key)

jira = Jira()

# ───── 4. LLM ──────────────────────────────────────────────────────────────
async def llm_recommend(issue, ex):
    msgs = [{"role":"system","content":"Ты ИИ-ассистент по Jira. Выбери: in_progress, on_hold, ganclik, cdc_cc, skip."}]
    for e in ex:
        msgs += [{"role":"user","content":f"Тикет {e['key']} — что сделать?"},{"role":"assistant","content":e['action']}]
    msgs.append({"role":"user","content":f"Тикет {issue.key}. Что делать?"})
    try:
        async with httpx.AsyncClient(timeout=60) as cl:
            r = await cl.post("https://openrouter.ai/api/v1/chat/completions",
                json={"model":"mistralai/mistral-7b-instruct","messages":msgs},
                headers={"Authorization":f"Bearer {OPENROUTER_API_KEY}"})
            r.raise_for_status(); return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning("LLM error: %s", e); return "(ИИ недоступен)"

# ───── 5. Telegram helpers ────────────────────────────────────────────────
def kb(key: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 В работу", callback_data=f"{key}:in_progress"),
         InlineKeyboardButton("⏸ On Hold", callback_data=f"{key}:on_hold")],
        [InlineKeyboardButton("🏢 ganclik", callback_data=f"{key}:ganclik"),
         InlineKeyboardButton("🏢 cdc cc", callback_data=f"{key}:cdc_cc")],
    ])

def card(issue):
    st = issue.fields.status.name if issue.fields.status else "—"
    last_auth = last_text = "—"
    if issue.fields.comment and issue.fields.comment.comments:
        last = issue.fields.comment.comments[-1]
        last_text = last.body[:300]
        last_auth = getattr(last.author, "displayName", "?")
    return (f"<b>📋 <a href='{JIRA_SERVER}/browse/{issue.key}'>{issue.key}</a></b> <i>{st}</i>\n"
            f"<b>Комментарий:</b> {last_text} — <i>{last_auth}</i>")

# ───── 6. Button & command handlers ───────────────────────────────────────
async def button_cb(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    if q.data == "refresh":
        await send_all(ctx, force=True); return
    key, act = q.data.split(":", 1)
    try:
        if act == "in_progress":
            jira.transition(key, NAME_IN_PROGRESS)
        elif act == "on_hold":
            jira.transition(key, NAME_ON_HOLD, {CF_ONHOLD_REASON:{"value":"Due to Customer"}})
        elif act == "ganclik":
            jira.assign(key, GANCLIK_ASSIGNEE)
            jira.update(key, {CF_CONNECT_DATE:(datetime.now(timezone.utc)+timedelta(days=1)).strftime("%Y-%m-%d")})
        elif act == "cdc_cc":
            jira.assign(key, CDC_CC_ASSIGNEE)
    except Exception as e:
        log.error("Jira action failed: %s", e); await q.edit_message_text(f"❌ Jira: {e}"); return
    remember(key, act); await q.edit_message_text(f"<b>{key}</b> → {act} ✅", ParseMode.HTML)

async def cmd_refresh(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_all(ctx, force=True)
    await upd.message.reply_text("Список тикетов обновлён ✅")

async def cmd_find(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает /find KEY ИЛИ простое сообщение с номером тикета."""
    # 1️⃣ достаём «сырой» ввод
    raw_input = (ctx.args[0] if ctx.args else upd.message.text).upper()
    raw = raw_input.replace("/FIND", "").strip()
    # 2️⃣ добавляем префикс, если нужно
    key = f"ISP-{raw}" if raw.isdigit() else raw
    # 3️⃣ пробуем получить тикет
    try:
        issue = jira.get(key)
    except Exception as e:
        await upd.message.reply_text(f"⛔ {e}")
        return
    # 4️⃣ Отправляем карточку
    await upd.message.reply_text(card(issue), ParseMode.HTML, reply_markup=kb(issue.key))

# ───── 7. Sending list ────────────────────────────────────────────────────
async def send_all(ctx: ContextTypes.DEFAULT_TYPE, force=False):
    sent: Set[str] = ctx.bot_data.setdefault("sent", set())
    if force:
        sent.clear()
    for issue in jira.search(JQL):
        if issue.key in sent: continue
        await ctx.bot.send_message(CHAT_ID, card(issue), ParseMode.HTML, reply_markup=kb(issue.key), disable_web_page_preview=True)
        sent.add(issue.key)

async def job_check(ctx: ContextTypes.DEFAULT_TYPE):
    await send_all(ctx)

# ───── 8. main() ─────────────────────────────────────────────────────────-
def main():
    if not all([TELEGRAM_TOKEN, JIRA_SERVER, CHAT_ID]):
        raise SystemExit("Заполните TELEGRAM_TOKEN, JIRA_SERVER, OPERATOR_CHAT_ID")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CallbackQueryHandler(button_cb))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^(?:ISP-)?\d+$"), cmd_find))
    app.job_queue.run_repeating(job_check, CHECK_INTERVAL, first=5)
    log.info("Bot started — polling…"); app.run_polling()

if __name__ == "__main__":
    main()