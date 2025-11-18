
# wakawa_aiogram_bot.py
# Wakawa Sniper Bot — aiogram + solana
# Single-file: per-user wallets, deposit poller, trending alerts, buy/sell via Jupiter (optimistic)

import os, asyncio, sqlite3, base64, logging, time, json, base58
from datetime import datetime, timedelta
from decimal import Decimal

import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor

from solana.rpc.async_api import AsyncClient
from solana.keypair import Keypair
from solana.publickey import PublicKey
from solana.transaction import Transaction
from solana.system_program import TransferParams, transfer

# ---------------- CONFIG - REPLACE LOCALLY ----------------
BOT_TOKEN = "TOKEN_REGENERATED_OK" # <- replace locally
ADMIN_ID = 6216659337 # <- replace locally
CHANNEL_ID = "@wakawasniper" # <- replace locally (private channel)
HELIUS_API_KEY = "HELIUS_KEY_OK" # <- replace locally
RPC_URL = f"https://rpc.helius.xyz/?api-key={HELIUS_API_KEY}"
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"
DEXSCREENER_TREND = "https://api.dexscreener.com/latest/dex/trending"
COINGECKO_PRICE = "https://api.coingecko.com/api/v3/simple/price"
USDT_MINT = "Es9vMFrzaCERmJf8G3s6uKp7c3y3uM5nCzq3f6nWbqG"
MIN_DEPOSIT_USD = 1.0
MAX_USERS = 30
POLL_INTERVAL = 12
TREND_THROTTLE_SEC = 100
# ----------------------------------------------------------

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("wakawa_aiogram")

DB = "wakawa_aiogram.db"
os.makedirs(".snapshots", exist_ok=True)

# ---------------- DB ----------------
def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        created_at TEXT,
        pubkey TEXT,
        privkey TEXT,
        balance_sol REAL DEFAULT 0,
        month_profit REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        type TEXT,
        token TEXT,
        amount REAL,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS alerts_sent (id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, created_at TEXT);
    ''')
    conn.commit()
    conn.close()

def get_conn():
    return sqlite3.connect(DB, timeout=30)

# ---------------- utils ----------------
async def get_sol_price():
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(COINGECKO_PRICE, params={"ids":"solana","vs_currencies":"usd"}, timeout=10) as r:
                j = await r.json()
                return float(j.get("solana",{}).get("usd",0.0))
    except Exception as e:
        log.warning("coingecko fail: %s", e)
        return 0.0

def fmt(n):
    try:
        return f"{float(n):,.6f}".rstrip('0').rstrip('.')
    except:
        return str(n)

def generate_kp():
    kp = Keypair.generate()
    pub = str(kp.public_key)
    priv = base58.b58encode(kp.secret_key).decode()
    return pub, priv

def decode_priv(priv_b58):
    return base58.b58decode(priv_b58)
# ---------------- user helpers ----------------
def user_count():
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users"); n = c.fetchone()[0]; conn.close(); return n

def create_user(user_id, username):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
    if c.fetchone():
        conn.close(); return False
    if user_count() >= MAX_USERS:
        conn.close(); raise RuntimeError("User limit reached")
    pub, priv = generate_kp()
    c.execute("INSERT INTO users (user_id, username, created_at, pubkey, privkey) VALUES (?,?,?,?,?)",
              (user_id, username or "", datetime.utcnow().isoformat(), pub, priv))
    conn.commit(); conn.close(); return True

def get_user(user_id):
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT user_id, username, pubkey, privkey, balance_sol, month_profit FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone(); conn.close(); return row

def update_balance(user_id, sol):
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE users SET balance_sol=? WHERE user_id=?", (sol, user_id))
    conn.commit(); conn.close()

def add_trade(user_id, typ, token, amount):
    conn = get_conn(); c = conn.cursor()
    c.execute("INSERT INTO trades (user_id, type, token, amount, created_at) VALUES (?,?,?,?,?)",
              (user_id, typ, token, amount, datetime.utcnow().isoformat()))
    conn.commit(); conn.close()

def add_month_profit(user_id, sol):
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE users SET month_profit=month_profit+? WHERE user_id=?", (sol, user_id))
    conn.commit(); conn.close()

# ---------------- RPC helpers ----------------
async def get_balance_rpc(pubkey):
    try:
        async with AsyncClient(RPC_URL) as client:
            resp = await client.get_balance(PublicKey(pubkey))
            if hasattr(resp, 'value'):
                lam = resp.value
            else:
                lam = resp.get('result', {}).get('value', 0)
            return (lam or 0) / 1e9
    except Exception as e:
        log.exception("get_balance_rpc: %s", e)
        return 0.0

async def get_token_balance_rpc(pubkey, mint):
    try:
        payload = {"jsonrpc":"2.0","id":1,"method":"getTokenAccountsByOwner",
                   "params":[pubkey, {"mint": mint}, {"encoding":"jsonParsed"}]}
        async with aiohttp.ClientSession() as s:
            async with s.post(RPC_URL, json=payload, timeout=12) as r:
                j = await r.json()
                accs = j.get('result', {}).get('value', []) or []
                total = 0.0
                for a in accs:
                    parsed = a.get('account', {}).get('data', {}).get('parsed', {}).get('info', {})
                    amt = parsed.get('tokenAmount', {}).get('uiAmount')
                    if amt is None:
                        raw = parsed.get('tokenAmount', {}).get('amount','0'); dec = parsed.get('tokenAmount', {}).get('decimals',0)
                        try: total += int(raw) / (10**dec)
                        except: pass
                    else:
                        try: total += float(amt)
                        except: pass
                return total
    except Exception as e:
        log.exception("get_token_balance_rpc: %s", e)
        return 0.0
---------------- deposit poller ----------------
async def poll_deposits(bot: Bot):
    log.info("Deposit poller started")
    while True:
        try:
            conn = get_conn(); c = conn.cursor()
            c.execute("SELECT user_id, pubkey, balance_sol FROM users")
            rows = c.fetchall(); conn.close()
            sol_price = await get_sol_price()
            for user_id, pubkey, stored in rows:
                try:
                    on_sol = await get_balance_rpc(pubkey)
                    if on_sol > (stored or 0) + 0.0005:
                        diff = on_sol - (stored or 0)
                        usd = diff * sol_price
                        if usd >= MIN_DEPOSIT_USD - 1e-6:
                            update_balance(user_id, on_sol)
                            try:
                                await bot.send_message(user_id, f"✅ Deposit received: +{fmt(diff)} SOL (~${fmt(usd)})\nBalance: {fmt(on_sol)} SOL")
                            except: pass
                    # USDT snapshot detection
                    usdt = await get_token_balance_rpc(pubkey, USDT_MINT)
                    snap = f".snapshots/{user_id}_usdt"
                    prev = 0.0
                    try:
                        if os.path.exists(snap):
                            prev = float(open(snap).read().strip() or 0.0)
                    except: prev = 0.0
                    if usdt > prev + 1e-8:
                        diff_usdt = usdt - prev
                        if diff_usdt >= MIN_DEPOSIT_USD - 1e-6:
                            sol_equiv = 0.0
                            try:
                                sol_price = await get_sol_price()
                                sol_equiv = diff_usdt / sol_price if sol_price>0 else 0.0
                                u = get_user(user_id)
                                cur_bal = float(u[4] or 0) if u else 0.0
                                new_bal = cur_bal + sol_equiv
                                update_balance(user_id, new_bal)
                                add_trade(user_id, "DEPOSIT_USDT", "USDT", diff_usdt)
                                await bot.send_message(user_id, f"✅ USDT deposit detected: +{fmt(diff_usdt)} USDT\nConverted ≈ {fmt(sol_equiv)} SOL\nNew balance: {fmt(new_bal)} SOL")
                            except: pass
                        try:
                            open(snap, "w").write(str(usdt))
                        except: pass
                    else:
                        try:
                            open(snap, "w").write(str(usdt))
                        except: pass
                except Exception as e:
                    log.exception("per-user deposit err: %s", e)
        except Exception:
            log.exception("poll_deposits main loop")
        await asyncio.sleep(POLL_INTERVAL)

# ---------------- trending poller ----------------
LAST_ALERT = {}
async def fetch_trending():
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(DEXSCREENER_TREND, timeout=10) as r:
                if r.status != 200: return []
                j = await r.json()
                pairs = j.get("pairs", []) or []
                out = []
                for p in pairs:
                    chain = str(p.get("chain","")).lower()
                    if "solana" in chain:
                        base = p.get("baseToken", {})
                        out.append({
                            "mint": base.get("address"),
                            "symbol": base.get("symbol"),

"liquidity": (p.get("liquidity") or {}).get("usd", 0) if isinstance(p.get("liquidity"), dict) else 0,
                            "volume": (p.get("volume") or {}).get("h24", 0) if isinstance(p.get("volume"), dict) else 0
                        })
                return out[:30]
    except Exception as e:
        log.exception("fetch_trending: %s", e)
        return []

async def simple_rug_check(mint):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=8) as r:
                if r.status != 200: return 50, ["unknown"]
                j = await r.json()
                pairs = j.get("pairs",[]) or []
                liq = 0
                for p in pairs:
                    liq = max(liq, (p.get("liquidity") or {}).get("usd",0) if isinstance(p.get("liquidity"), dict) else 0)
                score = 50
                risks = []
                if liq < 1000:
                    score -= 30; risks.append("LOW_LIQ")
                return max(0, min(100, score)), risks
    except Exception as e:
        log.exception("rug: %s", e)
        return 50, ["err"]

async def trending_poller(bot: Bot):
    log.info("Trending poller started")
    while True:
        try:
            trends = await fetch_trending()
            for t in trends:
                mint = t.get("mint")
                if not mint: continue
                last = LAST_ALERT.get(mint, 0)
                if time.time() - last < TREND_THROTTLE_SEC:
                    continue
                score, risks = await simple_rug_check(mint)
                if score >= 30:
                    txt = f"TRENDING: {t.get('symbol')} ({mint})\nLiq: ${int(t.get('liquidity') or 0):,} Vol24h: ${int(t.get('volume') or 0):,}\nScore: {score}/100"
                    kb = InlineKeyboardMarkup().add(InlineKeyboardButton("Buy (quick)", callback_data=f"BUY|{mint}|{t.get('symbol')}"))
                    try:
                        await bot.send_message(CHANNEL_ID, txt, reply_markup=kb)
                    except Exception:
                        pass
                    LAST_ALERT[mint] = time.time()
        except Exception:
            log.exception("trending main loop")
        await asyncio.sleep(45)

# ---------------- Jupiter quote & swap (optimistic) ----------------
async def jupiter_quote(input_mint, output_mint, amount_sol):
    try:
        amt = int(amount_sol * 1e9)
        async with aiohttp.ClientSession() as s:
            async with s.get(JUPITER_QUOTE_API, params={"inputMint": input_mint, "outputMint": output_mint, "amount": amt, "slippageBps": 100}, timeout=10) as r:
                if r.status != 200: return None
                return await r.json()
    except Exception as e:
        log.exception("jup quote: %s", e); return None

async def jupiter_swap(route, user_pubkey):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(JUPITER_SWAP_API, json={"route": route, "userPublicKey": str(user_pubkey), "wrapAndUnwrapSol": True}, timeout=12) as r:
                if r.status != 200: return None
                j = await r.json()
                return j.get("swapTransaction")
    except Exception as e:
        log.exception("jup swap: %s", e); return None

async def perform_buy(user_id, mint, amount_sol, bot: Bot):
    u = get_user(user_id)
    if not u: return "No wallet"
    pub = u[2]; priv = u[3]
    cur_bal = float(u[4] or 0)
    if cur_bal < amount_sol: return f"Low balance: {fmt(cur_bal)} SOL"
    quote = await jupiter_quote("So11111111111111111111111111111111111111112", mint, amount_sol)
    if not quote or not quote.get("data"): return "No route"
    route = quote["data"][0]
    swap_b64 = await jupiter_swap(route, pub)
    if not swap_b64: return "Swap build failed"
    try:
        raw = base64.b64decode(swap_b64)
        tx = Transaction.deserialize(raw)
        secret = decode_priv(priv)
        kp = Keypair.from_secret_key(secret)
        tx.sign(kp)
        async with AsyncClient(RPC_URL) as client:
            resp = await client.send_raw_transaction(tx.serialize())
            txid = resp if not isinstance(resp, dict) else resp.get("result") or str(resp)
        new_bal = cur_bal - amount_sol
        update_balance(user_id, new_bal)
        add_trade(user_id, "BUY", mint, amount_sol)
        add_month_profit(user_id, -amount_sol)
        try:
            await bot.send_message(user_id, f"Buy submitted ✅\nTx: {txid}\nSpent: {fmt(amount_sol)} SOL")
        except: pass
        return f"Buy OK: {txid}"
    except Exception as e:
        log.exception("perform_buy: %s", e)
        return f"Buy failed: {e}"

async def perform_sell(user_id, mint, amount_sol, bot: Bot):
    u = get_user(user_id); 
    if not u: return "No wallet"
    priv = u[3]; pub = u[2]
    quote = await jupiter_quote(mint, "So11111111111111111111111111111111111111112", amount_sol)
    if not quote or not quote.get("data"): return "No sell route"
    route = quote["data"][0]
    swap_b64 = await jupiter_swap(route, pub)
    if not swap_b64: return "Swap build failed"
    try:
        raw = base64.b64decode(swap_b64)
        tx = Transaction.deserialize(raw)
        secret = decode_priv(priv)
        kp = Keypair.from_secret_key(secret)
        tx.sign(kp)
        async with AsyncClient(RPC_URL) as client:
            resp = await client.send_raw_transaction(tx.serialize())
            txid = resp if not isinstance(resp, dict) else resp.get("result") or str(resp)
        # assume we got amount_sol back
        cur_bal = float(u[4] or 0)
        new_bal = cur_bal + amount_sol
        update_balance(user_id, new_bal)
        add_trade(user_id, "SELL", mint, amount_sol)
        add_month_profit(user_id, amount_sol)
        try:
            await bot.send_message(user_id, f"Sell submitted ✅\nTx: {txid}\nGot: {fmt(amount_sol)} SOL")
        except: pass
        return f"Sell OK: {txid}"
    except Exception as e:
        log.exception("perform_sell: %s", e)
        return f"Sell failed: {e}"

# ---------------- monthly leaderboard ----------------
def monthly_leaderboard():
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT user_id, username, month_profit FROM users ORDER BY month_profit DESC LIMIT 10")
    rows = c.fetchall(); conn.close(); return rows

async def send_monthly_leaderboard(bot: Bot):
    rows = monthly_leaderboard()
    if not rows: return
    txt = "🏆 MONTHLY LEADERBOARD\n\n"
    r = 1
    for uid, uname, prof in rows:
        txt += f"{r}. @{(uname or uid)} — {fmt(prof)} SOL\n"; r += 1
    try:
        await bot.send_message(ADMIN_ID, txt)
    except: pass
    conn = get_conn(); c = conn.cursor(); c.execute("UPDATE users SET month_profit=0"); conn.commit(); conn.close()

# ---------------- aiogram handlers ----------------
bot = Bot(token=BOT_TOKEN, parse_mode="Markdown")
dp = Dispatcher(bot)

@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    uid = msg.from_user.id; uname = msg.from_user.username or msg.from_user.first_name or ""
    try:
        created = create_user(uid, uname)
    except RuntimeError as e:
        await msg.reply(str(e)); return
    u = get_user(uid)
    if u:
        await msg.reply(f"Welcome. Your SOL address:\n`{u[2]}`\nSend ≥ ${MIN_DEPOSIT_USD} (SOL or USDT SPL). Use /balance to check.", parse_mode="Markdown")
    else:
        await msg.reply("Error creating wallet.")

@dp.message_handler(commands=["balance"])
async def cmd_balance(msg: types.Message):
    uid = msg.from_user.id; u = get_user(uid)
    if not u: return await msg.reply("No wallet. Use /start")
    sol_price = await get_sol_price()
    bal = float(u[4] or 0)
    await msg.reply(f"Balance: {fmt(bal)} SOL (~${fmt(bal*sol_price)})")

@dp.message_handler(commands=["buy"])
async def cmd_buy(msg: types.Message):
    args = msg.get_args().split()
    if len(args) < 2: return await msg.reply("Usage: /buy <mint> <amount_in_SOL>")
    mint = args[0]
    try: amt = float(args[1])
    except: return await msg.reply("Invalid amount")
    res = await perform_buy(msg.from_user.id, mint, amt, bot)
    await msg.reply(res)

@dp.message_handler(commands=["sell"])
async def cmd_sell(msg: types.Message):
    args = msg.get_args().split()
    if len(args) < 2: return await msg.reply("Usage: /sell <mint> <amount_in_SOL>")
    mint = args[0]
    try: amt = float(args[1])
    except: return await msg.reply("Invalid amount")
    res = await perform_sell(msg.from_user.id, mint, amt, bot)
    await msg.reply(res)

@dp.message_handler(commands=["refresh"])
async def cmd_refresh(msg: types.Message):
    uid = msg.from_user.id; u = get_user(uid)
    if not u: return await msg.reply("No wallet. /start first.")
    bal = await get_balance_rpc(u[2])
    update_balance(uid, bal)
    await msg.reply(f"Refreshed: {fmt(bal)} SOL")

@dp.message_handler(commands=["withdraw"])
async def cmd_withdraw(msg: types.Message):
    args = msg.get_args().split()
    if len(args) < 2: return await msg.reply("Usage: /withdraw <amount_SOL> <dest_pubkey>")
    try: amt = float(args[0]); dest = args[1]
    except: return await msg.reply("Invalid args")
    u = get_user(msg.from_user.id)
    if not u: return await msg.reply("No wallet")
    bal = float(u[4] or 0)
    if amt > bal: return await msg.reply("Insufficient funds")
    priv = u[3]; secret = decode_priv(priv); kp = Keypair.from_secret_key(secret)
    tx = Transaction().add(transfer(TransferParams(from_pubkey=PublicKey(u[2]), to_pubkey=PublicKey(dest), lamports=int(amt*1e9))))
    try:
        async with AsyncClient(RPC_URL) as client:
            resp = await client.send_transaction(tx, kp)
            update_balance(msg.from_user.id, bal-amt)
            add_trade(msg.from_user.id, "WITHDRAW", "SOL", amt)
            await msg.reply(f"Withdraw submitted: {resp}")
    except Exception as e:
        log.exception("withdraw err: %s", e)
        await msg.reply(f"Withdraw failed: {e}")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("BUY|"))
async def callback_buy(call: types.CallbackQuery):
    parts = call.data.split("|")
    if len(parts) >= 3:
        mint = parts[1]; symbol = parts[2]
        await call.message.answer(f"To buy {symbol} ({mint}) use: /buy {mint} <amount_in_SOL>")
    await call.answer()

# ---------------- startup background tasks ----------------
async def on_startup(dispatcher):
    init_db()
    loop = asyncio.get_event_loop()
    loop.create_task(poll_deposits(bot))
    loop.create_task(trending_poller(bot))

    async def monthly_loop():
        while True:
            now = datetime.utcnow()
            next_month = (now.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=10, microsecond=0)
            wait = (next_month - now).total_seconds()
            await asyncio.sleep(max(60, wait))
            try: await send_monthly_leaderboard(bot)
            except: log.exception("monthly err")
    loop.create_task(monthly_loop())

# ---------------- run ----------------
if __name__ == "__main__":
    executor.start_polling(dp, on_startup=on_startup)
