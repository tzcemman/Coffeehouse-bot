import os
import sys
from datetime import datetime, time, timedelta

import gspread
import pytz
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
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

# Unit each ingredient is measured in. Everything is weighed in grams except
# milk and oat milk, which are counted as cartons. Keep this matching the Unit
# column (B) of the Main tab, which /restock and /status read from.
UNITS = {
    "V60": "g",
    "Espresso Beans": "g",
    "Chocolate": "g",
    "Vanilla": "g",
    "Matcha": "g",
    "Caramel": "g",
    "Sugar": "g",
    "Oatmilk": "cartons",
    "Milk": "cartons",
}

# Ingredients that come in named varieties, recorded per delivery in the
# Restocks ledger. A set so more can be added without touching any logic.
VARIETAL_INGREDIENTS = {"V60"}

# How many days ahead the daily job warns about an upcoming expiry.
EXPIRY_WARNING_DAYS = 7

TIMEZONE = pytz.timezone("Asia/Singapore")

# Conversation states
WAITING_FOR_PRESALE = 1
WAITING_FOR_POSTSALE = 2
RESTOCK_SELECT = 10
RESTOCK_AMOUNT = 11
RESTOCK_VARIETY = 14
RESTOCK_EXPIRY = 12
RESTOCK_ANOTHER = 13
DISCARD_SELECT = 20
DISCARD_AMOUNT = 21

# Telegram user IDs per tier, populated in main() from the matching .env names.
# All three fail closed: an empty set grants nothing, so a missing or misspelt
# entry can never hand out access.
#
# The tiers cascade — ADMIN ⊃ EXCO ⊃ regular — so nobody needs listing twice:
#   ADMIN_USERS       everything EXCO can do, plus unauthorized-access alerts
#   EXCO_USERS        /restock, /discard, expiry alerts, weekly summary
#   AUTHORIZED_USERS  shift commands only
ADMIN_USERS = frozenset()
EXCO_USERS = frozenset()
AUTHORIZED_USERS = frozenset()

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

# Staff enter expiry dates day-first. Both separators are accepted so nobody is
# rejected for typing the "wrong" one; the value is written to the sheet as ISO
# regardless, so Google Sheets parses it the same way in any locale.
EXPIRY_INPUT_FORMATS = ("%d-%m-%Y", "%d/%m/%Y")
EXPIRY_FORMAT_HINT = "📅 Format: DD-MM-YYYY (e.g. 15-09-2026)"

# Built from shared pieces rather than two full literals so the common commands
# can't drift apart when one menu is edited.
_HELP_TOP = (
    "☕ Welcome to the Cafe Logistics Bot!\n\n"
    "I help track ingredient inventory for each shift. Here's what I can do:\n\n"
    "📋 /recordpresale — Record ingredient weights at the start of a shift\n"
    "📋 /recordpostsale — Record weights at the end of a shift\n"
    "📦 /status — View current stock levels\n"
)
_HELP_EXCO_ONLY = (
    "📥 /restock — Log a delivery of new stock\n"
    "🗑️ /discard — Write off expired or spoiled stock\n"
)
_HELP_BOTTOM = (
    "↩️ /undo — Undo the last entry\n"
    "🚫 /cancel — Cancel the current operation\n"
    "Start your shift with /recordpresale!"
)

HELP_TEXT_REGULAR = _HELP_TOP + _HELP_BOTTOM
HELP_TEXT_EXCO = _HELP_TOP + _HELP_EXCO_ONLY + _HELP_BOTTOM

# Shift shortcut buttons, shown to regular staff only. Telegram sends a reply
# keyboard button's LABEL as an ordinary text message — it does not invoke a
# command — so these strings are matched exactly to route to the shift flows.
# Both the keyboard and the matching filter read them from here: change a label
# in only one of the two places and the button silently stops working.
BTN_PRESALE = "Starting a shift? 👀"
BTN_POSTSALE = "Closing a shift? 🥹"
SHIFT_BUTTONS = (BTN_PRESALE, BTN_POSTSALE)

# Telegram command menus, per tier. Built from shared pieces so they cannot
# drift from HELP_TEXT_* above.
_CMDS_TOP = [
    BotCommand("recordpresale", "Record weights at the start of a shift"),
    BotCommand("recordpostsale", "Record weights at the end of a shift"),
    BotCommand("status", "View current stock levels"),
]
_CMDS_EXCO_ONLY = [
    BotCommand("restock", "Log a delivery of new stock"),
    BotCommand("discard", "Write off expired or spoiled stock"),
]
_CMDS_BOTTOM = [
    BotCommand("undo", "Undo the last entry"),
    BotCommand("cancel", "Cancel the current operation"),
    BotCommand("help", "Show what I can do"),
]

COMMANDS_REGULAR = _CMDS_TOP + _CMDS_BOTTOM
COMMANDS_EXCO = _CMDS_TOP + _CMDS_EXCO_ONLY + _CMDS_BOTTOM

# --- Google Sheets setup ---------------------------------------------------

gc = gspread.service_account(filename="credentials.json")
sh = gc.open("AY26/27 Logistics Tracker")
sheet = sh.worksheet("Working")
main_sheet = sh.worksheet("Main")
restock_sheet = sh.worksheet("Restocks")


# --- Helpers ---------------------------------------------------------------


def today_str() -> str:
    """Current date in Asia/Singapore as YYYY-MM-DD."""
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def now_time_str() -> str:
    """Current time in Asia/Singapore as HH:MM:SS."""
    return datetime.now(TIMEZONE).strftime("%H:%M:%S")


def is_admin(user_id: int) -> bool:
    """Return True if the user receives unauthorized-access alerts."""
    return user_id in ADMIN_USERS


def is_exco(user_id: int) -> bool:
    """Return True if the user has executive-committee powers.

    Admins are implicitly EXCO, so they need not appear in both lists.
    """
    return is_admin(user_id) or user_id in EXCO_USERS


def is_authorised(user_id: int) -> bool:
    """Return True if the user may use the bot at all, in any tier.

    The tiers cascade, so membership of a higher list is enough on its own.
    """
    return is_exco(user_id) or user_id in AUTHORIZED_USERS


def help_text_for(user_id: int) -> str:
    """Return the menu appropriate to the caller's tier.

    An unlisted user is given their own Telegram ID instead of a menu: a new
    staff member can then be onboarded without an EXCO member having to dig the
    ID out of an alert, and no operational detail leaks to a stranger.
    """
    if is_exco(user_id):
        return HELP_TEXT_EXCO
    if is_authorised(user_id):
        return HELP_TEXT_REGULAR
    return (
        "🚫 You are not authorised to use this bot.\n\n"
        f"Your Telegram user ID is: {user_id}\n"
        "Ask an EXCO member to add you."
    )


def keyboard_for(user_id: int):
    """Shift shortcut buttons for regular staff; a keyboard clear for everyone else.

    Returns ReplyKeyboardRemove rather than None for the other tiers on purpose:
    someone promoted out of the regular tier would otherwise keep buttons their
    tier no longer uses, since a reply keyboard persists until something
    replaces it.
    """
    if is_authorised(user_id) and not is_exco(user_id):
        return ReplyKeyboardMarkup(
            [[BTN_PRESALE], [BTN_POSTSALE]],  # one per row — bigger tap targets
            resize_keyboard=True,
            is_persistent=True,
        )
    return ReplyKeyboardRemove()


def commands_for(user_id: int):
    """The command menu appropriate to the caller's tier."""
    return COMMANDS_EXCO if is_exco(user_id) else COMMANDS_REGULAR


async def sync_commands_for(bot, user_id: int) -> None:
    """Set one user's command menu to match their tier. Never raises.

    Every user gets an explicit per-chat scope rather than relying on the
    default, so a tier change corrects itself — without this, someone demoted
    from EXCO would keep seeing /restock and /discard indefinitely.

    Telegram refuses this for anyone who has never messaged the bot, the same
    limitation broadcast_to_exco works around, so failures are logged and
    swallowed.
    """
    try:
        await bot.set_my_commands(
            commands_for(user_id), scope=BotCommandScopeChat(chat_id=user_id)
        )
    except Exception as exc:  # noqa: BLE001 - menus must never break a command
        print(
            f"WARNING: could not set command menu for {user_id}: {exc}",
            file=sys.stderr,
        )


async def sync_all_command_menus(application) -> None:
    """post_init hook: default menu, then a per-chat menu for every known user."""
    bot = application.bot
    try:
        await bot.set_my_commands(COMMANDS_REGULAR, scope=BotCommandScopeDefault())
    except Exception as exc:  # noqa: BLE001 - startup must not fail on this
        print(f"WARNING: could not set the default command menu: {exc}", file=sys.stderr)

    for user_id in ADMIN_USERS | EXCO_USERS | AUTHORIZED_USERS:
        await sync_commands_for(bot, user_id)


async def _broadcast(
    context: ContextTypes.DEFAULT_TYPE,
    recipients,
    audience: str,
    text: str,
    parse_mode=None,
) -> None:
    """Send a message to each recipient, one chat at a time.

    Failures are handled per recipient: Telegram refuses to let a bot message
    anyone who has never started a chat with it, and one unreachable person must
    not stop the rest from being told.
    """
    if not recipients:
        print(
            f"WARNING: no {audience} recipients configured — notification dropped.",
            file=sys.stderr,
        )
        return

    for user_id in recipients:
        try:
            await context.bot.send_message(
                chat_id=user_id, text=text, parse_mode=parse_mode
            )
        except Exception as exc:  # noqa: BLE001 - alerting must never crash a caller
            print(
                f"WARNING: failed to notify {audience} member {user_id}: {exc}",
                file=sys.stderr,
            )


async def broadcast_to_exco(
    context: ContextTypes.DEFAULT_TYPE, text: str, parse_mode=None
) -> None:
    """Operational notifications: expiry alerts and the weekly summary.

    Goes to admins as well as EXCO, since admins hold every EXCO power.
    """
    await _broadcast(context, EXCO_USERS | ADMIN_USERS, "EXCO", text, parse_mode)


async def broadcast_to_admin(
    context: ContextTypes.DEFAULT_TYPE, text: str, parse_mode=None
) -> None:
    """Security notifications: unauthorized access attempts, admins only.

    Kept separate from broadcast_to_exco so a stranger poking at the bot cannot
    generate noise for the whole committee.
    """
    await _broadcast(context, ADMIN_USERS, "admin", text, parse_mode)


async def deny_and_alert(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
    """Refuse an unlisted user and alert EXCO (rate-limited)."""
    await update.message.reply_text("🚫 You are not authorised to use this bot.")
    await send_unauthorized_alert(update, context, command)


async def deny_exco_only(update: Update, command: str) -> None:
    """Refuse a regular user a privileged command. Deliberately does not alert.

    An authorised staff member trying an EXCO command is not an intruder;
    broadcasting it would be noise and would mislabel a colleague. Logged to
    stderr so the attempt is still recoverable if it ever matters.
    """
    await update.message.reply_text(f"🔒 {command} is restricted to EXCO members.")
    print(
        f"INFO: user {update.effective_user.id} attempted EXCO command {command}",
        file=sys.stderr,
    )


async def send_unauthorized_alert(
    update: Update, context: ContextTypes.DEFAULT_TYPE, command: str
) -> None:
    """Alert the admins about an unauthorized attempt, at most once/hour/user."""
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
    await broadcast_to_admin(context, text)


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


# Unit symbols abut the number ("500g"); word units take a space ("3 cartons").
_SYMBOL_UNITS = {"g", "kg", "ml", "l", "L"}


def fmt_qty(value, unit) -> str:
    """Format a quantity with its unit, readably.

    Symbols abut the number, words are spaced, and a word unit is singularised
    at exactly one so it reads "1 carton" rather than "1 cartons".
    """
    number = fmt_number(value)
    unit = (unit or "").strip()
    if not unit:
        return number
    if unit in _SYMBOL_UNITS:
        return f"{number}{unit}"
    if value == 1 and unit.endswith("s"):
        unit = unit[:-1]
    return f"{number} {unit}"


def amount_prompt(ingredient: str, variety: str = "") -> str:
    """Prompt asking for one ingredient's amount, in that ingredient's own unit.

    Varietal ingredients carry the variety in use, so staff can see which beans
    the number they are about to type belongs to.
    """
    label = f"{ingredient} ({variety})" if variety else ingredient
    return f"Enter the amount ({UNITS.get(ingredient, '')}) for {label}:"


def variety_breakdown(restock_records, ingredient: str):
    """Return [(variety, remaining)] for one ingredient's live batches.

    Batches sharing a variety are summed, and blank varieties (older rows
    predating variety tracking) group under "unspecified".
    """
    totals = {}
    for _row, name, remaining, _expiry, variety in live_batches(restock_records):
        if name != ingredient:
            continue
        key = variety or "unspecified"
        totals[key] = totals.get(key, 0.0) + remaining
    return sorted(totals.items())


def stock_lines(main_records, restock_records=()):
    """Render current stock per ingredient, shared by /status and the summary.

    Main tab columns: A Ingredient, B Unit, E Current Stock, G Status. Data
    lives in rows 2-10. Varietal ingredients gain an indented per-variety
    breakdown beneath their pooled total.
    """
    lines = []
    for record in main_records[1:10]:
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

        if ingredient in VARIETAL_INGREDIENTS and restock_records:
            for variety, remaining in variety_breakdown(restock_records, ingredient):
                lines.append(f"   • {variety}: {fmt_qty(remaining, unit)}")
    return lines


def fetch_current_varieties() -> dict:
    """Resolve the variety in use per varietal ingredient, from the ledger.

    Never raises: a labelling failure must not stop a shift being recorded, so
    it degrades to unlabelled prompts.
    """
    try:
        return current_varieties(restock_sheet.get_all_values())
    except Exception as exc:  # noqa: BLE001 - labelling must not block a shift
        print(f"WARNING: could not resolve varieties: {exc}", file=sys.stderr)
        return {}


def current_varieties(restock_records) -> dict:
    """Map ingredient -> variety of its oldest live batch, for varietal items.

    "Oldest live" follows the same FIFO order the sheet uses to draw stock down,
    so the variety named is the one the ledger believes is being consumed.
    """
    found = {}
    for _row, name, _remaining, _expiry, variety in live_batches(restock_records):
        if name in VARIETAL_INGREDIENTS and name not in found and variety:
            found[name] = variety
    return found


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
    """Entry point. Shows the menu for the caller's tier."""
    await _menu_and_alert(update, context, "/start")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Same message as /start."""
    await _menu_and_alert(update, context, "/help")


async def _menu_and_alert(
    update: Update, context: ContextTypes.DEFAULT_TYPE, command: str
) -> None:
    """Reply with the caller's menu, alerting admins if they are unlisted.

    These two commands stay open to everyone so a prospective staff member can
    collect their own user ID. The attempt still reaches the admins, who alone
    receive security alerts — so a stranger finding the bot cannot generate
    noise for the wider committee.

    Alerts via send_unauthorized_alert rather than deny_and_alert because
    help_text_for() has already replied with the refusal; deny_and_alert would
    send a second one. Still rate-limited to once an hour per user, shared with
    every other command.
    """
    user_id = update.effective_user.id

    # Refresh the command menu here as well as at startup, so a user added
    # after the bot booted — or whose tier changed — is corrected on first
    # contact rather than waiting for a restart.
    await sync_commands_for(context.bot, user_id)

    await update.message.reply_text(
        help_text_for(user_id), reply_markup=keyboard_for(user_id)
    )
    if not is_authorised(user_id):
        await send_unauthorized_alert(update, context, command)


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

    context.user_data["varieties"] = fetch_current_varieties()

    await update.message.reply_text(
        f"📋 Recording presale for {context.user_data['date']}.\n\n"
        + amount_prompt(
            INGREDIENTS[0], context.user_data["varieties"].get(INGREDIENTS[0], "")
        )
    )
    return WAITING_FOR_PRESALE


async def presale_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    index = context.user_data["ingredient_index"]
    ingredient = INGREDIENTS[index]

    # The shift keyboard stays on screen mid-flow, so a stray tap arrives here
    # as text. Say what is actually happening rather than rejecting it as an
    # invalid number.
    if update.message.text in SHIFT_BUTTONS:
        await update.message.reply_text(
            "You're already recording a shift. Finish it, or /cancel to abandon "
            f"it.\n\n{amount_prompt(ingredient, context.user_data['varieties'].get(ingredient, ''))}"
        )
        return WAITING_FOR_PRESALE

    amount = parse_non_negative_number(update.message.text)
    if amount is None:
        await update.message.reply_text(
            f'❌ "{update.message.text}" is not a valid number. '
            f"Please enter the amount for {ingredient}:"
        )
        return WAITING_FOR_PRESALE

    context.user_data["values"].append(amount)
    await update.message.reply_text(
        f"✓ {ingredient} recorded: {fmt_qty(amount, UNITS.get(ingredient, ''))}"
    )

    index += 1
    context.user_data["ingredient_index"] = index

    if index < len(INGREDIENTS):
        await update.message.reply_text(
            amount_prompt(INGREDIENTS[index],
                          context.user_data["varieties"].get(INGREDIENTS[index], ""))
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

    context.user_data["varieties"] = fetch_current_varieties()

    await update.message.reply_text(
        f"📋 Recording postsale for {context.user_data['date']}.\n\n"
        + amount_prompt(
            INGREDIENTS[0], context.user_data["varieties"].get(INGREDIENTS[0], "")
        )
    )
    return WAITING_FOR_POSTSALE


async def postsale_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    index = context.user_data["ingredient_index"]
    ingredient = INGREDIENTS[index]

    # See presale_input — a stray keyboard tap mid-flow arrives here as text.
    if update.message.text in SHIFT_BUTTONS:
        await update.message.reply_text(
            "You're already recording a shift. Finish it, or /cancel to abandon "
            f"it.\n\n{amount_prompt(ingredient, context.user_data['varieties'].get(ingredient, ''))}"
        )
        return WAITING_FOR_POSTSALE

    amount = parse_non_negative_number(update.message.text)
    if amount is None:
        await update.message.reply_text(
            f'❌ "{update.message.text}" is not a valid number. '
            f"Please enter the amount for {ingredient}:"
        )
        return WAITING_FOR_POSTSALE

    context.user_data["values"].append(amount)
    await update.message.reply_text(
        f"✓ {ingredient} recorded: {fmt_qty(amount, UNITS.get(ingredient, ''))}"
    )

    index += 1
    context.user_data["ingredient_index"] = index

    if index < len(INGREDIENTS):
        await update.message.reply_text(
            amount_prompt(INGREDIENTS[index],
                          context.user_data["varieties"].get(INGREDIENTS[index], ""))
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
    if not is_exco(update.effective_user.id):
        await deny_exco_only(update, "/restock")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["first_name"] = update.effective_user.first_name or "N/A"
    context.user_data["restocks"] = []  # {name, amount, expiry, variety}

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

    if ingredient in VARIETAL_INGREDIENTS:
        return await ask_variety(update, context, ingredient)

    context.user_data["current_variety"] = ""
    await update.message.reply_text(
        f"Enter the restock amount for {ingredient} ({UNITS.get(ingredient, '')}):"
    )
    return RESTOCK_AMOUNT


def known_varieties(restock_records, ingredient: str):
    """Distinct variety names already used for this ingredient, in first-seen order.

    Offering these back as a menu is what keeps grouping reliable: free text
    would let "Ethiopian", "ethiopian" and "Yirg" become three separate
    varieties in every total, with nothing to flag that it happened.
    """
    seen = []
    for record in restock_records[1:]:
        name = record[2].strip() if len(record) > 2 and record[2] else ""
        variety = (record[6] or "").strip() if len(record) > 6 else ""
        if name == ingredient and variety and variety not in seen:
            seen.append(variety)
    return seen


async def ask_variety(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ingredient: str
) -> int:
    """Prompt for the variety, offering previously used names where there are any."""
    try:
        known = known_varieties(restock_sheet.get_all_values(), ingredient)
    except Exception as exc:  # noqa: BLE001 - fall back to free text
        print(f"WARNING: could not list past varieties: {exc}", file=sys.stderr)
        known = []

    context.user_data["known_varieties"] = known

    if not known:
        await update.message.reply_text(
            f"Which variety of {ingredient} is this? Type the name:"
        )
        return RESTOCK_VARIETY

    lines = [f"Which variety of {ingredient} is this?", ""]
    for i, variety in enumerate(known, start=1):
        lines.append(f"{i}. {variety}")
    lines.append(f"{len(known) + 1}. Something new")
    lines.append("")
    lines.append("Enter the number:")
    await update.message.reply_text("\n".join(lines))
    return RESTOCK_VARIETY


async def restock_variety(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    ingredient = context.user_data["current_ingredient"]
    known = context.user_data.get("known_varieties", [])

    if known:
        try:
            choice = int(text)
        except ValueError:
            await update.message.reply_text(
                f"❌ Please enter a number between 1 and {len(known) + 1}:"
            )
            return RESTOCK_VARIETY

        if not 1 <= choice <= len(known) + 1:
            await update.message.reply_text(
                f"❌ Please enter a number between 1 and {len(known) + 1}:"
            )
            return RESTOCK_VARIETY

        if choice <= len(known):
            context.user_data["current_variety"] = known[choice - 1]
            await update.message.reply_text(
                f"Enter the restock amount for {ingredient} "
                f"({UNITS.get(ingredient, '')}):"
            )
            return RESTOCK_AMOUNT

        # "Something new" -> ask for free text next time round.
        context.user_data["known_varieties"] = []
        await update.message.reply_text(
            f"Type the name of the new {ingredient} variety:"
        )
        return RESTOCK_VARIETY

    if not text:
        await update.message.reply_text("❌ Please type a variety name:")
        return RESTOCK_VARIETY

    context.user_data["current_variety"] = text
    await update.message.reply_text(
        f"Enter the restock amount for {ingredient} ({text}) "
        f"({UNITS.get(ingredient, '')}):"
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
        f"Enter the expiry date for this batch of {ingredient}.\n"
        f"{EXPIRY_FORMAT_HINT}\n"
        'Or type "skip" if it has no expiry date:'
    )
    return RESTOCK_EXPIRY


async def restock_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    ingredient = context.user_data["current_ingredient"]

    if text.lower() == "skip":
        expiry = None
    else:
        # Day-first only, either separator. Kept as a date object; written to the
        # sheet as ISO and shown back to staff day-first.
        expiry = None
        for fmt in EXPIRY_INPUT_FORMATS:
            try:
                expiry = datetime.strptime(text, fmt).date()
                break
            except ValueError:
                continue
        if expiry is None:
            await update.message.reply_text(
                f'❌ "{text}" is not a valid date.\n'
                f"{EXPIRY_FORMAT_HINT}\n"
                'Enter the expiry date, or type "skip":'
            )
            return RESTOCK_EXPIRY

    context.user_data["restocks"].append(
        {
            "name": ingredient,
            "amount": context.user_data["current_amount"],
            "expiry": expiry,
            "variety": context.user_data.get("current_variety", ""),
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

    # Finalise: append one batch row per restock. Formulas in restocks!H:I and on
    # the main tab derive stock levels and the next expiry date from these rows,
    # so the bot never writes totals or expiry dates to the main tab.
    restocks = context.user_data.get("restocks", [])
    if not restocks:
        context.user_data.clear()
        await update.message.reply_text("Nothing was restocked.")
        return ConversationHandler.END

    date = today_str()
    time_str = now_time_str()
    first_name = context.user_data["first_name"]

    try:
        for item in restocks:
            expiry = item["expiry"]
            restock_sheet.append_row(
                [
                    date,
                    time_str,
                    item["name"],
                    item["amount"],
                    expiry.strftime("%Y-%m-%d") if expiry is not None else "",
                    first_name,
                    item.get("variety", ""),
                ],
                # A:H keeps the I:J formulas from pushing appends past the data;
                # USER_ENTERED makes Sheets store the expiry as a real date.
                table_range="A:H",
                value_input_option="USER_ENTERED",
            )
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(
            f"⚠️ Failed while saving restock to the sheet: {exc}\n"
            "Some entries may have been saved — please check the sheet."
        )
        context.user_data.clear()
        return ConversationHandler.END

    # Read main AFTER the appends so the summary shows recalculated totals.
    try:
        main_records = main_sheet.get_all_values()
    except Exception as exc:  # noqa: BLE001
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Restock saved, but the summary could not be read back: {exc}\n"
            "Use /status to check current levels."
        )
        return ConversationHandler.END

    # main tab lookups: A Ingredient, B Unit, E Current Stock.
    unit_of = {}
    current_of = {}
    for record in main_records:
        name = record[0].strip() if len(record) > 0 and record[0] else ""
        if not name:
            continue
        unit_of[name] = record[1] if len(record) > 1 else ""
        current_of[name] = parse_sheet_number(record[4] if len(record) > 4 else "")

    summary_lines = ["✅ Restock complete!", ""]
    for item in restocks:
        name = item["name"]
        amount = item["amount"]
        expiry = item["expiry"]

        variety = item.get("variety", "")
        label = f"{name} ({variety})" if variety else name

        if name not in unit_of:
            # Batch row was still logged; only the main-tab rollup is missing.
            summary_lines.append(
                f"{label}: +{fmt_number(amount)} "
                "(⚠️ not found in main tab, stock not tracked)"
            )
            continue

        unit = unit_of.get(name, "")
        current = current_of.get(name)
        total_note = (
            f", now {fmt_qty(current, unit)}" if current is not None else ""
        )
        expiry_note = (
            f", expires {expiry.strftime('%d/%m/%Y')}"
            if expiry is not None
            else ", no expiry date"
        )
        summary_lines.append(
            f"{label}: +{fmt_qty(amount, unit)}{total_note}{expiry_note}"
        )

    context.user_data.clear()
    await update.message.reply_text("\n".join(summary_lines))
    return ConversationHandler.END


# --- Discard flow ----------------------------------------------------------


def live_batches(records):
    """Return [(row, ingredient, remaining, expiry_raw, variety)] for live batches.

    A batch is "live" when its Remaining (column J, 0-based index 9 — a formula
    on the Restocks tab) is greater than 0. Row 1 is the header. Rows come back
    in sheet order, which is the FIFO order the waterfall draws stock down in.
    """
    batches = []
    for i, record in enumerate(records, start=1):
        if i == 1:
            continue
        name = record[2].strip() if len(record) > 2 and record[2] else ""
        if not name:
            continue
        remaining = parse_sheet_number(record[9] if len(record) > 9 else "")
        if remaining is None or remaining <= 0:
            continue
        batches.append((
            i,
            name,
            remaining,
            record[4] if len(record) > 4 else "",   # E Expiry Date
            (record[6] or "").strip() if len(record) > 6 else "",  # G Variety
        ))
    return batches


def _discard_menu(batches) -> str:
    today = datetime.now(TIMEZONE).date()
    lines = ["🗑️ Discard — Select a batch to write off:", ""]
    for n, (_row, name, remaining, expiry_raw, variety) in enumerate(batches, start=1):
        expiry_date = parse_expiry_date(expiry_raw)
        if expiry_date is None:
            note = "no expiry date"
        elif expiry_date < today:
            note = f"⚠️ EXPIRED {expiry_date.strftime('%d/%m/%Y')}"
        else:
            note = f"expires {expiry_date.strftime('%d/%m/%Y')}"
        label = f"{name} ({variety})" if variety else name
        amount = fmt_qty(remaining, UNITS.get(name, ""))
        lines.append(f"{n}. {label} — {amount} left, {note}")
    lines.append("")
    lines.append("Enter the number:")
    return "\n".join(lines)


async def discard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorised(update.effective_user.id):
        await deny_and_alert(update, context, "/discard")
        return ConversationHandler.END
    if not is_exco(update.effective_user.id):
        await deny_exco_only(update, "/discard")
        return ConversationHandler.END

    try:
        records = restock_sheet.get_all_values()
    except Exception as exc:  # noqa: BLE001 - surface any API error to user
        await update.message.reply_text(
            f"⚠️ Failed to read the sheet: {exc}\nPlease try again."
        )
        return ConversationHandler.END

    batches = live_batches(records)
    if not batches:
        await update.message.reply_text(
            "❌ There are no batches with stock left to write off."
        )
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["batches"] = batches

    await update.message.reply_text(_discard_menu(batches))
    return DISCARD_SELECT


async def discard_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    batches = context.user_data["batches"]
    try:
        choice = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(
            f"❌ Please enter a valid number between 1 and {len(batches)}:"
        )
        return DISCARD_SELECT

    if not 1 <= choice <= len(batches):
        await update.message.reply_text(
            f"❌ Please enter a number between 1 and {len(batches)}:"
        )
        return DISCARD_SELECT

    row_number, name, remaining, _expiry, variety = batches[choice - 1]
    context.user_data["row_number"] = row_number
    context.user_data["name"] = name
    context.user_data["remaining"] = remaining

    label = f"{name} ({variety})" if variety else name
    await update.message.reply_text(
        f"{label} — {fmt_qty(remaining, UNITS.get(name, ''))} left in this batch.\n"
        'Enter the amount to write off, or type "all":'
    )
    return DISCARD_AMOUNT


async def discard_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = context.user_data["name"]
    remaining = context.user_data["remaining"]
    row_number = context.user_data["row_number"]

    text = update.message.text.strip()
    if text.lower() == "all":
        amount = remaining
    else:
        amount = parse_positive_number(text)
        if amount is None:
            await update.message.reply_text(
                "❌ Amount must be a positive number greater than 0. "
                f'Enter the amount to write off for {name}, or type "all":'
            )
            return DISCARD_AMOUNT
        if amount > remaining:
            await update.message.reply_text(
                f"❌ Only {fmt_qty(remaining, UNITS.get(name, ''))} left in this batch. "
                'Enter a smaller amount, or type "all":'
            )
            return DISCARD_AMOUNT

    # Written Off (column H) is cumulative — add to whatever is already there.
    try:
        existing = parse_sheet_number(restock_sheet.cell(row_number, 8).value) or 0.0
        restock_sheet.update_cell(row_number, 8, existing + amount)
    except Exception as exc:  # noqa: BLE001 - surface any API error to user
        await update.message.reply_text(
            f"⚠️ Failed to save the write-off to the sheet: {exc}\n"
            "Your data is kept — please try again."
        )
        return DISCARD_AMOUNT

    context.user_data.clear()
    await update.message.reply_text(
        f"🗑️ Wrote off {fmt_qty(amount, UNITS.get(name, ''))} of {name}. "
        f"{fmt_qty(remaining - amount, UNITS.get(name, ''))} left in that batch."
    )
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

    try:
        restock_records = restock_sheet.get_all_values()
    except Exception as exc:  # noqa: BLE001 - breakdown is a nicety, not essential
        print(f"WARNING: /status could not read the ledger: {exc}", file=sys.stderr)
        restock_records = []

    lines = ["📦 Current Inventory", ""] + stock_lines(records, restock_records)
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
    recorded_by = record[2].strip() if len(record) > 2 and record[2] else ""
    v60_postsale = record[12] if len(record) > 12 else ""
    postsale_filled = v60_postsale not in (None, "")

    # Regular users may only undo their own entry; EXCO may undo anything. A
    # blank "Recorded By" (hand-entered or backfilled rows) fails closed.
    if not is_exco(update.effective_user.id):
        caller = (update.effective_user.first_name or "").strip()
        if not recorded_by or recorded_by != caller:
            owner = recorded_by or "someone else"
            await update.message.reply_text(
                f"🔒 That entry was recorded by {owner}. "
                "Only they or an EXCO member can undo it."
            )
            return

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
    """Daily 7AM SGT: alert EXCO about expired / soon-to-expire ingredients."""
    try:
        records = main_sheet.get_all_values()
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: expiry check failed to read sheet: {exc}", file=sys.stderr)
        return

    # Used only to name the variety behind a varietal ingredient's expiry date.
    try:
        varieties = current_varieties(restock_sheet.get_all_values())
    except Exception as exc:  # noqa: BLE001 - naming is a nicety, not essential
        print(f"WARNING: expiry check could not read the ledger: {exc}", file=sys.stderr)
        varieties = {}

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
        elif days_left <= EXPIRY_WARNING_DAYS:
            expiring_soon.append((ingredient, expiry_date, days_left))

    if not expired and not expiring_soon:
        return  # nothing to report — don't spam an "all clear" message

    def label(name):
        """Name the variety behind the date, so 'V60' says which beans."""
        variety = varieties.get(name)
        return f"{name} ({variety})" if variety else name

    lines = [f"⚠️ Expiry Alert — {today.strftime('%d/%m/%Y')}", ""]
    if expired:
        lines.append("🔴 EXPIRED:")
        for name, d in expired:
            lines.append(f"  {label(name)} — expired {d.strftime('%d/%m/%Y')}")
        lines.append("")
    if expiring_soon:
        lines.append("🟡 EXPIRING SOON:")
        for name, d, days_left in expiring_soon:
            word = "day" if days_left == 1 else "days"
            lines.append(
                f"  {label(name)} — expires {d.strftime('%d/%m/%Y')} "
                f"({days_left} {word})"
            )

    await broadcast_to_exco(context, "\n".join(lines).rstrip())


async def weekly_summary_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Friday 9PM SGT: send EXCO the week's usage plus current stock levels.

    The window is a rolling seven days ending today, so consecutive reports are
    contiguous — every trading day appears in exactly one summary, with no gap
    and no double-counting.
    """
    end = datetime.now(TIMEZONE).date()
    start = end - timedelta(days=6)

    range_str = f"{start.strftime('%d/%m/%Y')} to {end.strftime('%d/%m/%Y')}"

    try:
        records = sheet.get_all_values()
        main_records = main_sheet.get_all_values()
        restock_records = restock_sheet.get_all_values()
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: weekly summary failed to read sheet: {exc}", file=sys.stderr)
        return

    stock_section = "\n".join(["📦 Current Stock", ""] + stock_lines(
        main_records, restock_records))

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
        if row_date is None or not (start <= row_date <= end):
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
            "No shift records found for this period.\n\n"
            f"{stock_section}"
        )
        await broadcast_to_exco(context, text)
        return

    table = [f"{'Ingredient':<18}{'Used':<12}Shifts"]
    for j, name in enumerate(INGREDIENTS):
        used = fmt_qty(totals[j], unit_of.get(name, ""))
        table.append(f"{name:<18}{used:<12}{shift_counts[j]}")

    text = (
        f"📊 Weekly Summary — {range_str}\n\n"
        "```\n" + "\n".join(table) + "\n```\n"
        f"Total shifts recorded: {total_shifts}\n\n"
        f"{stock_section}"
    )
    await broadcast_to_exco(context, text, parse_mode="Markdown")


# --- Main ------------------------------------------------------------------


def parse_user_ids(raw: str, name: str) -> frozenset:
    """Parse a comma-separated list of Telegram user IDs from .env.

    Invalid entries are named on stderr and skipped rather than raising, so one
    typo can't stop the bot from starting. Skipping only ever removes access,
    never grants it, so the failure direction is safe.
    """
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            print(
                f"WARNING: ignoring invalid entry {part!r} in {name}.",
                file=sys.stderr,
            )
    return frozenset(ids)


def main() -> None:
    global AUTHORIZED_USERS, EXCO_USERS, ADMIN_USERS

    load_dotenv()
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        print("ERROR: TELEGRAM_TOKEN is missing or empty in .env", file=sys.stderr)
        sys.exit(1)

    ADMIN_USERS = parse_user_ids(os.getenv("ADMIN_USERS", ""), "ADMIN_USERS")
    EXCO_USERS = parse_user_ids(os.getenv("EXCO_USERS", ""), "EXCO_USERS")
    AUTHORIZED_USERS = parse_user_ids(
        os.getenv("AUTHORIZED_USERS", ""), "AUTHORIZED_USERS"
    )

    # Every tier fails closed, so an empty list is an outage rather than a
    # permissive default. Say so loudly — this is the main way a bad deploy
    # shows itself. Counts are reported per distinct tier, so somebody listed
    # in two lists is only counted at their highest.
    everyone = ADMIN_USERS | EXCO_USERS | AUTHORIZED_USERS
    print("--- Access control ---")
    print(f"  Admins:        {len(ADMIN_USERS)}")
    print(f"  EXCO users:    {len(EXCO_USERS - ADMIN_USERS)}")
    print(f"  Regular users: {len(AUTHORIZED_USERS - EXCO_USERS - ADMIN_USERS)}")
    if not ADMIN_USERS:
        print(
            "  WARNING: ADMIN_USERS is empty — NO unauthorized-access alerts "
            "can be sent to anyone.",
            file=sys.stderr,
        )
    if not (EXCO_USERS | ADMIN_USERS):
        print(
            "  WARNING: no admins or EXCO — /restock and /discard are "
            "unavailable, and expiry/weekly reports go nowhere.",
            file=sys.stderr,
        )
    if not everyone:
        print(
            "  WARNING: all three lists are empty — nobody can use the bot.",
            file=sys.stderr,
        )
    if os.getenv("ADMIN_CHAT_ID", "").strip():
        print(
            "  NOTE: ADMIN_CHAT_ID is set but no longer used — "
            "security alerts now go to ADMIN_USERS."
        )
    print("----------------------")

    application = (
        Application.builder()
        .token(token)
        .post_init(sync_all_command_menus)
        .build()
    )

    presale_conv = ConversationHandler(
        entry_points=[
            CommandHandler("recordpresale", recordpresale),
            # The keyboard button sends its label as plain text, so it needs a
            # text entry point -- a command mid-string never reaches CommandHandler.
            MessageHandler(filters.Text([BTN_PRESALE]), recordpresale),
        ],
        states={
            WAITING_FOR_PRESALE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, presale_input)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    postsale_conv = ConversationHandler(
        entry_points=[
            CommandHandler("recordpostsale", recordpostsale),
            MessageHandler(filters.Text([BTN_POSTSALE]), recordpostsale),
        ],
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
            RESTOCK_VARIETY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, restock_variety)
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

    discard_conv = ConversationHandler(
        entry_points=[CommandHandler("discard", discard)],
        states={
            DISCARD_SELECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, discard_select)
            ],
            DISCARD_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, discard_amount)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(presale_conv)
    application.add_handler(postsale_conv)
    application.add_handler(restock_conv)
    application.add_handler(discard_conv)
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("undo", undo))

    # Scheduled jobs (all times Asia/Singapore).
    #
    # NOTE: run_daily's `days` is 0-6 = SUNDAY-saturday, not Monday-first. PTB
    # flipped this in v20.0; the previous `days=(0,)  # Monday` was silently
    # running on Sundays. 5 = Friday.
    job_queue = application.job_queue
    expiry_time = time(hour=7, minute=0, second=0, tzinfo=TIMEZONE)
    summary_time = time(hour=21, minute=0, second=0, tzinfo=TIMEZONE)
    job_queue.run_daily(expiry_check_job, time=expiry_time)
    job_queue.run_daily(weekly_summary_job, time=summary_time, days=(5,))  # Friday

    print("Cafe Logistics bot is running. Press Ctrl+C to stop.")
    application.run_polling()


if __name__ == "__main__":
    main()
