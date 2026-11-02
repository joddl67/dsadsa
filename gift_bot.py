import asyncio
import html
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import time

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, Message, PreCheckoutQuery,
)
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

E = os.environ


def num(name, default):
    try:
        return float(E.get(name)) or default
    except (TypeError, ValueError):
        return default


STARS_PER_COIN = num("STARS_PER_COIN", 1)
USD_PER_COIN = 0.015
MIN = 15
MAX = int(num("MAX_COINS", 100000))
NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
INT_RE = re.compile(r"^\d+$", re.ASCII)
# --- Crypto Pay ---
CRYPTO_TOKEN = "652364:AAWFuh7bJOcvuaMKx8gVaIJQhoN0SlNjRQ8"  # @CryptoBot -> Crypto Pay -> My Apps -> API Token
CRYPTO_TESTNET = False   # True, если токен из @CryptoTestnetBot
CRYPTO_PROXY = None      # например "http://127.0.0.1:8080", если pay.crypt.bot не открывается
CRYPTO_API = "https://testnet-pay.crypt.bot/api" if CRYPTO_TESTNET else "https://pay.crypt.bot/api"
CRYPTO_HEADERS = {"Crypto-Pay-API-Token": CRYPTO_TOKEN}

BOT_TOKEN = "8833339730:AAFn0xGP8YhOPTnr_MrFcSZL6SwPPMSHqbM"
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# ---------- БД ----------
db = sqlite3.connect("bot.db", isolation_level=None)  # autocommit
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode = WAL")
db.executescript("""
CREATE TABLE IF NOT EXISTS dialogs(user_id INTEGER PRIMARY KEY, step TEXT, nick TEXT, coins INTEGER);
CREATE TABLE IF NOT EXISTS orders(
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, username TEXT, nick TEXT NOT NULL, coins INTEGER NOT NULL,
  method TEXT, price TEXT, status TEXT NOT NULL DEFAULT 'new',   -- new -> pending -> paid
  pay_ref TEXT, invoice_id INTEGER, created_at INTEGER NOT NULL, paid_at INTEGER,
  notified INTEGER NOT NULL DEFAULT 0, credited INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pay_ref ON orders(pay_ref) WHERE pay_ref IS NOT NULL;  -- один платёж = один заказ
""")


def dlg_get(uid):
    return db.execute("SELECT * FROM dialogs WHERE user_id=?", (uid,)).fetchone()


def dlg_set(uid, step, nick, coins):
    db.execute(
        """INSERT INTO dialogs(user_id,step,nick,coins) VALUES(?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET step=excluded.step,nick=excluded.nick,coins=excluded.coins""",
        (uid, step, nick, coins),
    )


def dlg_del(uid):
    db.execute("DELETE FROM dialogs WHERE user_id=?", (uid,))


def order_get(oid):
    return db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()


def order_set_pending(method, price, invoice_id, oid):
    db.execute(
        "UPDATE orders SET method=?, price=?, status='pending', invoice_id=? "
        "WHERE id=? AND status IN('new','pending')",
        (method, price, invoice_id, oid),
    )


def order_mark_paid(ref, oid):
    # АТОМАРНО: только первый вызов вернёт rowcount=1 → защита от двойной обработки
    cur = db.execute(
        "UPDATE orders SET status='paid', pay_ref=?, paid_at=? WHERE id=? AND status='pending'",
        (ref, int(time.time() * 1000), oid),
    )
    return cur.rowcount


def stars(c):
    return math.ceil(c * STARS_PER_COIN)


def usd(c):
    return f"{c * USD_PER_COIN:.2f}"


def esc(s):
    return html.escape(str(s), quote=False)


async def crypto_call(method, params=None, post=False):
    """Вызов Crypto Pay API. При любой проблеме кидает RuntimeError с понятным текстом."""
    url = f"{CRYPTO_API}/{method}"
    try:
        if post:
            cm = http.post(url, json=params, headers=CRYPTO_HEADERS, proxy=CRYPTO_PROXY)
        else:
            cm = http.get(url, params=params, headers=CRYPTO_HEADERS, proxy=CRYPTO_PROXY)
        async with cm as r:
            text = await r.text()
            status = r.status
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise RuntimeError(f"нет связи с {CRYPTO_API}: {e!r}")
    try:
        j = json.loads(text)
    except ValueError:
        raise RuntimeError(f"HTTP {status}: {text[:200]}")
    if not j.get("ok"):
        err = j.get("error")
        name = err.get("name") if isinstance(err, dict) else err
        raise RuntimeError(str(name))
    return j["result"]


# ---------- диалог ----------
@dp.message(CommandStart())
async def cmd_start(m: Message):
    dlg_set(m.from_user.id, "nick", None, None)
    await m.answer("👋 Привет! Это бот пополнения ONIX Company.\n\nВведи свой <b>никнейм в Minecraft</b>:")


@dp.message(F.text, F.chat.type == "private")
async def on_text(m: Message):
    d = dlg_get(m.from_user.id)
    if not d:
        return await m.answer("Нажми /start, чтобы начать пополнение.")
    text = m.text.strip()

    if d["step"] == "nick":
        if not NICK_RE.match(text):
            return await m.answer("Ник: 3–16 символов, латиница, цифры и «_». Попробуй ещё раз.")
        dlg_set(m.from_user.id, "coins", text, None)
        return await m.answer(
            f"Ник: {esc(text)}\n\nСколько монет ◆ хочешь купить? (от {MIN} до {MAX})\n"
            f"Курс: 1 ◆ = {STARS_PER_COIN:g} ⭐ или {USD_PER_COIN:g} $"
        )

    if d["step"] == "coins":
        if not INT_RE.match(text) or not (MIN <= int(text) <= MAX):
            return await m.answer(f"Введи целое число от {MIN} до {MAX}.")
        coins = int(text)
        oid = secrets.token_hex(8)
        db.execute(
            "INSERT INTO orders(id,user_id,username,nick,coins,created_at) VALUES(?,?,?,?,?,?)",
            (oid, m.from_user.id, m.from_user.username, d["nick"], coins, int(time.time() * 1000)),
        )
        dlg_del(m.from_user.id)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐ Звёзды — {stars(coins)} ⭐", callback_data=f"pay:stars:{oid}")],
            [InlineKeyboardButton(text=f"💎 Crypto Pay — {usd(coins)} $", callback_data=f"pay:crypto:{oid}")],
        ])
        return await m.answer(
            f"Заказ <code>{oid}</code>\nНик: <b>{esc(d['nick'])}</b>\nМонеты: <b>{coins} ◆</b>\n\n"
            "Выбери способ оплаты:",
            reply_markup=kb,
        )


# ---------- оплата ----------
@dp.callback_query(F.data.regexp(r"^pay:(stars|crypto):([0-9a-f]{16})$").as_("match"))
async def on_pay(cb: CallbackQuery, match: re.Match):
    method, oid = match.group(1), match.group(2)
    o = order_get(oid)
    if not o or o["user_id"] != cb.from_user.id:
        return await cb.answer("Заказ не найден", show_alert=True)
    if o["status"] == "paid":
        return await cb.answer("Заказ уже оплачен", show_alert=True)
    await cb.answer()
    chat_id = cb.message.chat.id

    if method == "stars":
        order_set_pending("stars", str(stars(o["coins"])), None, oid)
        # для Stars: currency XTR, provider_token пустой
        return await bot.send_invoice(
            chat_id,
            title=f"{o['coins']} ◆ для {o['nick']}",
            description=f"Пополнение баланса ONIX, ник {o['nick']}",
            payload=oid,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=f"{o['coins']} монет", amount=stars(o["coins"]))],
        )

    try:
        base = {
            "description": f"ONIX: {o['coins']} монет для {o['nick']}", "payload": oid,
            "expires_in": 3600, "allow_comments": False, "allow_anonymous": False,
        }
        try:  # 1) счёт в фиате (USD), пользователь платит любой криптой
            inv = await crypto_call("createInvoice", {
                **base, "currency_type": "fiat", "fiat": "USD", "amount": usd(o["coins"])}, post=True)
        except RuntimeError as e1:  # 2) запасной вариант: счёт в USDT 1:1 к доллару
            log.warning("createInvoice fiat не вышел (%s), пробую USDT", e1)
            inv = await crypto_call("createInvoice", {
                **base, "currency_type": "crypto", "asset": "USDT", "amount": usd(o["coins"])}, post=True)
        order_set_pending("crypto", usd(o["coins"]), inv["invoice_id"], oid)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💎 Оплатить в Crypto Pay", url=inv["bot_invoice_url"])
        ]])
        await bot.send_message(
            chat_id,
            f"Счёт на <b>{usd(o['coins'])} $</b> создан (действует 1 час). Оплата подтвердится автоматически.",
            reply_markup=kb,
        )
    except Exception as e:
        log.error("createInvoice %s", e)
        await bot.send_message(chat_id, f"Не удалось создать счёт: <code>{esc(e)}</code>\nПопробуй позже.")


# Stars: подтверждение перед списанием
@dp.pre_checkout_query()
async def on_pre_checkout(p: PreCheckoutQuery):
    o = order_get(p.invoice_payload)
    ok = bool(
        o and o["user_id"] == p.from_user.id and o["status"] == "pending" and o["method"] == "stars"
        and p.currency == "XTR" and p.total_amount == stars(o["coins"])
    )
    if ok:
        await p.answer(ok=True)
    else:
        await p.answer(ok=False, error_message="Заказ недействителен, создай новый через /start")


@dp.message(F.successful_payment)
async def on_success(m: Message):
    p = m.successful_payment
    await finalize(p.invoice_payload, "tg:" + p.telegram_payment_charge_id)


# ---------- Crypto Pay: опрос статуса счетов раз в 15 сек (без вебхука) ----------
async def crypto_poll_loop():
    while True:
        await asyncio.sleep(15)
        try:
            since = int((time.time() - 2 * 3600) * 1000)
            lst = db.execute(
                "SELECT * FROM orders WHERE status='pending' AND method='crypto' "
                "AND invoice_id IS NOT NULL AND created_at>?",
                (since,),
            ).fetchall()
            if not lst:
                continue
            ids = ",".join(str(o["invoice_id"]) for o in lst)
            res = await crypto_call("getInvoices", {"invoice_ids": ids})
            for inv in res["items"]:
                if inv.get("status") != "paid":
                    continue
                o = next((x for x in lst if x["invoice_id"] == inv["invoice_id"]), None)
                # сверка: payload и сумма должны совпасть с нашим заказом
                if o and inv.get("payload") == o["id"] and float(inv["amount"]) >= float(o["price"]) - 0.001:
                    await finalize(o["id"], f"crypto:{inv['invoice_id']}")
        except Exception as e:
            log.error("poll %s", e)


# ---------- завершение заказа (идемпотентно) ----------
async def finalize(order_id, ref):
    try:
        changed = order_mark_paid(ref, order_id)
    except sqlite3.IntegrityError:
        return log.error("dup payment ref %s", ref)  # этот платёж уже привязан к заказу
    if changed != 1:
        return  # уже обработан → второй раз не шлём
    o = order_get(order_id)
    try:
        await bot.send_message(
            o["user_id"],
            f"✅ Оплата получена!\n{o['coins']} ◆ для <b>{esc(o['nick'])}</b>. "
            "Заявка отправлена, монеты скоро придут.",
        )
    except Exception:
        pass
    await notify_channel(o)


async def notify_channel(o):
    try:
        who = f"@{o['username']}" if o["username"] else f"id {o['user_id']}"
        pay = f"{o['price']} ⭐" if o["method"] == "stars" else f"{o['price']} $ (Crypto Pay)"
        await bot.send_message(
            E["-1004479601062"],
            "🆕 <b>Заявка на пополнение</b>\n\n"
            f"Заказ: <code>{o['id']}</code>\nНик: <b>{esc(o['nick'])}</b>\nМонеты: <b>{o['coins']} ◆</b>\n"
            f"Оплата: {pay}\n"
            f"Telegram: {esc(who)}\n\n"
            f"Команда: <code>scoreboard players add {esc(o['nick'])} chess_bal {o['coins']}</code>",
        )
        db.execute("UPDATE orders SET notified=1 WHERE id=?", (o["id"],))
        await auto_credit(o)
    except Exception as e:
        log.error("channel send %s", e)  # повтор ниже


# необязательное авто-зачисление через бэкенд сайта; key=id заказа → повтор не зачислит дважды
async def auto_credit(o):
    url = E.get("BACKEND_CREDIT_URL")
    if not url or o["credited"]:
        return
    try:
        async with http.post(
            url,
            headers={"x-admin-token": E.get("ADMIN_TOKEN", "")},
            json={"key": "tg-" + o["id"], "nick": o["nick"], "coins": o["coins"]},
        ) as r:
            if r.status < 300 or r.status == 202:
                db.execute("UPDATE orders SET credited=1 WHERE id=?", (o["id"],))
    except Exception as e:
        log.error("autoCredit %s", e)


# ретрай: оплачено, но заявка в канал не ушла
async def retry_loop():
    while True:
        await asyncio.sleep(30)
        try:
            for o in db.execute("SELECT * FROM orders WHERE status='paid' AND notified=0").fetchall():
                await notify_channel(o)
        except Exception as e:
            log.error("retry %s", e)


# ---------- запуск ----------
http: aiohttp.ClientSession


async def main():
    global http
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        try:  # проверка токена Crypto Pay при старте
            app = await crypto_call("getMe")
            log.info("Crypto Pay OK: %s", app.get("name"))
        except RuntimeError as e:
            log.error("Crypto Pay НЕ РАБОТАЕТ: %s", e)
        tasks = [asyncio.create_task(crypto_poll_loop()), asyncio.create_task(retry_loop())]
        log.info("bot started")
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            for t in tasks:
                t.cancel()
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
