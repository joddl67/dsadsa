import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    LabeledPrice,
    PreCheckoutQuery
)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup


# ==========================
# НАСТРОЙКИ
# ==========================

BOT_TOKEN = "8833339730:AAFn0xGP8YhOPTnr_MrFcSZL6SwPPMSHqbM"

# ID твоего Telegram канала/чата для заявок
ADMIN_CHAT_ID = -1004479601062


bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# ==========================
# СОСТОЯНИЯ
# ==========================

class BuyStars(StatesGroup):
    nickname = State()
    amount = State()


# ==========================
# СТАРТ
# ==========================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):

    await state.clear()

    await message.answer(
        "⭐ Покупка звёзд\n\n"
        "Введите ваш ник игрока:"
    )

    await state.set_state(BuyStars.nickname)


# ==========================
# НИК
# ==========================

@dp.message(BuyStars.nickname)
async def get_nickname(
        message: Message,
        state: FSMContext
):

    await state.update_data(
        nickname=message.text
    )

    await message.answer(
        "Введите количество звёзд ⭐:"
    )

    await state.set_state(BuyStars.amount)


# ==========================
# СУММА
# ==========================

@dp.message(BuyStars.amount)
async def get_amount(
        message: Message,
        state: FSMContext
):

    if not message.text.isdigit():

        await message.answer(
            "Введите только число ⭐"
        )
        return


    stars = int(message.text)

    if stars < 1:

        await message.answer(
            "Минимум 1 звезда"
        )
        return


    data = await state.get_data()


    await state.update_data(
        stars=stars
    )


    # Создание оплаты Telegram Stars

    await bot.send_invoice(

        chat_id=message.from_user.id,

        title="Покупка звёзд",

        description=
        f"Игровой ник: {data['nickname']}\n"
        f"Количество: {stars} ⭐",

        payload=
        f"stars_{message.from_user.id}",

        currency="XTR",

        prices=[
            LabeledPrice(
                label="Telegram Stars",
                amount=stars
            )
        ]
    )


# ==========================
# ПРОВЕРКА ОПЛАТЫ
# ==========================

@dp.pre_checkout_query()
async def checkout(
        query: PreCheckoutQuery
):

    await bot.answer_pre_checkout_query(
        query.id,
        ok=True
    )


# ==========================
# ПОСЛЕ ОПЛАТЫ
# ==========================

@dp.message(F.successful_payment)
async def payment_success(
        message: Message,
        state: FSMContext
):

    data = await state.get_data()


    nickname = data.get(
        "nickname",
        "Не указан"
    )

    stars = data.get(
        "stars",
        0
    )


    # Сообщение админу

    await bot.send_message(

        ADMIN_CHAT_ID,

        "💰 Новая заявка!\n\n"

        f"👤 Ник: {nickname}\n"

        f"⭐ Количество: {stars}\n"

        f"🆔 Игрок: {message.from_user.id}"

    )


    await message.answer(
        "✅ Оплата прошла успешно!\n"
        "Заявка отправлена администрации."
    )


    await state.clear()



# ==========================
# ЗАПУСК
# ==========================

async def main():

    await dp.start_polling(bot)



if __name__ == "__main__":
    asyncio.run(main())
