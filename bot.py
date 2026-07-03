"""Cafe Logistics Telegram Bot.

Tracks cafe ingredient inventory by writing presale/postsale weights to a
Google Sheet ("Cafe Logistics", tab "working"). Formulas in the sheet handle
usage calculations; the bot only writes Date (A), Time (B), Recorded By (C),
presale (cols D-L) and postsale (cols M-U) values.

Working tab layout (1-based columns):
  A Date | B Time | C Recorded By | D-L Presale (9) | M-U Postsale (9)
  | V-AD Usage (9, ARRAYFORMULA — bot never touches)

Phase 5 adds: multi-user "Recorded By" column, /start & /help, unauthorized
access alerts, /restock, daily expiry alerts, and a weekly summary report.
"""

import os
import sys
from datetime import datetime, time, timedelta

import gspread
import pytz
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --- Configuration ---------------------------------------------------------

INGREDIENTS = [
    "V60",
    "Espresso Beans",
    "Chocolate",
    "Vanilla",
    "Matcha",
    "Caramel",
    "Sugar",
    "Oatmilk",
    "Milk",
]

TIMEZONE = pytz.timezone("Asia/Singapore")

# Conversation states
WAITING_FOR_PRESALE = 1
WAITING_FOR_POSTSALE = 2
RESTOCK_SELECT = 10
RESTOCK_AMOUNT = 11
RESTOCK_EXPIRY = 12
RESTOCK_ANOTHER = 13

# Set of authorised Telegram user IDs, populated in main() from AUTHORIZED_USERS.
# None means "no restriction configured" -> allow all users.
AUTHORIZED_USERS = None

# Chat ID for admin alerts (unauthorized access, expiry, weekly summary).
# None means alerting is disabled (ADMIN_CHAT_ID unset in .env).
ADMIN_CHAT_ID = None

# In-memory rate-limit tracker for unauthorized-access alerts: user_id -> last
# alert datetime. Not persisted; resets on restart, which is fine.
_last_alert_time = {}

# Maps a status flag (from the main tab's Status column) to a display emoji.
STATUS_EMOJI = {
    "OK": "🟢",
    "LOW": "🔴",
    "OUT": "🔴",
    "EXPIRED": "⚠️",
}

HELP_TEXT = (
    "☕ Welcome to the Cafe Logistics Bot!\n\n"
    "I help track ingredient inventory for each shift. Here's what I can do:\n\n"
    "📋 /recordpresale — Record ingredient weights at the start of a shift\n"
    "📋 /recordpostsale — Record weights at the end of a shift\n"
    "📦 /status — View current stock levels\n"
    "📥 /restock — Log a delivery of new stock\n"
    "↩️ /undo — Undo the last entry\n"
    "🚫 /cancel — Cancel the current operation\n"
    "Start your shift with /recordpresale!"
)

# --- Google Sheets setup ---------------------------------------------------

gc = gspread.service_account(filename="credentials.json")
sh = gc.open("Cafe Logistics")
sheet = sh.worksheet("working")
main_sheet = sh.worksheet("main")
restock_sheet = sh.worksheet("restocks")


# --- Helpers ---------------------------------------------------------------


def today_str() -> str:
    """Current date in Asia/Singapore as YYYY-MM-DD."""
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def now_time_str() -> str:
    """Current time in Asia/Singapore as HH:MM:SS."""
    return datetime.now(TIMEZONE).strftime("%H:%M:%S")


def is_authorised(user_id: int) -> bool:
    """Return True if the user may use the bot.

    When AUTHORIZED_USERS is None (unset/empty in .env) everyone is allowed, so
    a forgotten config doesn't lock out all staff. Otherwise only listed IDs.
    """
    return AUTHORIZED_USERS is None or user_id in AUTHORIZED_USERS


async def deny_and_alert(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
    """Reply with the standard refusal and notify the admin (rate-limited)."""
    await update.message.reply_text("🚫 You are not authorised to use this bot.")
    await send_unauthorized_alert(update, context, command)


async def send_unauthorized_alert(
    update: Update, context: ContextTypes.DEFAULT_TYPE, command: str
) -> None:
    """Send an admin alert about an unauthorized attempt, at most once/hour/user."""
    if ADMIN_CHAT_ID is None:
        return

    user = update.effective_user
    now = datetime.now(TIMEZONE)

    last = _last_alert_time.get(user.id)
    if last is not None and (now - last).total_seconds() < 3600:
        return
    _last_alert_time[user.id] = now

    first_name = user.first_name or "N/A"
    last_name = user.last_name or "N/A"
    username = f"@{user.username}" if user.username else "N/A"

    text = (
        "🚨 Unauthorized Access Attempt\n\n"
        f"User: {first_name} {last_name}\n"
        f"Username: {username}\n"
        f"User ID: {user.id}\n"
        f"Command: {command}\n"
        f"Time: {now.strftime('%Y-%m-%d %H:%M:%S')} SGT"
    )
    try:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except Exception as exc:  # noqa: BLE001 - alerting must never crash a command
        print(f"WARNING: failed to send unauthorized alert: {exc}", file=sys.stderr)


def parse_non_negative_number(text: str):
    """Return a non-negative number (int/float) parsed from text, or None.

    Accepts ints and floats that are >= 0 (so 0 and 0.0 are valid).
    Rejects negative numbers and non-numeric text.
    """
    try:
        value = float(text.strip())
    except (ValueError, AttributeError):
        return None
    if value < 0:
        return None
    return value


def parse_positive_number(text: str):
    """Return a strictly positive number (> 0) parsed from text, or None."""
    value = parse_non_negative_number(text)
    if value is None or value == 0:
        return None
    return value


def parse_sheet_number(text):
    """Parse a numeric cell value (possibly with commas) to float, or None."""
    if text is None:
        return None
    cleaned = str(text).strip().replace(",", "")
    if cleaned == "":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_expiry_date(text):
    """Flexibly parse a date string to a date object, or None if unparseable.

    Tries DD/MM/YYYY first (as entered via /restock), then YYYY-MM-DD, then a
    handful of other common formats.
    """
    if text is None:
        return None
    cleaned = str(text).strip()
    if cleaned == "":
        return None
    formats = [
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%m/%d/%Y",
        "%Y/%m/%d",
        "%d/%m/%y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def fmt_number(value) -> str:
    """Format a number with thousands separators; drop the decimal if integral."""
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.1f}"


def find_open_presale_row(today: str):
    """Return the 1-based row number of today's open presale row, or None.

    An "open" row is one where column A (Date) matches ``today`` and column M
    (V60 Postsale, 0-based index 12) is still empty. Queries the sheet
    directly so it works even after a bot restart wiped in-memory state.
    """
    records = sheet.get_all_values()
    for i, record in enumerate(records, start=1):
        date_cell = record[0] if len(record) > 0 else ""
        v60_postsale = record[12] if len(record) > 12 else ""
        if date_cell == today and (v60_postsale is None or v60_postsale == ""):
            return i
    return None


def find_unclosed_shift_date():
    """Return the date of any unclosed shift row, or None.

    An "unclosed" row is one where column D (V60 Presale, 0-based index 3) is
    NOT empty but column M (V60 Postsale, 0-based index 12) is still empty,
    meaning presale was recorded but postsale was never filled in. Scans ALL
    rows regardless of date, and queries the sheet directly so it works even
    after a bot restart wiped in-memory state.
    """
    records = sheet.get_all_values()
    for record in records:
        v60_presale = record[3] if len(record) > 3 else ""
        v60_postsale = record[12] if len(record) > 12 else ""
        presale_filled = v60_presale is not None and v60_presale != ""
        postsale_empty = v60_postsale is None or v60_postsale == ""
        if presale_filled and postsale_empty:
            date_cell = record[0] if len(record) > 0 else ""
            return date_cell
    return None


# --- Start / Help ----------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point shown to any user (authorised or not)."""
    await update.message.reply_text(HELP_TEXT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Same message as /start; accessible to all users."""
    await update.message.reply_text(HELP_TEXT)


# --- Presale flow ----------------------------------------------------------


async def recordpresale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorised(update.effective_user.id):
        await deny_and_alert(update, context, "/recordpresale")
        return ConversationHandler.END

    # Block starting a new presale while a previous shift is still open. Query
    # the sheet directly (not context.user_data) so this works across all dates
    # and even after a bot restart.
    try:
        unclosed_date = find_unclosed_shift_date()
    except Exception as exc:  # noqa: BLE001 - surface any API error to user
        await update.message.reply_text(
            f"⚠️ Failed to read the sheet: {exc}\nPlease try again."
        )
        return ConversationHandler.END

    if unclosed_date is not None:
        await update.message.reply_text(
            f"❌ There is an open shift from {unclosed_date} that hasn't been "
            "closed yet.\nPlease run /recordpostsale first to close it before "
            "starting a new presale."
        )
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["date"] = today_str()
    context.user_data["ingredient_index"] = 0
    context.user_data["values"] = []

    await update.message.reply_text(
        f"📋 Recording presale for {context.user_data['date']}.\n\n"
        f"Enter the amount (g) for {INGREDIENTS[0]}:"
    )
    return WAITING_FOR_PRESALE


async def presale_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    index = context.user_data["ingredient_index"]
    ingredient = INGREDIENTS[index]

    amount = parse_non_negative_number(update.message.text)
    if amount is None:
        await update.message.reply_text(
            f'❌ "{update.message.text}" is not a valid number. '
            f"Please enter the amount for {ingredient}:"
        )
        return WAITING_FOR_PRESALE

    context.user_data["values"].append(amount)
    await update.message.reply_text(f"✓ {ingredient} recorded: {amount}g")

    index += 1
    context.user_data["ingredient_index"] = index

    if index < len(INGREDIENTS):
        await update.message.reply_text(
            f"Enter the amount (g) for {INGREDIENTS[index]}:"
        )
        return WAITING_FOR_PRESALE

    # All 9 collected -> write presale row immediately.
    # Layout: [date, time, recorded_by, 9 presale, 9 blank postsale] = 21 values (A:U).
    presale_values = context.user_data["values"]
    first_name = update.effective_user.first_name or "N/A"
    row = (
        [context.user_data["date"], now_time_str(), first_name]
        + presale_values
        + [""] * 9
    )

    try:
        sheet.append_row(row, table_range="A:U")
    except Exception as exc:  # noqa: BLE001 - surface any API error to user
        await update.message.reply_text(
            f"⚠️ Failed to save presale to sheet: {exc}\n"
            "Your data is kept — please try /recordpresale again."
        )
        return WAITING_FOR_PRESALE

    context.user_data.clear()
    await update.message.reply_text(
        "✅ Presale saved to sheet. Use /recordpostsale at end of shift."
    )
    return ConversationHandler.END


# --- Postsale flow ---------------------------------------------------------


async def recordpostsale(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorised(update.effective_user.id):
        await deny_and_alert(update, context, "/recordpostsale")
        return ConversationHandler.END

    today = today_str()

    # Verify an open presale row exists in the sheet BEFORE prompting. Query the
    # sheet directly (not context.user_data) so this works after a bot restart.
    try:
        open_row = find_open_presale_row(today)
    except Exception as exc:  # noqa: BLE001 - surface any API error to user
        await update.message.reply_text(
            f"⚠️ Failed to read the sheet: {exc}\nPlease try again."
        )
        return ConversationHandler.END

    if open_row is None:
        await update.message.reply_text(
            "❌ No open presale record found for today. "
            "Please run /recordpresale first."
        )
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["date"] = today
    context.user_data["ingredient_index"] = 0
    context.user_data["values"] = []

    await update.message.reply_text(
        f"📋 Recording postsale for {context.user_data['date']}.\n\n"
        f"Enter the amount (g) for {INGREDIENTS[0]}:"
    )
    return WAITING_FOR_POSTSALE


async def postsale_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    index = context.user_data["ingredient_index"]
    ingredient = INGREDIENTS[index]

    amount = parse_non_negative_number(update.message.text)
    if amount is None:
        await update.message.reply_text(
            f'❌ "{update.message.text}" is not a valid number. '
            f"Please enter the amount for {ingredient}:"
        )
        return WAITING_FOR_POSTSALE

    context.user_data["values"].append(amount)
    await update.message.reply_text(f"✓ {ingredient} recorded: {amount}g")

    index += 1
    context.user_data["ingredient_index"] = index

    if index < len(INGREDIENTS):
        await update.message.reply_text(
            f"Enter the amount (g) for {INGREDIENTS[index]}:"
        )
        return WAITING_FOR_POSTSALE

    # All 9 collected -> find the open presale row for today and update M:U.
    postsale_values = context.user_data["values"]
    today = context.user_data["date"]

    try:
        row_number = find_open_presale_row(today)
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(
            f"⚠️ Failed to read the sheet: {exc}\n"
            "Your data is kept — please try again."
        )
        return WAITING_FOR_POSTSALE

    if row_number is None:
        context.user_data.clear()
        await update.message.reply_text(
            "❌ No open presale record found for today. "
            "Please run /recordpresale first."
        )
        return ConversationHandler.END

    try:
        sheet.update(range_name=f"M{row_number}:U{row_number}", values=[postsale_values])
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(
            f"⚠️ Failed to save postsale to sheet: {exc}\n"
            "Your data is kept — please try again."
        )
        return WAITING_FOR_POSTSALE

    context.user_data.clear()
    await update.message.reply_text("✅ Postsale saved to sheet. Shift complete.")
    return ConversationHandler.END


# --- Restock flow ----------------------------------------------------------


def _restock_menu() -> str:
    lines = ["📥 Restock — Select an ingredient:", ""]
    for i, name in enumerate(INGREDIENTS, start=1):
        lines.append(f"{i}. {name}")
    lines.append("")
    lines.append("Enter the number:")
    return "\n".join(lines)


async def restock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorised(update.effective_user.id):
        await deny_and_alert(update, context, "/restock")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["first_name"] = update.effective_user.first_name or "N/A"
    context.user_data["restocks"] = []  # list of {name, amount, expiry}

    await update.message.reply_text(_restock_menu())
    return RESTOCK_SELECT


async def restock_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        choice = int(text)
    except ValueError:
        await update.message.reply_text(
            "❌ Please enter a valid number between 1 and 9:"
        )
        return RESTOCK_SELECT

    if not 1 <= choice <= len(INGREDIENTS):
        await update.message.reply_text(
            f"❌ Please enter a number between 1 and {len(INGREDIENTS)}:"
        )
        return RESTOCK_SELECT

    ingredient = INGREDIENTS[choice - 1]
    context.user_data["current_ingredient"] = ingredient
    await update.message.reply_text(
        f"Enter the restock amount for {ingredient}:"
    )
    return RESTOCK_AMOUNT


async def restock_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ingredient = context.user_data["current_ingredient"]
    amount = parse_positive_number(update.message.text)
    if amount is None:
        await update.message.reply_text(
            f"❌ Amount must be a positive number greater than 0. "
            f"Enter the restock amount for {ingredient}:"
        )
        return RESTOCK_AMOUNT

    context.user_data["current_amount"] = amount
    await update.message.reply_text(
        f'Enter the new expiry date for {ingredient} (DD/MM/YYYY) '
        'or type "skip" to keep the current date:'
    )
    return RESTOCK_EXPIRY


async def restock_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    ingredient = context.user_data["current_ingredient"]

    if text.lower() == "skip":
        expiry = None
    else:
        # Only accept the DD/MM/YYYY format prompted for.
        try:
            parsed = datetime.strptime(text, "%d/%m/%Y").date()
        except ValueError:
            parsed = None
        if parsed is None:
            await update.message.reply_text(
                f'❌ "{text}" is not a valid date. Please enter DD/MM/YYYY '
                'or type "skip":'
            )
            return RESTOCK_EXPIRY
        expiry = parsed.strftime("%d/%m/%Y")

    context.user_data["restocks"].append(
        {
            "name": ingredient,
            "amount": context.user_data["current_amount"],
            "expiry": expiry,
        }
    )

    await update.message.reply_text("Restock another ingredient? (yes/no)")
    return RESTOCK_ANOTHER


async def restock_another(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answer = update.message.text.strip().lower()
    if answer in ("yes", "y"):
        await update.message.reply_text(_restock_menu())
        return RESTOCK_SELECT
    if answer not in ("no", "n"):
        await update.message.reply_text('❌ Please answer "yes" or "no":')
        return RESTOCK_ANOTHER

    # Finalise: write restock rows and update the main tab.
    restocks = context.user_data.get("restocks", [])
    if not restocks:
        context.user_data.clear()
        await update.message.reply_text("Nothing was restocked.")
        return ConversationHandler.END

    date = today_str()
    time_str = now_time_str()
    first_name = context.user_data["first_name"]

    try:
        main_records = main_sheet.get_all_values()
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(
            f"⚠️ Failed to read the sheet: {exc}\nPlease try again."
        )
        return ConversationHandler.END

    # Build lookups from the main tab (A Ingredient, B Unit, C Initial Stock).
    row_of = {}
    unit_of = {}
    stock_of = {}
    for i, record in enumerate(main_records, start=1):
        name = record[0].strip() if len(record) > 0 and record[0] else ""
        if not name:
            continue
        row_of[name] = i
        unit_of[name] = record[1] if len(record) > 1 else ""
        stock_of[name] = parse_sheet_number(record[2] if len(record) > 2 else "") or 0.0

    summary_lines = ["✅ Restock complete!", ""]
    try:
        for item in restocks:
            name = item["name"]
            amount = item["amount"]
            expiry = item["expiry"]

            # Log the delivery in the restocks tab.
            restock_sheet.append_row([date, time_str, name, amount, first_name])

            row_number = row_of.get(name)
            unit = unit_of.get(name, "")

            if row_number is None:
                # Ingredient missing from main tab — record what we can.
                summary_lines.append(
                    f"{name}: +{fmt_number(amount)}{unit} "
                    "(⚠️ not found in main tab, stock not updated)"
                )
                continue

            new_total = stock_of.get(name, 0.0) + amount
            stock_of[name] = new_total  # keep running total for repeats
            main_sheet.update_cell(row_number, 3, new_total)  # Column C

            expiry_note = ""
            if expiry is not None:
                main_sheet.update_cell(row_number, 6, expiry)  # Column F
                expiry_note = f", expiry updated to {expiry}"

            summary_lines.append(
                f"{name}: +{fmt_number(amount)}{unit} "
                f"(new total: {fmt_number(new_total)}{unit}{expiry_note})"
            )
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(
            f"⚠️ Failed while saving restock to the sheet: {exc}\n"
            "Some entries may have been saved — please check the sheet."
        )
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text("\n".join(summary_lines))
    return ConversationHandler.END


# --- Cancel ----------------------------------------------------------------


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorised(update.effective_user.id):
        await deny_and_alert(update, context, "/cancel")
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text("Cancelled. Nothing was saved.")
    return ConversationHandler.END


# --- Status (read-only) ----------------------------------------------------


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Read the 'main' tab and reply with current stock per ingredient."""
    if not is_authorised(update.effective_user.id):
        await deny_and_alert(update, context, "/status")
        return

    try:
        records = main_sheet.get_all_values()
    except Exception as exc:  # noqa: BLE001 - surface any API error to user
        await update.message.reply_text(
            f"⚠️ Failed to read the sheet: {exc}\nPlease try again."
        )
        return

    # main tab: A Ingredient, B Unit, C Initial Stock, D Total Usage,
    # E Current Stock, F Expiry Date, G Status. Data lives in rows 2-10.
    lines = ["📦 Current Inventory", ""]
    for record in records[1:10]:
        ingredient = record[0] if len(record) > 0 else ""
        if not ingredient:
            continue
        unit = record[1] if len(record) > 1 else ""
        current_stock = record[4] if len(record) > 4 else ""
        status_flag = record[6] if len(record) > 6 else ""
        emoji = STATUS_EMOJI.get(status_flag.strip().upper(), "")
        emoji_part = f" {emoji}" if emoji else ""
        status_part = f" {status_flag}" if status_flag else ""
        lines.append(f"{ingredient}: {current_stock}{unit}{emoji_part}{status_part}")

    await update.message.reply_text("\n".join(lines))


# --- Undo ------------------------------------------------------------------


def find_last_data_row(records) -> int:
    """Return the 1-based number of the bottom-most non-empty data row, or 0.

    Row 1 is the header, so only rows >= 2 count. A row is "data" if any cell
    in it is non-empty.
    """
    for i in range(len(records), 1, -1):
        record = records[i - 1]
        if any(cell not in (None, "") for cell in record):
            return i
    return 0


async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Undo the last entry: clear postsale if closed, else delete the row."""
    if not is_authorised(update.effective_user.id):
        await deny_and_alert(update, context, "/undo")
        return

    try:
        records = sheet.get_all_values()
    except Exception as exc:  # noqa: BLE001 - surface any API error to user
        await update.message.reply_text(
            f"⚠️ Failed to read the sheet: {exc}\nPlease try again."
        )
        return

    row_number = find_last_data_row(records)
    if row_number == 0:
        await update.message.reply_text("❌ Nothing to undo.")
        return

    record = records[row_number - 1]
    date_cell = record[0] if len(record) > 0 else ""
    time_cell = record[1] if len(record) > 1 else ""
    v60_postsale = record[12] if len(record) > 12 else ""
    postsale_filled = v60_postsale not in (None, "")

    try:
        if postsale_filled:
            # Shift fully closed -> clear only the postsale columns (M:U).
            sheet.update(range_name=f"M{row_number}:U{row_number}", values=[[""] * 9])
        else:
            # Only presale exists -> remove the whole row.
            sheet.delete_rows(row_number)
    except Exception as exc:  # noqa: BLE001 - surface any API error to user
        await update.message.reply_text(
            f"⚠️ Failed to undo on the sheet: {exc}\nPlease try again."
        )
        return

    if postsale_filled:
        await update.message.reply_text(
            f"↩️ Postsale cleared for {date_cell} {time_cell}. "
            "You can re-enter with /recordpostsale."
        )
    else:
        await update.message.reply_text(
            f"↩️ Presale entry from {date_cell} {time_cell} deleted. "
            "You can re-enter with /recordpresale."
        )


# --- Scheduled jobs --------------------------------------------------------


async def expiry_check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily 7AM SGT: alert admin about expired / soon-to-expire ingredients."""
    if ADMIN_CHAT_ID is None:
        return

    try:
        records = main_sheet.get_all_values()
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: expiry check failed to read sheet: {exc}", file=sys.stderr)
        return

    today = datetime.now(TIMEZONE).date()
    expired = []       # (name, date)
    expiring_soon = [] # (name, date, days)

    for record in records[1:]:
        ingredient = record[0].strip() if len(record) > 0 and record[0] else ""
        if not ingredient:
            continue
        expiry_raw = record[5] if len(record) > 5 else ""  # Column F
        if not expiry_raw or not str(expiry_raw).strip():
            continue
        expiry_date = parse_expiry_date(expiry_raw)
        if expiry_date is None:
            print(
                f"WARNING: could not parse expiry '{expiry_raw}' for {ingredient}",
                file=sys.stderr,
            )
            continue

        days_left = (expiry_date - today).days
        if days_left < 0:
            expired.append((ingredient, expiry_date))
        elif days_left <= 3:
            expiring_soon.append((ingredient, expiry_date, days_left))

    if not expired and not expiring_soon:
        return  # nothing to report — don't spam an "all clear" message

    lines = [f"⚠️ Expiry Alert — {today.strftime('%d/%m/%Y')}", ""]
    if expired:
        lines.append("🔴 EXPIRED:")
        for name, d in expired:
            lines.append(f"  {name} — expired {d.strftime('%d/%m/%Y')}")
        lines.append("")
    if expiring_soon:
        lines.append("🟡 EXPIRING SOON:")
        for name, d, days_left in expiring_soon:
            word = "day" if days_left == 1 else "days"
            lines.append(
                f"  {name} — expires {d.strftime('%d/%m/%Y')} ({days_left} {word})"
            )

    text = "\n".join(lines).rstrip()
    try:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: failed to send expiry alert: {exc}", file=sys.stderr)


async def weekly_summary_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Monday 7AM SGT: send the admin a usage summary for the previous week."""
    if ADMIN_CHAT_ID is None:
        return

    today = datetime.now(TIMEZONE).date()
    this_monday = today - timedelta(days=today.weekday())  # Monday of current week
    last_monday = this_monday - timedelta(days=7)
    last_sunday = this_monday - timedelta(days=1)

    range_str = (
        f"{last_monday.strftime('%d/%m/%Y')} to {last_sunday.strftime('%d/%m/%Y')}"
    )

    try:
        records = sheet.get_all_values()
        main_records = main_sheet.get_all_values()
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: weekly summary failed to read sheet: {exc}", file=sys.stderr)
        return

    # Units come from the main tab (A Ingredient, B Unit).
    unit_of = {}
    for record in main_records[1:]:
        name = record[0].strip() if len(record) > 0 and record[0] else ""
        if name:
            unit_of[name] = record[1] if len(record) > 1 else ""

    totals = [0.0] * len(INGREDIENTS)
    shift_counts = [0] * len(INGREDIENTS)
    total_shifts = 0

    for record in records[1:]:
        date_cell = record[0] if len(record) > 0 else ""
        try:
            row_date = datetime.strptime(str(date_cell).strip(), "%Y-%m-%d").date()
        except (ValueError, AttributeError):
            row_date = parse_expiry_date(date_cell)
        if row_date is None or not (last_monday <= row_date <= last_sunday):
            continue

        row_has_usage = False
        for j in range(len(INGREDIENTS)):
            idx = 21 + j  # usage columns V-AD, 0-based indices 21-29
            cell = record[idx] if len(record) > idx else ""
            value = parse_sheet_number(cell)
            if value is not None and value != 0:
                totals[j] += value
                shift_counts[j] += 1
                row_has_usage = True
        if row_has_usage:
            total_shifts += 1

    if total_shifts == 0:
        text = (
            f"📊 Weekly Summary — {range_str}\n\n"
            "No shift records found for this period."
        )
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: failed to send weekly summary: {exc}", file=sys.stderr)
        return

    table = [f"{'Ingredient':<18}{'Used':<10}Shifts"]
    for j, name in enumerate(INGREDIENTS):
        unit = unit_of.get(name, "")
        used = f"{fmt_number(totals[j])}{unit}"
        table.append(f"{name:<18}{used:<10}{shift_counts[j]}")

    text = (
        f"📊 Weekly Summary — {range_str}\n\n"
        "```\n" + "\n".join(table) + "\n```\n\n"
        f"Total shifts recorded: {total_shifts}"
    )
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID, text=text, parse_mode="Markdown"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: failed to send weekly summary: {exc}", file=sys.stderr)


# --- Main ------------------------------------------------------------------


def main() -> None:
    global AUTHORIZED_USERS, ADMIN_CHAT_ID

    load_dotenv()
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        print("ERROR: TELEGRAM_TOKEN is missing or empty in .env", file=sys.stderr)
        sys.exit(1)

    # Parse authorised Telegram user IDs. Empty/unset -> None (allow everyone).
    raw_ids = os.getenv("AUTHORIZED_USERS", "")
    ids = {int(part) for part in raw_ids.split(",") if part.strip()}
    AUTHORIZED_USERS = ids if ids else None
    if AUTHORIZED_USERS is None:
        print("WARNING: AUTHORIZED_USERS is empty — all users are allowed.")

    # Admin chat ID for alerts / scheduled reports. Optional.
    raw_admin = os.getenv("ADMIN_CHAT_ID", "").strip()
    if raw_admin:
        try:
            ADMIN_CHAT_ID = int(raw_admin)
        except ValueError:
            print(
                f"WARNING: ADMIN_CHAT_ID '{raw_admin}' is not a valid integer — "
                "admin alerts disabled.",
                file=sys.stderr,
            )
            ADMIN_CHAT_ID = None
    else:
        print("WARNING: ADMIN_CHAT_ID is not set — admin alerts disabled.")

    application = Application.builder().token(token).build()

    presale_conv = ConversationHandler(
        entry_points=[CommandHandler("recordpresale", recordpresale)],
        states={
            WAITING_FOR_PRESALE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, presale_input)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    postsale_conv = ConversationHandler(
        entry_points=[CommandHandler("recordpostsale", recordpostsale)],
        states={
            WAITING_FOR_POSTSALE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, postsale_input)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    restock_conv = ConversationHandler(
        entry_points=[CommandHandler("restock", restock)],
        states={
            RESTOCK_SELECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, restock_select)
            ],
            RESTOCK_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, restock_amount)
            ],
            RESTOCK_EXPIRY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, restock_expiry)
            ],
            RESTOCK_ANOTHER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, restock_another)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(presale_conv)
    application.add_handler(postsale_conv)
    application.add_handler(restock_conv)
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("undo", undo))

    # Scheduled jobs (all times Asia/Singapore).
    job_queue = application.job_queue
    alert_time = time(hour=7, minute=0, second=0, tzinfo=TIMEZONE)
    job_queue.run_daily(expiry_check_job, time=alert_time)
    job_queue.run_daily(weekly_summary_job, time=alert_time, days=(0,))  # Monday

    print("Cafe Logistics bot is running. Press Ctrl+C to stop.")
    application.run_polling()


if __name__ == "__main__":
    main()
