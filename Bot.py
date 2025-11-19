import os
import asyncio
import logging
import json
import base64
import sqlite3
from datetime import datetime
from cryptography.fernet import Fernet
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from solana.rpc.async_api import AsyncClient
from solana.keypair import Keypair
from solana.transaction import Transaction
from solana.publickey import PublicKey
import requests

# =====================================================
#                 CONFIG
# =====================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_RPC = os.getenv("HELIUS_RPC")
FERNET_KEY = os.getenv("FERNET_KEY")

MAX_USERS = 30

# =====================================================
#   ENCRYPTION
# =====================================================

fernet = Fernet(FERNET_KEY)

def encrypt(data: str):
    return fernet.encrypt(data.encode()).decode()

def decrypt(data: str):
    return fernet.decrypt(data.encode()).decode()

# =====================================================
#   DATABASE
# =====================================================

db = sqlite3.connect("db.sqlite3")
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    pub TEXT,
    priv TEXT,
    balance REAL DEFAULT 0
)
""")
db.commit()

# =====================================================
#   HELIUS / JUPITER API
# =====================================================

sol_client = AsyncClient(HELIUS_RPC)

async def get_balance(pub):
    try:
        balance = await sol_client.get_balance(PublicKey(pub))
        return balance["result"]["value"] / 1e9
    except:
        return 0

async def jupiter_swap(from_token, to_token, amount, owner):
    try:
        url = "https://quote-api.jup.ag/v6/quote"
        params = {
            "inputMint": from_token,
            "outputMint": to_token,
            "amount": int(amount),
            "slippageBps": 100,
            "swapMode": "ExactIn"
        }
        quote = requests.get(url, params=params).json()

        url2 = "https://quote-api.jup.ag/v6/swap"
        payload = {
            "quoteResponse": quote,
            "userPublicKey": owner,
            "wrapAndUnwrapSol": True
        }
        swap_tx = requests.post(url2, json=payload).json()
        return swap_tx
    except Exception as e:
        print("Swap error:", e)
        return None

# =====================================================
#  UTILS
# =====================================================

def user_exists(user_id):
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    return cursor.fetchone() is not None

def create_wallet():
    kp = Keypair()
    pub = str(kp.public_key)
    priv = base64.b64encode(kp.secret_key).decode()
    return pub, priv

async def ensure_user(update: Update):
    uid = update.effective_user.id

    cursor.execute("SELECT COUNT(*) FROM users")
    count = cursor.fetchone()[0]
    if count >= MAX_USERS and not user_exists(uid):
        await update.message.reply_text("❌ Bot full (max 30 users).")
        return False

    if not user_exists(uid):
        pub, priv = create_wallet()
        cursor.execute("INSERT INTO users (user_id, pub, priv) VALUES (?, ?, ?)",
                       (uid, encrypt(pub), encrypt(priv)))
        db.commit()

    return True

# =====================================================
#  COMMANDS
# =====================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_user(update):
        return

    uid = update.effective_user.id
    cursor.execute("SELECT pub FROM users WHERE user_id=?", (uid,))
    pub = decrypt(cursor.fetchone()[0])

    bal = await get_balance(pub)

    await update.message.reply_text(
        f"👋 Welcome!\n\n"
        f"Your internal wallet:\n"
        f"🪪 Address: `{pub}`\n"
        f"💰 Balance: {bal} SOL\n\n"
        "Use /deposit , /withdraw , /buy , /sell"
    )

async def deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cursor.execute("SELECT pub FROM users WHERE user_id=?", (uid,))
    pub = decrypt(cursor.fetchone()[0])
    await update.message.reply_text(f"💳 Send SOL to:\n`{pub}`\nIt will auto-update.")

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cursor.execute("SELECT pub FROM users WHERE user_id=?", (uid,))
    pub = decrypt(cursor.fetchone()[0])
    bal = await get_balance(pub)
    await update.message.reply_text(f"💰 Balance: {bal} SOL")

async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Withdraw not yet implemented.")

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Buy not yet implemented.")

async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Sell not yet implemented.")

# =====================================================
#  BOT RUNNER
# =====================================================

async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("deposit", deposit))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("sell", sell))

    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
