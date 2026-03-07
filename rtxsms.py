"""
PROJECT: PREMIUM OTP BOT (Ultimate Update - Version 3.1)
FEATURES: Auto Message Delete, Italic OTP format, Advanced Admin Panel, Ban System.
UPDATES: Removed FAQ & Find Old SMS entirely.
"""

import logging
import aiohttp
import os
import asyncio
import re
import sqlite3
import html
import datetime
import json
from contextlib import contextmanager
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, ConversationHandler
from telegram.constants import ParseMode

# ==============================================================================
# CONFIGURATION
# ==============================================================================

# Bot Token & Admin ID
TOKEN = "8784714590:AAGW1bthOSIh2HUl2vPCYS_zv13zEz7BOsg"
ADMIN_ID = 6031032502

# Channels required to join (Anti-Leech)
CHANNELS = ["@EarnXtract", "@RTx_Sms"]

# API Endpoints
API_GET_NUM = "https://sheba10.site/otpX/getnum.php"
API_OTP_CHECK = "https://sheba10.site/otpX/otpcheck.php"
API_CONSOLE = "https://sheba10.site/otpX/console.php"
API_2FA = "https://2fa.cn/codes/{}"

# Economy & Referral System Settings
REWARD_PER_OTP = 0.00125  # 0.15 BDT ($0.00125 at 120 BDT/USD) - Lifetime Commission for Referrer
MIN_WITHDRAW_BDT = 50     # Minimum Withdrawal in BDT
MIN_WITHDRAW_USD = 0.416  # Minimum Withdrawal in USD (50/120)
USD_TO_BDT_RATE = 120     # Exchange Rate: $1 = 120 BDT

# Bot Startup Time for /status
START_TIME = datetime.datetime.now()

# Anti-Blocking Headers for API Requests
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    "Accept": "application/json"
}

# Conversation Handler States
SELECT_METHOD, ENTER_ADDRESS = range(2)

# Performance Settings
MAX_CONCURRENT_API_CALLS = 50 
API_TIMEOUT = 12 
DB_POOL_SIZE = 10 

# Logging Configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==============================================================================
# DATABASE MANAGEMENT WITH CONNECTION POOLING & BAN SYSTEM
# ==============================================================================

DB_FILE = "bot.db"

class DatabasePool:
    def __init__(self, db_file, pool_size=10):
        self.db_file = db_file
        self.pool_size = pool_size
        self._lock = asyncio.Lock()
        
    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_file, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

db_pool = DatabasePool(DB_FILE, DB_POOL_SIZE)

def init_db():
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0.0,
            referrer_id INTEGER,
            otp_success_count INTEGER DEFAULT 0,
            total_earned REAL DEFAULT 0.0,
            join_date TEXT,
            is_banned INTEGER DEFAULT 0
        )''')
        
        try:
            c.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
            
        c.execute('''CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            method TEXT,
            address TEXT,
            amount_usd REAL,
            amount_bdt REAL,
            status TEXT DEFAULT 'pending',
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS otp_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            number TEXT,
            code TEXT,
            service TEXT,
            full_message TEXT,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(user_id, status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_otp_history_user ON otp_history(user_id, date DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_otp_history_number ON otp_history(number)')
        
        conn.commit()
    logger.info("✅ Database initialized successfully.")

def get_user(user_id):
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return c.fetchone()

def is_user_banned(user_id):
    user = get_user(user_id)
    if user and len(user) > 6 and user[6] == 1:
        return True
    return False

def register_user(user_id, referrer_id=None):
    if get_user(user_id) is None:
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO users (user_id, referrer_id, join_date) VALUES (?, ?, CURRENT_TIMESTAMP)", 
                     (user_id, referrer_id))
            conn.commit()
        return True
    return False

def save_otp_history(user_id, number, code, service, msg):
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO otp_history (user_id, number, code, service, full_message) VALUES (?, ?, ?, ?, ?)",
                     (user_id, number, code, service, msg))
            conn.commit()
        except Exception as e:
            logger.error(f"Save History Error: {e}")

def update_otp_and_reward(user_id):
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        reward_given = False
        referrer_id = None
        
        try:
            c.execute("UPDATE users SET otp_success_count = otp_success_count + 1 WHERE user_id=?", (user_id,))
            c.execute("SELECT referrer_id FROM users WHERE user_id=?", (user_id,))
            data = c.fetchone()
            
            if data and data[0]:
                referrer_id = data[0]
                c.execute("UPDATE users SET balance = balance + ?, total_earned = total_earned + ? WHERE user_id=?",
                            (REWARD_PER_OTP, REWARD_PER_OTP, referrer_id))
                reward_given = True
            
            conn.commit()
        except Exception as e:
            logger.error(f"DB Reward Error: {e}")
    
    return reward_given, referrer_id, REWARD_PER_OTP

# ==============================================================================
# HELPER FUNCTIONS & COUNTRY FLAGS
# ==============================================================================

COUNTRY_FLAGS = {
    "Sierra Leone": "🇸🇱", "Ivory Coast": "🇨🇮", "Vietnam": "🇻🇳", "Cameroon": "🇨🇲", 
    "Senegal": "🇸🇳", "Mali": "🇲🇱", "Ghana": "🇬🇭", "Nigeria": "🇳🇬", "Kenya": "🇰🇪",
    "India": "🇮🇳", "Bangladesh": "🇧🇩", "Pakistan": "🇵🇰", "Indonesia": "🇮🇩",
    "Tajikistan": "🇹🇯", "Kyrgyzstan": "🇰🇬", "Madagascar": "🇲🇬", "Togo": "🇹🇬",
    "Morocco": "🇲🇦", "Egypt": "🇪🇬", "South Africa": "🇿🇦", "Brazil": "🇧🇷",
    "USA": "🇺🇸", "UK": "🇬🇧", "Russia": "🇷🇺", "China": "🇨🇳", "France": "🇫🇷",
    "Germany": "🇩🇪", "Philippines": "🇵🇭", "Thailand": "🇹🇭", "Malaysia": "🇲🇾",
    "Tanzania": "🇹🇿", "Uganda": "🇺🇬", "Zambia": "🇿🇲", "Zimbabwe": "🇿🇼",
    "Algeria": "🇩🇿", "Tunisia": "🇹🇳", "Burkina Faso": "🇧🇫", "Guinea": "🇬🇳",
    "Benin": "🇧🇯", "Rwanda": "🇷🇼", "Angola": "🇦🇴", "Mozambique": "🇲🇿",
    "Argentina": "🇦🇷", "Colombia": "🇨🇴", "Peru": "🇵🇪", "Venezuela": "🇻🇪",
    "Chile": "🇨🇱", "Ecuador": "🇪🇨", "Bolivia": "🇧🇴", "Mexico": "🇲🇽",
    "Canada": "🇨🇦", "Spain": "🇪🇸", "Italy": "🇮🇹", "Netherlands": "🇳🇱",
    "Turkey": "🇹🇷", "Iran": "🇮🇷", "Iraq": "🇮🇶", "Saudi Arabia": "🇸🇦",
    "UAE": "🇦🇪", "Myanmar": "🇲🇲", "Nepal": "🇳🇵", "Sri Lanka": "🇱🇰"
}

def get_flag(country_name):
    if country_name in COUNTRY_FLAGS:
        return COUNTRY_FLAGS[country_name]
    for name, flag in COUNTRY_FLAGS.items():
        if name.lower() == country_name.lower():
            return flag
    return "🌍"

async def check_subscription(user_id, bot):
    for channel in CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in ['left', 'kicked']:
                return False
        except Exception:
            return False
    return True

def extract_code(message):
    match = re.search(r'\b\d{4,8}\b', str(message))
    return match.group(0) if match else "See Msg"

def clean_number(num):
    return re.sub(r'\D', '', str(num))

def is_number_match(user_number, api_number):
    u_num = clean_number(user_number)
    a_num = clean_number(api_number)
    if not u_num or not a_num:
        return False
    check_len = min(min(len(u_num), len(a_num)), 8)
    return u_num[-check_len:] == a_num[-check_len:]

def escape_html(text):
    return html.escape(str(text))

async def send_join_prompt(update, context):
    keyboard = [[InlineKeyboardButton(f"📢 Join {c}", url=f"https://t.me/{c.replace('@', '')}")] for c in CHANNELS]
    keyboard.append([InlineKeyboardButton("✅ Joined / Verify", callback_data="check_join")])
    msg = (
        "⛔ <b>Access Denied!</b>\n\n"
        "To use this premium bot, you must be a member of our official channels.\n"
        "<i>Please join below to continue.</i>"
    )
    
    if update.callback_query:
        try: await update.callback_query.message.delete()
        except: pass
        await context.bot.send_message(chat_id=update.effective_chat.id, text=msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

async def check_ban_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_user_banned(user_id):
        if update.callback_query:
            await update.callback_query.answer("🚫 You are banned from using this bot.", show_alert=True)
        else:
            await update.message.reply_text("🚫 <b>You have been banned by the Admin.</b>\nContact support if you think this is a mistake.", parse_mode=ParseMode.HTML)
        return True
    return False

# ==============================================================================
# AUTO OTP CHECKING & NUMBER GENERATION LOGIC
# ==============================================================================

async def fetch_otp_async(session):
    try:
        timeout = aiohttp.ClientTimeout(total=8, connect=3, sock_read=6)
        async with session.get(API_OTP_CHECK, headers=HEADERS, timeout=timeout, ssl=False) as response:
            if response.status == 200:
                return await response.json()
            return None
    except Exception as e:
        logger.warning(f"OTP Fetch Warning: {e}")
        return None

async def auto_check_otp_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    user_id = data['user_id']
    target_number = data['number']
    chat_id = data['chat_id']
    message_id = data['message_id']
    
    try:
        timeout = aiohttp.ClientTimeout(total=10, connect=3)
        connector = aiohttp.TCPConnector(limit=20, force_close=True)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            result = await fetch_otp_async(session)
            if not result:
                return
            
            otp_list = result.get('data', {}).get('numbers', [])
            found_otp = None
            
            for item in otp_list:
                if item.get('status') == 'success':
                    api_num = item.get('number')
                    if is_number_match(target_number, api_num):
                        found_otp = item
                        break
            
            if found_otp:
                logger.info(f"✅ OTP Found for User {user_id} - Number: {target_number}")
                
                job.schedule_removal()
                
                try: await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
                except: pass
                
                raw_msg = found_otp.get('message', 'No Message')
                code_only = extract_code(raw_msg)
                svc_name = found_otp.get('full_number', 'Service')
                
                save_otp_history(user_id, found_otp.get('number'), code_only, svc_name, raw_msg)
                
                # REQ: Italicized text using <i> ... </i> and no inline buttons
                final_msg = (
                    f"<i>🔑 OTP Received ✅\n\n"
                    f"📱 Service : {escape_html(svc_name)}\n"
                    f"🔢 Number : <code>{found_otp.get('number')}</code>\n"
                    f"🔑 OTP : <code>{code_only}</code>\n"
                    f"📢 Refer for earn more ✅</i>"
                )
                
                await context.bot.send_message(
                    chat_id=chat_id, 
                    text=final_msg, 
                    parse_mode=ParseMode.HTML
                )
                
                # Reward processing
                reward_given, referrer_id, amount = update_otp_and_reward(user_id)
                if reward_given and referrer_id:
                    try:
                        ref_msg = (
                            f"🔔 <b>Commission Received!</b>\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"👤 <b>Source:</b> Referral User\n"
                            f"💰 <b>Amount:</b> +${amount:.5f} (0.15 BDT)\n"
                            f"✅ <b>Status:</b> Added to Wallet"
                        )
                        await context.bot.send_message(
                            chat_id=referrer_id,
                            text=ref_msg,
                            parse_mode=ParseMode.HTML
                        )
                    except: pass
    
    except Exception as e:
        logger.error(f"Auto Check Job Error: {e}")

async def get_number_api(update: Update, context: ContextTypes.DEFAULT_TYPE, range_val):
    if update.message:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
    else:
        chat_id = update.callback_query.message.chat_id
        user_id = update.callback_query.from_user.id
        try: await update.callback_query.message.delete()
        except: pass

    loading_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ <i>Connecting to premium server...</i>", parse_mode=ParseMode.HTML)
    
    range_val = str(range_val).strip()
    if not range_val.upper().endswith("XXX"):
        range_val += "XXX"
        
    for job in context.job_queue.get_jobs_by_name(str(user_id)):
        job.schedule_removal()
    
    try:
        timeout = aiohttp.ClientTimeout(total=12, connect=5, sock_read=10)
        connector = aiohttp.TCPConnector(limit=30, force_close=True)
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            url = f"{API_GET_NUM}?range={range_val}"
            async with session.get(url, headers=HEADERS, ssl=False) as response:
                if response.status == 200:
                    resp_json = await response.json()
                else:
                    try: await loading_msg.delete()
                    except: pass
                    await context.bot.send_message(chat_id=chat_id, text=f"❌ <b>API Error!</b> HTTP Status: {response.status}", parse_mode=ParseMode.HTML)
                    return
        
        try: await loading_msg.delete()
        except: pass

        if resp_json and 'data' in resp_json:
            data = resp_json['data']
            number_val = data.get('number', 'N/A')
            country_name = data.get('country', 'Unknown')
            flag = get_flag(country_name)
            
            context.user_data['current_number'] = number_val
            context.user_data['range'] = range_val
            
            txt = (
                f"✅ <b>Number Generated!</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📞 <b>Number:</b> <code>{number_val}</code>\n"
                f"{flag} <b>Country:</b> {country_name}\n"
                f"📊 <b>Status:</b> Pending ⏳\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<i>🚀 Waiting for SMS code... (Auto-Checking)</i>"
            )
            
            kb = [
                [InlineKeyboardButton("📥 Refresh Inbox", callback_data="refresh_inbox")],
                [InlineKeyboardButton("🔄 Change Number", callback_data="change_num"),
                 InlineKeyboardButton("🔙 Back", callback_data="go_cat")]
            ]
            
            sent_msg = await context.bot.send_message(chat_id=chat_id, text=txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
            
            context.job_queue.run_repeating(
                auto_check_otp_job, 
                interval=8, 
                first=4, 
                name=str(user_id),
                data={
                    'user_id': user_id, 
                    'number': number_val, 
                    'chat_id': sent_msg.chat_id, 
                    'message_id': sent_msg.message_id
                }
            )
        else:
            if isinstance(resp_json, dict):
                err = resp_json.get('message', 'Server empty response')
            else:
                err = 'Unknown server error'
            await context.bot.send_message(chat_id=chat_id, text=f"❌ <b>Error:</b> {err}\n\n<i>Please try another country or category.</i>", parse_mode=ParseMode.HTML)
            
    except Exception as e:
        logger.error(f"Generate Number Error: {e}")
        try: await loading_msg.delete()
        except: pass
        await context.bot.send_message(chat_id=chat_id, text="❌ <b>Server Connection Error!</b>\n\nAPI is unavailable right now. Please try again later.", parse_mode=ParseMode.HTML)

# ==============================================================================
# COMMANDS & MAIN MENUS
# ==============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_ban_middleware(update, context): return
    
    user_id = update.effective_user.id
    first_name = update.effective_user.first_name
    
    referrer_id = None
    if context.args:
        try:
            potential_ref = int(context.args[0])
            if potential_ref != user_id: 
                referrer_id = potential_ref
        except ValueError: 
            pass
    
    is_new = register_user(user_id, referrer_id)
    if is_new and referrer_id:
        try: 
            await context.bot.send_message(
                chat_id=referrer_id, 
                text=f"👤 <b>New Referral Joined!</b>\nName: {first_name}\nID: <code>{user_id}</code>", 
                parse_mode=ParseMode.HTML
            )
        except: pass
    
    context.user_data.clear()
    
    if not await check_subscription(user_id, context.bot):
        await send_join_prompt(update, context)
    else:
        await show_main_menu(update, context)

async def show_main_menu(update_obj, context):
    kb = [
        ["📱 Get Number", "🔐 Get 2FA Code"],
        ["💰 Wallet / Refer", "💸 Withdraw"]
    ]
    msg = (
        "🤖 <b>Premium OTP Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👋 Welcome to the most advanced OTP system.\n"
        "Choose a service from the menu below to start receiving OTPs instantly."
    )
    reply_markup = ReplyKeyboardMarkup(kb, resize_keyboard=True)
    
    if hasattr(update_obj, 'message') and update_obj.message:
        await update_obj.message.reply_text(msg, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        try: await update_obj.callback_query.message.delete()
        except: pass
        await context.bot.send_message(chat_id=update_obj.effective_chat.id, text=msg, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

# ==============================================================================
# WALLET FEATURE
# ==============================================================================

async def wallet_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    balance_usd = user[1]
    balance_bdt = balance_usd * USD_TO_BDT_RATE
    total_earned = user[4] if len(user) > 4 else 0.0
    
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users WHERE referrer_id=?", (user_id,))
        total_refs = c.fetchone()[0]
    
    ref_link = f"https://t.me/{context.bot.username}?start={user_id}"
    
    txt = (
        f"💰 <b>User Wallet & Profile</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
        f"💵 <b>Balance:</b> <b>${balance_usd:.4f}</b>\n"
        f"🇧🇩 <b>Approx BDT:</b> {balance_bdt:.2f} Taka\n"
        f"📈 <b>Total Earned:</b> ${total_earned:.4f}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Total Referrals:</b> {total_refs} Users\n\n"
        f"🔗 <b>Your Referral Link:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"🎁 <b>LIFETIME Rewards System:</b>\n"
        f"• Earn <b>0.15 BDT (${REWARD_PER_OTP})</b> for EVERY OTP your friend receives.\n"
        f"• No Limits! Lifetime Commission.\n"
        f"• Minimum Withdrawal: {MIN_WITHDRAW_BDT} BDT"
    )
    kb = [[InlineKeyboardButton("💸 Withdraw Now", callback_data="req_withdraw")]]
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

# ==============================================================================
# CATEGORY & CONSOLE API LOGIC (LIVE DATA FETCH)
# ==============================================================================

async def start_category_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("📘 Facebook", callback_data="cat_facebook"), InlineKeyboardButton("💬 WhatsApp", callback_data="cat_whatsapp")]
    ]
    txt = (
        "📱 <b>Select Service Category:</b>\n\n"
        "<i>Choose the app you need a number for.\n"
        "We will show you countries where OTPs are arriving right now!</i>"
    )
    if update.callback_query:
        try: await update.callback_query.message.delete()
        except: pass
        await context.bot.send_message(chat_id=update.effective_chat.id, text=txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

async def handle_category_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    category = query.data.split('_')[1].lower()
    chat_id = query.message.chat_id
    
    try: await query.message.delete()
    except: pass
    
    loading_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ <i>Fetching live country data from server... Please wait.</i>", parse_mode=ParseMode.HTML)
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_CONSOLE, headers=HEADERS, timeout=10) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                data = await resp.json()
                
        logs = data.get('data', {}).get('logs', [])
        countries = {}
        
        for log in logs:
            app_name = log.get('app_name', '').lower()
            if (category == 'other') or (category in app_name):
                c_name = log.get('country')
                r_val = log.get('range')
                if c_name and r_val and c_name not in countries:
                    countries[c_name] = r_val
        
        try: await loading_msg.delete()
        except: pass

        if not countries:
            err_msg = (
                f"❌ <b>No live numbers available for {category.title()} right now.</b>\n\n"
                f"<i>Please try another category or check back in a few minutes.</i>"
            )
            await context.bot.send_message(chat_id=chat_id, text=err_msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="go_cat")]]), parse_mode=ParseMode.HTML)
            return
            
        kb = []
        row = []
        for c_name, r_val in countries.items():
            flag = get_flag(c_name)
            row.append(InlineKeyboardButton(f"{flag} {c_name}", callback_data=f"rng_{r_val}"))
            if len(row) == 2:
                kb.append(row)
                row = []
        if row: 
            kb.append(row)
            
        kb.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="go_cat")])
        
        txt = (
            f"🌍 <b>Select Country for {category.title()}:</b>\n\n"
            f"<i>Live numbers are currently arriving from these countries. Click one to generate a number:</i>"
        )
        await context.bot.send_message(chat_id=chat_id, text=txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        
    except Exception as e:
        logger.error(f"Console fetch error: {e}")
        try: await loading_msg.delete()
        except: pass
        await context.bot.send_message(chat_id=chat_id, text="❌ <b>Failed to fetch live data!</b>\nServer might be busy.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="go_cat")]]), parse_mode=ParseMode.HTML)

# ==============================================================================
# WITHDRAWAL SYSTEM
# ==============================================================================

async def start_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    if user[1] < MIN_WITHDRAW_USD:
        await update.message.reply_text(
            f"❌ <b>Insufficient Balance!</b>\n\n"
            f"Your Balance: <b>${user[1]:.4f}</b>\n"
            f"Min Withdraw: <b>{MIN_WITHDRAW_BDT} BDT (${MIN_WITHDRAW_USD:.3f})</b>", 
            parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END
    
    kb = [["bKash", "Nagad"], ["Binance"], ["🔙 Cancel"]]
    await update.message.reply_text(
        "💸 <b>Withdrawal Request</b>\n\nPlease select your payment method:", 
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), 
        parse_mode=ParseMode.HTML
    )
    return SELECT_METHOD

async def select_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    method = update.message.text
    if method == "🔙 Cancel":
        await show_main_menu(update, context)
        return ConversationHandler.END
        
    context.user_data['wd_method'] = method
    await update.message.reply_text(
        f"🏦 <b>{method}</b> selected.\n\n✍️ <b>Please enter your Wallet Number / Binance ID:</b>", 
        reply_markup=ReplyKeyboardRemove(), 
        parse_mode=ParseMode.HTML
    )
    return ENTER_ADDRESS

async def process_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text
    user_id = update.effective_user.id
    method = context.user_data['wd_method']
    
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        bal = c.fetchone()[0]
        
        if bal < MIN_WITHDRAW_USD: 
            await show_main_menu(update, context)
            return ConversationHandler.END
        
        amt_usd = bal
        amt_bdt = amt_usd * USD_TO_BDT_RATE
        
        c.execute("UPDATE users SET balance = 0 WHERE user_id=?", (user_id,))
        c.execute("INSERT INTO withdrawals (user_id, method, address, amount_usd, amount_bdt) VALUES (?, ?, ?, ?, ?)", 
                 (user_id, method, address, amt_usd, amt_bdt))
        wd_id = c.lastrowid
        conn.commit()
    
    await update.message.reply_text(
        f"✅ <b>Request Submitted!</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID: #{wd_id}\n"
        f"💰 Amount: <b>{amt_bdt:.2f} BDT</b>\n"
        f"⏳ Status: Pending Approval", 
        parse_mode=ParseMode.HTML
    )
    await show_main_menu(update, context)
    
    try:
        admin_msg = (
            f"🔔 <b>New Withdrawal Request</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 User: <code>{user_id}</code>\n"
            f"🆔 ID: #{wd_id}\n"
            f"💰 Amount: ${amt_usd:.4f} ({amt_bdt:.2f} BDT)\n"
            f"🏦 Method: {method}\n"
            f"📝 Address: <code>{address}</code>"
        )
        kb = [
            [InlineKeyboardButton("✅ Approve", callback_data=f"wd_approve_{wd_id}_{user_id}"), 
             InlineKeyboardButton("❌ Reject", callback_data=f"wd_reject_{wd_id}_{user_id}")]
        ]
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Admin Notify Error: {e}")
        
    return ConversationHandler.END

async def cancel_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)
    return ConversationHandler.END

# ==============================================================================
# ADMIN SYSTEM (Panel, Add Balance, Status, Ban)
# ==============================================================================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
        
    txt = (
        f"🔐 <b>ADVANCED ADMIN PANEL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Commands List:</b>\n"
        f"🟢 <code>/status</code> - Check Bot & Server Stats\n"
        f"💰 <code>/addbalance &lt;id&gt; &lt;amt&gt;</code> - Give Balance\n"
        f"📢 <code>/broadcast &lt;msg&gt;</code> - Message all users\n"
        f"🔎 <code>/userinfo &lt;id&gt;</code> - Check user details\n"
        f"🚫 <code>/ban &lt;id&gt;</code> - Ban a user\n"
        f"✅ <code>/unban &lt;id&gt;</code> - Unban a user\n"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

async def admin_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    uptime = datetime.datetime.now() - START_TIME
    
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        t_users = c.fetchone()[0]
        c.execute("SELECT SUM(balance) FROM users")
        t_bal = c.fetchone()[0] or 0.0
        c.execute("SELECT COUNT(*) FROM withdrawals WHERE status='pending'")
        t_pend = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM otp_history")
        t_otps = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM users WHERE is_banned=1")
        t_banned = c.fetchone()[0]
        
    txt = (
        f"📊 <b>LIVE SYSTEM STATUS</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏱ <b>Uptime:</b> {str(uptime).split('.')[0]}\n"
        f"👥 <b>Total Users:</b> {t_users}\n"
        f"📩 <b>Total OTPs Processed:</b> {t_otps}\n"
        f"💰 <b>Total Balance Held:</b> ${t_bal:.4f}\n"
        f"⏳ <b>Pending Withdraws:</b> {t_pend}\n"
        f"🚫 <b>Banned Users:</b> {t_banned}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ Everything is running smoothly!"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

async def user_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        u_id = int(context.args[0])
        user = get_user(u_id)
        if not user:
            return await update.message.reply_text("❌ User not found in database.")
            
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM users WHERE referrer_id=?", (u_id,))
            refs = c.fetchone()[0]
            
        banned_text = "🔴 YES" if user[6] == 1 else "🟢 NO"
            
        txt = (
            f"🔎 <b>USER INFORMATION</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>ID:</b> <code>{user[0]}</code>\n"
            f"💰 <b>Balance:</b> ${user[1]:.4f}\n"
            f"📈 <b>Total Earned:</b> ${user[4]:.4f}\n"
            f"👥 <b>Total Referrals:</b> {refs}\n"
            f"📩 <b>OTPs Received:</b> {user[3]}\n"
            f"🗓 <b>Joined:</b> {user[5]}\n"
            f"🚫 <b>Banned:</b> {banned_text}"
        )
        await update.message.reply_text(txt, parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text("⚠️ <b>Usage:</b> `/userinfo <user_id>`", parse_mode=ParseMode.MarkdownV2)

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        u_id = int(context.args[0])
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET is_banned = 1 WHERE user_id=?", (u_id,))
            conn.commit()
        await update.message.reply_text(f"🚫 <b>User <code>{u_id}</code> has been BANNED.</b>", parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text("⚠️ <b>Usage:</b> `/ban <user_id>`", parse_mode=ParseMode.MarkdownV2)

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        u_id = int(context.args[0])
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET is_banned = 0 WHERE user_id=?", (u_id,))
            conn.commit()
        await update.message.reply_text(f"✅ <b>User <code>{u_id}</code> has been UNBANNED.</b>", parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text("⚠️ <b>Usage:</b> `/unban <user_id>`", parse_mode=ParseMode.MarkdownV2)

async def add_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        u_id = int(context.args[0])
        amt = float(context.args[1])
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amt, u_id))
            conn.commit()
        await update.message.reply_text(f"✅ Added ${amt} to user {u_id}")
        try:
            await context.bot.send_message(chat_id=u_id, text=f"🎉 <b>Admin has added ${amt} to your balance!</b>\nCheck your Wallet.", parse_mode=ParseMode.HTML)
        except Exception: pass
    except Exception:
        await update.message.reply_text("⚠️ <b>Usage:</b>\n`/addbalance <user_id> <amount>`", parse_mode=ParseMode.MarkdownV2)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    msg = ' '.join(context.args)
    if not msg:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id FROM users")
        users = c.fetchall()
    
    await update.message.reply_text(f"📢 Sending broadcast to {len(users)} users...")
    count = 0
    for row in users:
        try:
            await context.bot.send_message(row[0], f"📢 <b>Announcement:</b>\n\n{msg}", parse_mode=ParseMode.HTML)
            count += 1
            await asyncio.sleep(0.05)
        except: pass
    await update.message.reply_text(f"✅ Successfully sent to {count} users.")

# ==============================================================================
# MAIN MESSAGE ROUTER & 2FA GENERATOR LOGIC
# ==============================================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_ban_middleware(update, context): return
    
    text = update.message.text
    user_data = context.user_data
    user_id = update.effective_user.id
    
    if text == "📱 Get Number":
        if not await check_subscription(user_id, context.bot):
            await send_join_prompt(update, context)
            return
        await start_category_selection(update, context)
    
    elif text == "🔐 Get 2FA Code":
        user_data['state'] = 'WAITING_FOR_2FA'
        await update.message.reply_text(
            "🔐 <b>2FA Code Generator</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🔑 Send me your Secret Key:\n"
            "<i>(Spaces will be removed automatically)</i>", 
            parse_mode=ParseMode.HTML
        )
    
    elif user_data.get('state') == 'WAITING_FOR_2FA':
        key = text.replace(" ", "").strip()
        msg = await update.message.reply_text("⏳ Generating 2FA Code...")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(API_2FA.format(key), timeout=10) as resp:
                    if resp.status == 200:
                        res_json = await resp.json()
                        code = res_json.get('code')
                        if code:
                            await msg.edit_text(
                                f"✅ <b>2FA Code Generated!</b>\n"
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"🔢 <b>Code:</b> <code>{code}</code>\n\n"
                                f"<i>Tap the code above to copy it.</i>", 
                                parse_mode=ParseMode.HTML
                            )
                        else:
                            await msg.edit_text("❌ <b>Error:</b> Invalid Secret Key.", parse_mode=ParseMode.HTML)
                    else:
                        await msg.edit_text("❌ <b>API Error!</b> Unable to generate code right now.", parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"2FA Generator Error: {e}")
            await msg.edit_text("❌ <b>Failed!</b>\nInvalid Secret Key or API is currently down.", parse_mode=ParseMode.HTML)
            
        user_data['state'] = None
            
    elif text == "💰 Wallet / Refer": 
        await wallet_page(update, context)
    elif text == "💸 Withdraw": 
        await start_withdraw(update, context)
    else:
        await show_main_menu(update, context)

# ==============================================================================
# BUTTON HANDLER (WITH AUTO MESSAGE DELETE)
# ==============================================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_ban_middleware(update, context): return
    
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    
    if data == "check_join":
        if await check_subscription(user_id, context.bot):
            try: await query.message.delete()
            except: pass
            await show_main_menu(query, context)
        else: 
            await query.answer("⚠️ Not joined yet! Please join the channels.", show_alert=True)
            
    elif data == "req_withdraw":
        await query.answer("💸 Use the Withdraw button from the Main Menu.", show_alert=True)
        
    elif data.startswith("cat_"):
        await handle_category_click(update, context)
        
    elif data == "go_cat":
        await start_category_selection(update, context)
        
    elif data.startswith("rng_"):
        range_val = data.split("_")[1]
        await get_number_api(update, context, range_val)
        
    elif data == "change_num":
        rng = context.user_data.get('range')
        if rng: 
            await get_number_api(update, context, rng)
        else:
            try: await query.message.delete()
            except: pass
            await context.bot.send_message(chat_id=query.message.chat_id, text="⚠️ Session expired. Please generate a new number.")
            
    elif data == "refresh_inbox":
        await query.answer("🔄 Auto-checking is active... Please wait.", show_alert=False)
        
    elif data == "go_main":
        try: await query.message.delete()
        except: pass
        await show_main_menu(update, context)
        
    elif data.startswith("wd_") and user_id == ADMIN_ID:
        parts = data.split('_')
        action = parts[1]
        wd_id = int(parts[2])
        target_user = int(parts[3])
        
        with db_pool.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT status, amount_usd FROM withdrawals WHERE id=?", (wd_id,))
            res = c.fetchone()
            
            if not res or res[0] != 'pending': 
                return await query.answer("⚠️ Already Processed!", show_alert=True)
            
            if action == "approve":
                c.execute("UPDATE withdrawals SET status='approved' WHERE id=?", (wd_id,))
                await query.message.edit_text(f"{query.message.text}\n\n✅ <b>APPROVED</b>", parse_mode=ParseMode.HTML)
                try: 
                    await context.bot.send_message(
                        chat_id=target_user, 
                        text=f"✅ <b>Withdrawal #{wd_id} Approved!</b>\nFunds have been sent.", 
                        parse_mode=ParseMode.HTML
                    )
                except: pass
                    
            elif action == "reject":
                c.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wd_id,))
                c.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (res[1], target_user))
                await query.message.edit_text(f"{query.message.text}\n\n❌ <b>REJECTED & REFUNDED</b>", parse_mode=ParseMode.HTML)
                try: 
                    await context.bot.send_message(
                        chat_id=target_user, 
                        text=f"❌ <b>Withdrawal #{wd_id} Rejected!</b>\nFunds have been refunded to your wallet.", 
                        parse_mode=ParseMode.HTML
                    )
                except: pass
            conn.commit()

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================

def main():
    init_db()
    
    app = Application.builder().token(TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 Withdraw$"), start_withdraw)],
        states={
            SELECT_METHOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_method)],
            ENTER_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_withdrawal)]
        },
        fallbacks=[CommandHandler("cancel", cancel_withdraw)]
    )
    
    # Handlers Registration
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("status", admin_status))
    app.add_handler(CommandHandler("userinfo", user_info))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("addbalance", add_balance))
    app.add_handler(CommandHandler("broadcast", broadcast))
    
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("✅ VERSION 3.1 (CLEAN CODE) STARTED SUCCESSFULLY...")
    app.run_polling()

if __name__ == "__main__":
    main()