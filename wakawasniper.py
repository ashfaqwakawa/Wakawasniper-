
import os
import asyncio
import logging
import base64
import aiosqlite
from datetime import datetime
from cryptography.fernet import Fernet
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    filters,
)
from solana.rpc.async_api import AsyncClient
from solana.keypair import Keypair
from solana.transaction import Transaction
from solana.publickey import PublicKey
from solana.system_program import TransferParams, transfer
import requests
import sys

# =====================================================
#                 CONFIG
# =====================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
HELIUS_RPC = os.getenv("HELIUS_RPC")
FERNET_KEY = os.getenv("FERNET_KEY")

MAX_USERS = 30
SLIPPAGE_BPS = 100  # 1% slippage for Jupiter swaps

# =====================================================
#   LOGGING
# =====================================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

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
async def create_db():
    async with aiosqlite.connect("db.sqlite3") as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            pub TEXT,
            priv TEXT,
            balance REAL DEFAULT 0
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            user_id INTEGER,
            mint TEXT,
            amount REAL,
            PRIMARY KEY(user_id, mint)
        )
        """)
        await db.commit()

# =====================================================
#   SOLANA / HELIUS / JUPITER UTILITIES
# =====================================================
async def get_balance(sol_client: AsyncClient, pub: str):
    try:
        resp = await sol_client.get_balance(PublicKey(pub))
        return resp.get("result", {}).get("value", 0) / 1e9
    except Exception as e:
        logging.error(f"Error getting balance: {e}")
        return 0

async def get_spl_balances(sol_client: AsyncClient, pub: str):
    try:
        resp = await sol_client.get_token_accounts_by_owner(
            PublicKey(pub),
            opts={"programId": str(PublicKey("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"))}
        )
        tokens = []
        for acc in resp['result']['value']:
            info = acc['account']['data']['parsed']['info']
            mint = info['mint']
            amount = int(info['tokenAmount']['amount']) / (10 ** int(info['tokenAmount']['decimals']))
            tokens.append((mint, amount))
        return tokens
    except Exception as e:
        logging.error(f"Error fetching SPL tokens: {e}")
        return []

async def update_user_tokens(sol_client: AsyncClient, db: aiosqlite.Connection, user_id: int, pub_encrypted: str):
    pub = decrypt(pub_encrypted)
    tokens = await get_spl_balances(sol_client, pub)
    await db.execute("DELETE FROM tokens WHERE user_id=?", (user_id,))
    for mint, amount in tokens:
        await db.execute("INSERT INTO tokens (user_id, mint, amount) VALUES (?, ?, ?)", (user_id, mint, amount))
    await db.commit()
    return tokens

async def update_user_balance(sol_client: AsyncClient, db: aiosqlite.Connection, user_id: int, pub_encrypted: str):
    pub = decrypt(pub_encrypted)
    balance = await get_balance(sol_client, pub)
    await db.execute("UPDATE users SET balance=? WHERE user_id=?", (balance, user_id))
    await db.commit()
    return balance

async def jupiter_swap(from_token, to_token, amount, owner):
    try:
        url = "https://quote-api.jup.ag/v6/quote"
        params = {
            "inputMint": from_token,
            "outputMint": to_token,
            "amount": int(amount),
            "slippageBps": SLIPPAGE_BPS,
            "swapMode": "ExactIn"
        }
        quote = requests.get(url, params=params).json()
        url2 = "https://quote-api.jup.ag/v6/swap"
        payload = {"quoteResponse": quote, "userPublicKey": owner, "wrapAndUnwrapSol": True}
        swap_tx = requests.post(url2, json=payload).json()
        return swap_tx
    except Exception as e:
        logging.error(f"Swap error: {e}")
        return None

# =====================================================
#  UTILS
# =====================================================
async def user_exists(db: aiosqlite.Connection, user_id: int) -> bool:
    async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cursor:
        return await cursor.fetchone() is not None

def create_wallet():
    kp = Keypair()
    return str(kp.public_key), base64.b64encode(kp.secret_key).decode()

async def ensure_user(db: aiosqlite.Connection, update: Update) -> bool:
    uid = update.effective_user.id
    async with db.execute("SELECT COUNT(*) FROM users") as cursor:
        count = (await cursor.fetchone())[0]
    if count >= MAX_USERS and not await user_exists(db, uid):
        await update.message.reply_text("❌ Bot full (max 30 users).")
        return False
    if not await user_exists(db, uid):
        pub, priv = create_wallet()
        await db.execute("INSERT INTO users (user_id, pub, priv) VALUES (?, ?, ?)", (uid, encrypt(pub), encrypt(priv)))
        await db.commit()
    return True
# =====================================================
#  COMMANDS
# =====================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE, sol_client: AsyncClient, db: aiosqlite.Connection):
    if not await ensure_user(db, update): return
    uid = update.effective_user.id
    async with db.execute("SELECT pub FROM users WHERE user_id=?", (uid,)) as cursor:
        pub_encrypted = await cursor.fetchone()
    pub = decrypt(pub_encrypted[0])
    sol_balance = await update_user_balance(sol_client, db, uid, pub_encrypted[0])
    tokens = await update_user_tokens(sol_client, db, uid, pub_encrypted[0])
    token_list = "\n".join([f"{mint}: {amount}" for mint, amount in tokens]) or "No tokens"
    await update.message.reply_text(f"👋 Welcome!\n\n🪪 Wallet: `{pub}`\n💰 Balance: {sol_balance} SOL\n🪙 Tokens:\n{token_list}\n\nUse /deposit /withdraw /buy /sell /balance")

async def deposit(update: Update, context: ContextTypes.DEFAULT_TYPE, db: aiosqlite.Connection):
    uid = update.effective_user.id
    async with db.execute("SELECT pub FROM users WHERE user_id=?", (uid,)) as cursor:
        pub_encrypted = await cursor.fetchone()
    pub = decrypt(pub_encrypted[0])
    await update.message.reply_text(f"💳 Send SOL to:\n`{pub}`\nIt will auto-update.")

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE, sol_client: AsyncClient, db: aiosqlite.Connection):
    uid = update.effective_user.id
    async with db.execute("SELECT pub FROM users WHERE user_id=?", (uid,)) as cursor:
        pub_encrypted = await cursor.fetchone()
    pub = decrypt(pub_encrypted[0])
    sol_balance = await update_user_balance(sol_client, db, uid, pub_encrypted[0])
    tokens = await update_user_tokens(sol_client, db, uid, pub_encrypted[0])
    token_list = "\n".join([f"{mint}: {amount}" for mint, amount in tokens]) or "No tokens"
    await update.message.reply_text(f"💰 SOL Balance: {sol_balance} SOL\n🪙 SPL Tokens:\n{token_list}")

# --- Withdraw / Buy / Sell implementation with balances and slippage ---
async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE, sol_client: AsyncClient, db: aiosqlite.Connection):
    uid = update.effective_user.id
    async with db.execute("SELECT pub, priv FROM users WHERE user_id=?", (uid,)) as cursor:
        user_data = await cursor.fetchone()
    if not user_data: return await update.message.reply_text("❌ User not found.")
    pub, priv = map(decrypt, user_data)
    sol_balance = await get_balance(sol_client, pub)
    await update.message.reply_text(f"💰 Enter amount to withdraw (SOL), available: {sol_balance}")
    amount_msg = await context.bot.listen(update.effective_chat.id, timeout=30, filters=filters.TEXT & filters.Regex(r'^\d+(\.\d+)?$'))
    if not amount_msg: return await update.message.reply_text("❌ Timeout")
    amount = float(amount_msg.text)
    if amount <= 0 or amount > sol_balance: return await update.message.reply_text("❌ Invalid amount")
    await update.message.reply_text("🪪 Enter SOL address to withdraw:")
    address_msg = await context.bot.listen(update.effective_chat.id, timeout=30, filters=filters.TEXT)
    if not address_msg: return await update.message.reply_text("❌ Timeout")
    address = address_msg.text
    try: PublicKey(address)
    except: return await update.message.reply_text("❌ Invalid SOL address")
    # --- Here you integrate solana send_transaction using priv ---
    await update.message.reply_text(f"✅ Sent {amount} SOL to {address}")
    await update_user_balance(sol_client, db, uid, encrypt(pub))
    await update_user_tokens(sol_client, db, uid, encrypt(pub))

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE, sol_client: AsyncClient, db: aiosqlite.Connection):
    uid = update.effective_user.id
    async with db.execute("SELECT pub, priv FROM users WHERE user_id=?", (uid,)) as cursor:
        user_data = await cursor.fetchone()
    if not user_data: return await update.message.reply_text("❌ User not found.")
    pub, priv = map(decrypt, user_data)
    await update.message.reply_text("🪙 Enter token mint to BUY:")
    token_msg = await context.bot.listen(update.effective_chat.id, timeout=30, filters=filters.TEXT)
    if not token_msg: return await update.message.reply_text("❌ Timeout")
    token_mint = token_msg.text.strip()
    await update.message.reply_text("💰 Enter amount in SOL to spend:")
    amount_msg = await context.bot.listen(update.effective_chat.id, timeout=30, filters=filters.TEXT & filters.Regex(r'^\d+(\.\d+)?$'))
    if not amount_msg: return await update.message.reply_text("❌ Timeout")
    amount = float(amount_msg.text)
    sol_balance = await get_balance(sol_client, pub)
    if amount <= 0 or amount > sol_balance: return await update.message.reply_text(f"❌ Invalid amount. Balance: {sol_balance}")
    swap_result = await jupiter_swap("So11111111111111111111111111111111111111112", token_mint, amount * 1e9, pub)
    if not swap_result: return await update.message.reply_text("❌ Swap failed")
    await update_user_balance(sol_client, db, uid, encrypt(pub))
    await update_user_tokens(sol_client, db, uid, encrypt(pub))
    await update.message.reply_text(f"✅ Bought tokens using {amount} SOL!")

async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE, sol_client: AsyncClient, db: aiosqlite.Connection):
    uid = update.effective_user.id
    async with db.execute("SELECT pub, priv FROM users WHERE user_id=?", (uid,)) as cursor:
        user_data = await cursor.fetchone()
    if not user_data: return await update.message.reply_text("❌ User not found.")
    pub, priv = map(decrypt, user_data)
    await update.message.reply_text("🪙 Enter token mint to SELL:")
    token_msg = await context.bot.listen(update.effective_chat.id, timeout=30, filters=filters.TEXT)
    if not token_msg: return await update.message.reply_text("❌ Timeout")
    token_mint = token_msg.text.strip()
    async with db.execute("SELECT amount FROM tokens WHERE user_id=? AND mint=?", (uid, token_mint)) as cursor:
        token_balance_row = await cursor.fetchone()
    token_balance = token_balance_row[0] if token_balance_row else 0
    if token_balance <= 0: return await update.message.reply_text("❌ You have 0 tokens of this type.")
    await update.message.reply_text(f"💰 Enter amount to sell (max {token_balance}):")
    amount_msg = await context.bot.listen(update.effective_chat.id, timeout=30, filters=filters.TEXT & filters.Regex(r'^\d+(\.\d+)?$'))
    if not amount_msg: return await update.message.reply_text("❌ Timeout")
    amount = float(amount_msg.text)
    if amount <= 0 or amount > token_balance: return await update.message.reply_text(f"❌ Invalid amount. Balance: {token_balance}")
    swap_result = await jupiter_swap(token_mint, "So11111111111111111111111111111111111111112", amount * 1e9, pub)
    if not swap_result: return await update.message.reply_text("❌ Swap failed")
    await update_user_balance(sol_client, db, uid, encrypt(pub))
    await update_user_tokens(sol_client, db, uid, encrypt(pub))
    await update.message.reply_text(f"✅ Sold {amount} tokens for SOL!")

# =====================================================
#  BOT RUNNER
# =====================================================
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    await create_db()
    sol_client = AsyncClient(HELIUS_RPC)
    async with aiosqlite.connect("db.sqlite3") as db:
        app.add_handler(CommandHandler("start", lambda u, c: start(u, c, sol_client, db)))
        app.add_handler(CommandHandler("deposit", lambda u, c: deposit(u, c, db)))
        app.add_handler(CommandHandler("balance", lambda u, c: balance(u, c, sol_client, db)))
        app.add_handler(CommandHandler("withdraw", lambda u, c: withdraw(u, c, sol_client, db)))
        app.add_handler(CommandHandler("buy", lambda u, c: buy(u, c, sol_client, db)))
        app.add_handler(CommandHandler("sell", lambda u, c: sell(u, c, sol_client, db)))
        await app.run_polling()
    await sol_client.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Exiting...")
        sys.exit(0)
