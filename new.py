"""
WhatsApp Group Cloner — Telegram Bot
Uses Whapi.cloud API (free tier supported)
Termux/Android compatible — NO Chrome, NO Selenium

Setup:
  1. Sign up free at https://whapi.cloud
  2. Create a channel and get your API token
  3. Fill in BOT_TOKEN, OWNER_ID, WHAPI_TOKEN below
  4. pip install python-telegram-bot==21.6 requests
  5. python text.py
"""

import json
import logging
import os
import time

import requests
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# CONFIG
BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "8509987473:AAHKKIkP98OXDSixynYgrlvH7ad3SWkOMaQ")
OWNER_ID       = int(os.getenv("ADMIN_CHAT_ID",  "6992782253"))
GREEN_INSTANCE = os.getenv("GREEN_INSTANCE_ID",  "7107550564")
GREEN_TOKEN    = os.getenv("GREEN_API_TOKEN",     "76f1599b788648aa80502528e388d62f251efb14776e489b87")

GREEN_BASE  = "https://api.green-api.com"
ADMINS_FILE = "admins.json"


# ADMIN MANAGEMENT

def load_admins():
    admins = {OWNER_ID}
    if os.path.exists(ADMINS_FILE):
        try:
            with open(ADMINS_FILE, "r") as f:
                admins.update(int(i) for i in json.load(f).get("admins", []))
        except Exception:
            pass
    return admins


def save_admins(admins):
    with open(ADMINS_FILE, "w") as f:
        json.dump({"admins": [i for i in admins if i != OWNER_ID]}, f, indent=2)


def add_admin(user_id):
    admins = load_admins()
    if user_id in admins:
        return False
    admins.add(user_id)
    save_admins(admins)
    return True


def remove_admin(user_id):
    if user_id == OWNER_ID:
        return "owner"
    admins = load_admins()
    if user_id not in admins:
        return "not_found"
    admins.discard(user_id)
    save_admins(admins)
    return "ok"


def is_admin(user_id):
    return user_id in load_admins()


def is_owner(user_id):
    return user_id == OWNER_ID


# DECORATORS

def admin_only(func):
    async def wrapper(update, ctx):
        if not is_admin(update.effective_chat.id):
            await update.message.reply_text("Unauthorized.")
            return
        return await func(update, ctx)
    return wrapper


def owner_only(func):
    async def wrapper(update, ctx):
        if not is_owner(update.effective_chat.id):
            await update.message.reply_text("Only the bot owner can use this command.")
            return
        return await func(update, ctx)
    return wrapper


# HELPERS

async def safe_send(bot, chat_id, text, parse_mode="Markdown"):
    try:
        await bot.send_message(chat_id, text, parse_mode=parse_mode)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


def green_url(method):
    """Build Green API URL: https://api.green-api.com/waInstance{ID}/{method}/{TOKEN}"""
    return f"{GREEN_BASE}/waInstance{GREEN_INSTANCE}/{method}/{GREEN_TOKEN}"


def green_get(method, params=None):
    """Safe GET to Green API. Returns (data, error)."""
    try:
        r   = requests.get(green_url(method), params=params, timeout=15)
        raw = r.text.strip()
        if not raw:
            return None, "Empty response from API"
        data = r.json()
        if r.status_code >= 400:
            return None, data.get("message") or data.get("error") or f"HTTP {r.status_code}"
        return data, None
    except Exception as e:
        return None, str(e)


def green_post(method, payload=None):
    """Safe POST to Green API. Returns (data, error)."""
    try:
        r   = requests.post(green_url(method), json=payload or {}, timeout=20)
        raw = r.text.strip()
        if not raw:
            return None, "Empty response from API"
        data = r.json()
        if r.status_code >= 400:
            return None, data.get("message") or data.get("error") or f"HTTP {r.status_code}"
        return data, None
    except Exception as e:
        return None, str(e)


user_state   = {}
batch_sizes  = {}   # per-admin batch size setting (default 50)
DEFAULT_BATCH = 50  # safe default — change with /setbatch


# GREEN API FUNCTIONS

def wa_check_status():
    data, err = green_get("getStateInstance")
    if err:
        return False, err
    state = data.get("stateInstance", "unknown") if isinstance(data, dict) else "unknown"
    return state == "authorized", state


def wa_get_qr():
    import base64
    data, err = green_get("qr")
    if err:
        return None, err
    if not isinstance(data, dict):
        return None, f"Unexpected response: {data}"

    # 409 / already authorized
    t = data.get("type", "")
    if t == "alreadyLogged":
        return None, "ALREADY_CONNECTED"

    qr_raw = data.get("message", "")
    if not qr_raw:
        return None, f"No QR in response: {data}"

    if "," in qr_raw:
        qr_raw = qr_raw.split(",", 1)[1]
    qr_raw = qr_raw.strip()
    padding = 4 - (len(qr_raw) % 4)
    if padding != 4:
        qr_raw += "=" * padding
    try:
        return base64.b64decode(qr_raw), None
    except Exception as e:
        return None, f"Base64 decode failed: {e}"


def wa_get_pairing_code(phone):
    clean = phone.strip().replace("+", "").replace(" ", "").replace("-", "")
    if not clean.isdigit():
        return None, "Phone must be digits only."
    # Green API pairing code
    data, err = green_post("getPairingCode", {"phoneNumber": clean})
    if err:
        return None, err
    code = ""
    if isinstance(data, dict):
        code = data.get("pairingCode") or data.get("code") or data.get("message") or ""
    if code and len(str(code)) >= 4:
        return str(code), None
    return None, f"No code in response: {data}"


def wa_get_group_info_from_link(invite_link):
    if not invite_link.startswith("https://chat.whatsapp.com/"):
        raise RuntimeError("Link must start with https://chat.whatsapp.com/")

    invite_code = invite_link.replace("https://chat.whatsapp.com/", "").strip("/")

    # Try joining to get group ID
    data, err = green_post("joinGroup", {"inviteLink": invite_link})
    if not err and isinstance(data, dict):
        gid = data.get("groupId") or data.get("chatId") or data.get("id") or ""
        if gid:
            # Get group details
            gdata, _ = green_post("getGroupData", {"groupId": gid})
            name = gdata.get("subject", "Unknown") if isinstance(gdata, dict) else "Unknown"
            size = len(gdata.get("participants", [])) if isinstance(gdata, dict) else "?"
            return gid, name, size

    raise RuntimeError(
        f"Could not get group info.\n"
        f"Error: {err or 'no group ID in response'}\n\n"
        f"Note: Green API free plan may not support joinGroup.\n"
        f"Use /scrapeid and enter the group ID manually from the Green API dashboard."
    )


def wa_scrape_members(group_id):
    if not group_id.endswith("@g.us"):
        group_id = f"{group_id}@g.us"

    data, err = green_post("getGroupData", {"groupId": group_id})
    if err:
        raise RuntimeError(f"Could not get group data: {err}")
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected response type: {type(data)}")

    participants = data.get("participants") or data.get("members") or []
    if not participants:
        raise RuntimeError(f"No participants found. API keys: {list(data.keys())}")

    numbers = []
    for p in participants:
        if isinstance(p, dict):
            pid = p.get("id") or p.get("jid") or p.get("phone") or ""
        else:
            pid = str(p)
        number = (
            str(pid)
            .replace("@c.us", "").replace("@s.whatsapp.net", "")
            .replace("@g.us", "").replace("+", "").replace(" ", "").strip()
        )
        if number and number.isdigit() and 6 < len(number) < 16:
            numbers.append(number)

    numbers = list(dict.fromkeys(numbers))
    if not numbers:
        raise RuntimeError(
            f"Found {len(participants)} participants but no valid numbers.\n"
            f"Sample: {str(participants[:2])}"
        )
    return numbers, data.get("subject") or group_id


def wa_scrape_via_link(invite_link):
    if not invite_link.startswith("https://chat.whatsapp.com/"):
        raise RuntimeError("Link must start with https://chat.whatsapp.com/")

    group_id = ""

    # Try joinGroup to get group ID
    data, err = green_post("joinGroup", {"inviteLink": invite_link})
    if not err and isinstance(data, dict):
        group_id = data.get("groupId") or data.get("chatId") or data.get("id") or ""

    if not group_id:
        raise RuntimeError(
            f"Could not join group.\nError: {err or 'no group ID returned'}\n\n"
            "Use /groupid or /scrapeid with the group ID from Green API dashboard."
        )

    time.sleep(2)
    return wa_scrape_members(group_id)


def wa_create_group(group_name, numbers, batch_size=50, delay=10):
    """
    Create WhatsApp group in batches to avoid bans.
    batch_size: members per group (default 50, max 249)
    delay: seconds to wait between creating multiple groups
    """
    if not numbers:
        raise RuntimeError("No numbers to add.")

    # Clamp batch size between 5 and 249
    batch_size = max(5, min(249, batch_size))

    # Split numbers into batches
    batches = [numbers[i:i+batch_size] for i in range(0, len(numbers), batch_size)]
    total_added  = 0
    total_failed = 0
    results      = []

    for idx, batch in enumerate(batches):
        # Name each group: "GroupName" or "GroupName (2)", "GroupName (3)"...
        name = group_name if idx == 0 else f"{group_name} ({idx + 1})"
        participants = [f"{n}@c.us" for n in batch]

        # Try with increasing timeouts
        data     = None
        last_err = None
        for timeout in [30, 60, 90]:
            try:
                r   = requests.post(
                    green_url("createGroup"),
                    json={"groupName": name, "chatIds": participants},
                    timeout=timeout,
                )
                raw = r.text.strip()
                if raw:
                    data     = r.json()
                    last_err = None
                    break
            except requests.exceptions.Timeout:
                last_err = f"Timed out after {timeout}s"
                time.sleep(3)
            except Exception as e:
                last_err = str(e)
                break

        if last_err or not data or not isinstance(data, dict):
            total_failed += len(batch)
            results.append({
                "name": name, "added": 0,
                "failed": len(batch), "invite_link": "N/A",
                "error": last_err or "No response"
            })
            continue

        group_id    = data.get("chatId") or data.get("groupId") or data.get("id") or ""
        failed_list = []
        if isinstance(data.get("addParticipants"), dict):
            failed_list = data["addParticipants"].get("notAddedChatIds", [])
        failed = len(failed_list)
        added  = len(batch) - failed
        total_added  += added
        total_failed += failed

        # Get invite link
        invite_link = "N/A"
        if group_id:
            try:
                link_data, _ = green_post("getGroupInviteLink", {"groupId": group_id})
                if isinstance(link_data, dict):
                    invite_link = link_data.get("link") or link_data.get("inviteLink") or "N/A"
            except Exception:
                pass

        results.append({
            "name": name, "added": added,
            "failed": failed, "invite_link": invite_link,
            "error": None
        })

        # Wait between batches to avoid ban
        if idx < len(batches) - 1:
            logger.info(f"Batch {idx+1} done. Waiting {delay}s before next batch...")
            time.sleep(delay)

    return {
        "added":   total_added,
        "failed":  total_failed,
        "batches": results,
        "total_groups": len(batches),
    }


# TELEGRAM COMMANDS

@admin_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    owner_cmds = ""
    if is_owner(update.effective_chat.id):
        owner_cmds = (
            "\n\n👑 *Owner Commands:*\n"
            "• /addadmin `<id>` — Add admin\n"
            "• /removeadmin `<id>` — Remove admin\n"
            "• /listadmins — List all admins"
        )
    await update.message.reply_text(
        "👋 *WhatsApp Group Cloner Bot*\n"
        "_Powered by Whapi.cloud_\n\n"
        "🔐 *Login:*\n"
        "• /status — Check connection\n"
        "• /qr — Login via QR code\n"
        "• /logincode `<phone>` — Login via phone code\n\n"
        "📋 *Scraping:*\n"
        "• /scrape — Extract via invite link\n"
        "• /groupid — Get group ID from link\n"
        "• /scrapeid — Extract via group ID\n\n"
        "📤 *Cloning:*\n"
        "• /clone — Create new group with members\n"
        "• /setbatch `<n>` — Set members per group (default 50)\n"
        "• /batchinfo — Show batch settings & group preview\n"
        "• /mynumbers — Show extracted numbers"
        f"{owner_cmds}\n\n"
        "*Flow:* /status → /qr or /logincode → /scrape → /clone",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Checking connection...")
    connected, state = wa_check_status()
    if connected:
        await update.message.reply_text("✅ *WhatsApp is connected and ready!*", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"❌ *Not connected.* State: `{state}`\n\nUse /qr or /logincode.",
            parse_mode="Markdown",
        )


@admin_only
async def cmd_qr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    connected, _ = wa_check_status()
    if connected:
        await update.message.reply_text("✅ Already connected!")
        return
    await update.message.reply_text("⏳ Fetching QR code...")
    qr_bytes, error = wa_get_qr()

    if error == "ALREADY_CONNECTED":
        await update.message.reply_text(
            "✅ *WhatsApp is already connected!*\n\n"
            "No need to scan QR. Use /scrape to get started.",
            parse_mode="Markdown",
        )
        return

    if error:
        await update.message.reply_text(
            f"❌ Could not get QR.\n\nError: `{error}`",
            parse_mode="Markdown",
        )
        return

    await ctx.bot.send_photo(chat_id=update.effective_chat.id, photo=qr_bytes)
    await update.message.reply_text(
        "📱 Scan with WhatsApp → Linked Devices → Link a Device\n\nThen use /status.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_logincode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    connected, _ = wa_check_status()
    if connected:
        await update.message.reply_text("✅ Already connected!")
        return
    args = ctx.args
    if not args:
        await update.message.reply_text(
            "📱 *Login via Phone Number*\n\nUsage: `/logincode <phone>`\n\n"
            "Example: `/logincode 919876543210`\n\n"
            "Country code + number, no + or spaces.",
            parse_mode="Markdown",
        )
        return
    phone = args[0].strip().replace("+", "").replace(" ", "").replace("-", "")
    if not phone.isdigit():
        await update.message.reply_text("❌ Digits only. Example: `919876543210`", parse_mode="Markdown")
        return
    await update.message.reply_text(f"⏳ Requesting code for `+{phone}`...", parse_mode="Markdown")
    code, error = wa_get_pairing_code(phone)
    if error:
        await update.message.reply_text(f"❌ Failed.\n\nError: `{error}`\n\nTry /qr instead.", parse_mode="Markdown")
        return
    await update.message.reply_text(
        f"🔑 *Pairing Code:*\n\n`{code}`\n\n"
        f"WhatsApp → Linked Devices → Link a Device → *Link with phone number instead*\n\n"
        f"⏳ Expires in ~2 min. Use /status after.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_groupid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_chat.id] = "awaiting_groupid_link"
    await update.message.reply_text(
        "🔗 *Send the WhatsApp group invite link*\n\n"
        "Example:\n`https://chat.whatsapp.com/AbCdEfGhIjK`",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_scrape(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    connected, state = wa_check_status()
    if not connected:
        await update.message.reply_text(f"❌ Not connected (`{state}`). Use /qr first.", parse_mode="Markdown")
        return
    user_state[update.effective_chat.id] = "awaiting_scrape_link"
    await update.message.reply_text(
        "📎 *Send the WhatsApp group invite link*\n\n"
        "Example:\n`https://chat.whatsapp.com/AbCdEfGhIjK`",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_scrapeid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    connected, state = wa_check_status()
    if not connected:
        await update.message.reply_text(f"❌ Not connected (`{state}`). Use /qr first.", parse_mode="Markdown")
        return
    user_state[update.effective_chat.id] = "awaiting_group_id"
    await update.message.reply_text(
        "🆔 *Send the WhatsApp Group ID*\n\n"
        "Example: `120363XXXXXXXXXX@g.us`\n\n"
        "Use /groupid to get it from an invite link.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_clone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    numbers = user_state.get(f"numbers_{chat_id}", [])
    if not numbers:
        await update.message.reply_text("⚠️ No numbers yet. Use /scrape or /scrapeid first.")
        return
    connected, state = wa_check_status()
    if not connected:
        await update.message.reply_text(f"❌ Not connected (`{state}`). Use /qr first.", parse_mode="Markdown")
        return
    user_state[chat_id] = "awaiting_group_name"
    await update.message.reply_text(
        f"📋 *{len(numbers)} numbers ready.*\n\nWhat should the new group be named?",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_mynumbers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    numbers = user_state.get(f"numbers_{chat_id}", [])
    if not numbers:
        await update.message.reply_text("No numbers yet. Use /scrape first.")
        return
    preview = "\n".join(f"• +{n}" for n in numbers[:15])
    extra   = f"\n_...and {len(numbers) - 15} more_" if len(numbers) > 15 else ""
    await update.message.reply_text(
        f"📋 *Extracted numbers ({len(numbers)} total):*\n\n{preview}{extra}",
        parse_mode="Markdown",
    )


@owner_only
async def cmd_addadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("❌ Usage: `/addadmin <user_id>`", parse_mode="Markdown")
        return
    user_id = int(args[0])
    if user_id == OWNER_ID:
        await update.message.reply_text("ℹ️ You are already the owner!")
        return
    if add_admin(user_id):
        await update.message.reply_text(f"✅ Admin added: `{user_id}`", parse_mode="Markdown")
        try:
            await ctx.bot.send_message(user_id, "You have been added as admin! Use /start.")
        except Exception:
            await update.message.reply_text("⚠️ Could not notify user.")
    else:
        await update.message.reply_text(f"ℹ️ `{user_id}` is already an admin.", parse_mode="Markdown")


@owner_only
async def cmd_removeadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("❌ Usage: `/removeadmin <user_id>`", parse_mode="Markdown")
        return
    user_id = int(args[0])
    result  = remove_admin(user_id)
    if result == "owner":
        await update.message.reply_text("⛔ Cannot remove the owner.")
    elif result == "not_found":
        await update.message.reply_text(f"❌ `{user_id}` is not an admin.", parse_mode="Markdown")
    elif result == "ok":
        await update.message.reply_text(f"✅ Removed: `{user_id}`", parse_mode="Markdown")
        try:
            await ctx.bot.send_message(user_id, "Your admin access has been removed.")
        except Exception:
            pass


@owner_only
async def cmd_listadmins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    admins = load_admins()
    lines  = [f"• `{a}` — {'👑 Owner' if a == OWNER_ID else '🛡 Admin'}" for a in sorted(admins)]
    await update.message.reply_text(
        f"📋 *Admins ({len(admins)}):*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


@admin_only
async def cmd_setbatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Set how many members to add per group. Usage: /setbatch 50"""
    chat_id = update.effective_chat.id
    args    = ctx.args

    if not args:
        current = batch_sizes.get(chat_id, DEFAULT_BATCH)
        await update.message.reply_text(
            f"📦 *Batch Size Settings*\n\nCurrent: *{current} members per group*\n\n"
            "Usage: `/setbatch <number>`\n\n"
            "Examples:\n"
            "• `/setbatch 20` — safest (low ban risk)\n"
            "• `/setbatch 50` — balanced (recommended)\n"
            "• `/setbatch 100` — faster (higher risk)\n"
            "• `/setbatch 249` — maximum (highest risk)\n\n"
            "⚠️ Lower = safer from WhatsApp ban",
            parse_mode="Markdown",
        )
        return

    if not args[0].isdigit():
        await update.message.reply_text("❌ Please enter a number. Example: `/setbatch 50`", parse_mode="Markdown")
        return

    size = int(args[0])
    if size < 5:
        await update.message.reply_text("❌ Minimum batch size is 5.")
        return
    if size > 249:
        await update.message.reply_text("❌ Maximum batch size is 249 (WhatsApp limit).")
        return

    batch_sizes[chat_id] = size

    # Risk level
    if size <= 20:
        risk = "🟢 Very Safe"
    elif size <= 50:
        risk = "🟡 Safe (Recommended)"
    elif size <= 100:
        risk = "🟠 Medium Risk"
    else:
        risk = "🔴 High Risk"

    await update.message.reply_text(
        f"✅ *Batch size set to {size}*\n\nRisk level: {risk}\n\nWhen you use /clone, members will be split into groups of {size}.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_batchinfo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show current batch settings and how groups will be split."""
    chat_id    = update.effective_chat.id
    batch_size = batch_sizes.get(chat_id, DEFAULT_BATCH)
    numbers    = user_state.get(f"numbers_{chat_id}", [])

    if numbers:
        total_groups = max(1, -(-len(numbers) // batch_size))
        preview = f"\n\n📊 *With your {len(numbers)} extracted numbers:*\n• {total_groups} group(s) will be created\n• ~{batch_size} members each"
    else:
        preview = "\n\n_No numbers extracted yet. Use /scrape first._"

    if batch_size <= 20:
        risk = "🟢 Very Safe"
    elif batch_size <= 50:
        risk = "🟡 Safe (Recommended)"
    elif batch_size <= 100:
        risk = "🟠 Medium Risk"
    else:
        risk = "🔴 High Risk"

    await update.message.reply_text(
        f"📦 *Current Batch Settings*\n\n"
        f"Batch size: *{batch_size} members per group*\n"
        f"Risk level: {risk}\n"
        f"Delay between groups: *10 seconds*"
        f"{preview}\n\n"
        "Use /setbatch to change.",
        parse_mode="Markdown",
    )


@admin_only
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state   = user_state.get(chat_id)
    text    = update.message.text.strip()

    if state == "awaiting_groupid_link":
        del user_state[chat_id]
        if not text.startswith("https://chat.whatsapp.com/"):
            await update.message.reply_text("❌ Must start with `https://chat.whatsapp.com/`", parse_mode="Markdown")
            return
        await update.message.reply_text("🔍 Fetching group info...")
        try:
            gid, name, size = wa_get_group_info_from_link(text)
            await safe_send(ctx.bot, chat_id,
                f"✅ *Group Info Found!*\n\n📌 Name: *{name}*\n👥 Members: {size}\n🆔 Group ID:\n`{gid}`\n\nNow use /scrapeid and paste this ID.")
        except Exception as e:
            await safe_send(ctx.bot, chat_id, f"❌ Could not fetch group info.\n\n`{e}`")
        return

    if state == "awaiting_scrape_link":
        del user_state[chat_id]
        if not text.startswith("https://chat.whatsapp.com/"):
            await update.message.reply_text("❌ Must start with `https://chat.whatsapp.com/`", parse_mode="Markdown")
            return
        await update.message.reply_text("🔍 Fetching members... please wait.")
        try:
            numbers, group_name = wa_scrape_via_link(text)
            user_state[f"numbers_{chat_id}"] = numbers
            preview = "\n".join(f"• +{n}" for n in numbers[:10])
            extra   = f"\n_...and {len(numbers) - 10} more_" if len(numbers) > 10 else ""
            await safe_send(ctx.bot, chat_id,
                f"✅ *Extracted {len(numbers)} members from \"{group_name}\"!*\n\n{preview}{extra}\n\nUse /clone.")
        except Exception as e:
            await safe_send(ctx.bot, chat_id, f"❌ Failed.\n\n`{e}`")
        return

    if state == "awaiting_group_id":
        del user_state[chat_id]
        await update.message.reply_text(f"🔍 Fetching members for `{text}`...", parse_mode="Markdown")
        try:
            numbers, group_name = wa_scrape_members(text)
            user_state[f"numbers_{chat_id}"] = numbers
            preview = "\n".join(f"• +{n}" for n in numbers[:10])
            extra   = f"\n_...and {len(numbers) - 10} more_" if len(numbers) > 10 else ""
            await safe_send(ctx.bot, chat_id,
                f"✅ *Extracted {len(numbers)} members from \"{group_name}\"!*\n\n{preview}{extra}\n\nUse /clone.")
        except Exception as e:
            await safe_send(ctx.bot, chat_id, f"❌ Failed.\n\n`{e}`")
        return

    if state == "awaiting_group_name":
        del user_state[chat_id]
        group_name  = text
        numbers     = user_state.get(f"numbers_{chat_id}", [])
        batch_size  = batch_sizes.get(chat_id, DEFAULT_BATCH)
        total_groups = max(1, -(-len(numbers) // batch_size))  # ceiling division

        await safe_send(
            ctx.bot, chat_id,
            f"⚙️ Creating *\"{group_name}\"* with {len(numbers)} members\n"
            f"📦 Batch size: *{batch_size}* per group\n"
            f"📋 Total groups to create: *{total_groups}*\n\n"
            f"⏳ Please wait — adding delays between groups to avoid ban..."
        )
        try:
            result = wa_create_group(group_name, numbers, batch_size=batch_size, delay=10)

            # Build per-group summary
            lines = []
            for i, b in enumerate(result["batches"]):
                if b["error"]:
                    lines.append(f"Group {i+1}: ❌ {b['error'][:50]}")
                else:
                    lines.append(
                        f"*{b['name']}*\n"
                        f"✔️ {b['added']} added | ❌ {b['failed']} failed\n"
                        f"🔗 {b['invite_link']}"
                    )

            summary = "\n\n".join(lines)
            await safe_send(
                ctx.bot, chat_id,
                f"✅ *All Done!*\n\n"
                f"👥 Total added: {result['added']}\n"
                f"❌ Total failed: {result['failed']}\n"
                f"📋 Groups created: {result['total_groups']}\n\n"
                f"{summary}"
            )
        except Exception as e:
            await safe_send(ctx.bot, chat_id, f"❌ Failed.\n\n`{e}`")
        return

    await update.message.reply_text("Use /start to see commands.")


# ENTRY POINT

def main():
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        raise SystemExit("\n❌  Set TELEGRAM_BOT_TOKEN\n")
    if OWNER_ID == 0:
        raise SystemExit("\n❌  Set ADMIN_CHAT_ID\n")
    if GREEN_INSTANCE == "YOUR_INSTANCE_ID":
        raise SystemExit("\n❌  Set GREEN_INSTANCE_ID — sign up at https://green-api.com\n")
    if GREEN_TOKEN == "YOUR_GREEN_API_TOKEN":
        raise SystemExit("\n❌  Set GREEN_API_TOKEN — sign up at https://green-api.com\n")

    logger.info(f"Starting | Owner: {OWNER_ID} | Admins: {load_admins()}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("qr",          cmd_qr))
    app.add_handler(CommandHandler("logincode",   cmd_logincode))
    app.add_handler(CommandHandler("groupid",     cmd_groupid))
    app.add_handler(CommandHandler("scrape",      cmd_scrape))
    app.add_handler(CommandHandler("scrapeid",    cmd_scrapeid))
    app.add_handler(CommandHandler("clone",       cmd_clone))
    app.add_handler(CommandHandler("setbatch",    cmd_setbatch))
    app.add_handler(CommandHandler("batchinfo",   cmd_batchinfo))
    app.add_handler(CommandHandler("mynumbers",   cmd_mynumbers))
    app.add_handler(CommandHandler("addadmin",    cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    app.add_handler(CommandHandler("listadmins",  cmd_listadmins))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
