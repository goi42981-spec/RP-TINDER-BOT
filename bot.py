import asyncio
import logging
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import BOT_TOKEN, REQUIRED_CHAT_ID, REQUIRED_CHAT_LINK
from db import (
    count_pending_likes,
    find_next_candidate,
    find_next_pending_like,
    get_profile,
    get_swipe,
    has_liked_me,
    init_db,
    record_swipe,
    upsert_profile,
)


logger = logging.getLogger(__name__)
router = Router()


GENDER_LABELS = {
    "f": "Женский",
    "m": "Мужской",
}

PREFERRED_LABELS = {
    **GENDER_LABELS,
    "a": "Не важно",
}

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
TME_RE = re.compile(r"(?:^|\s)(?:@|t\.me/|telegram\.me/)([A-Za-z0-9_]{4,32})", re.IGNORECASE)


class Form(StatesGroup):
    char_gender = State()
    preferred_gender = State()
    username = State()
    profile_link = State()


def gender_keyboard(prefix: str, include_any: bool = False) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(text="Ж", callback_data=f"{prefix}:f"),
        InlineKeyboardButton(text="М", callback_data=f"{prefix}:m"),
    ]
    if include_any:
        row.append(InlineKeyboardButton(text="Не важно", callback_data=f"{prefix}:a"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def main_menu_keyboard(has_profile: bool, pending_likes: int = 0) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_profile:
        rows.append([InlineKeyboardButton(text="🔍 Найти пару", callback_data="find")])
        likes_text = (
            f"❤️ Входящие лайки ({pending_likes})"
            if pending_likes > 0
            else "❤️ Входящие лайки"
        )
        rows.append([InlineKeyboardButton(text=likes_text, callback_data="likes")])
        rows.append([InlineKeyboardButton(text="✏️ Перезаполнить анкету", callback_data="fill")])
    else:
        rows.append([InlineKeyboardButton(text="📝 Заполнить анкету", callback_data="fill")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def swipe_keyboard(candidate_id: int, source: str = "feed") -> InlineKeyboardMarkup:
    """source='feed' (discovery, авто-листание) | 'like' (просмотр чьего-то лайка)."""
    rows = [
        [
            InlineKeyboardButton(text="❤️", callback_data=f"swipe:like:{candidate_id}:{source}"),
            InlineKeyboardButton(text="👎", callback_data=f"swipe:pass:{candidate_id}:{source}"),
        ]
    ]
    if source == "feed":
        rows.append([InlineKeyboardButton(text="⏹ Выйти из поиска", callback_data="swipe:stop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def view_like_keyboard(liker_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👀 Посмотреть", callback_data=f"view_like:{liker_id}")]
        ]
    )


def candidate_card_text(profile: dict) -> str:
    return (
        "<b>Анкета кандидата:</b>\n\n"
        f"• Пол персонажа: <b>{profile['char_gender']}</b>\n"
        f"• Ищет: <b>{profile['preferred_gender']}</b>\n"
        f"• Анкета: {profile['profile_link']}"
    )


async def is_chat_member(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(REQUIRED_CHAT_ID, user_id)
    except TelegramBadRequest as e:
        logger.warning("get_chat_member failed: %s", e)
        return False
    return member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)


def not_a_member_text() -> str:
    if REQUIRED_CHAT_LINK:
        return (
            "Чтобы пользоваться ботом, нужно состоять в нашем РП-чате.\n\n"
            f"Вступай: {REQUIRED_CHAT_LINK}\n\n"
            "После вступления отправь /start ещё раз."
        )
    return (
        "Чтобы пользоваться ботом, нужно состоять в нашем РП-чате.\n\n"
        "После вступления отправь /start ещё раз."
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    if not await is_chat_member(bot, message.from_user.id):
        await message.answer(not_a_member_text())
        return

    existing = await get_profile(message.from_user.id)
    if existing:
        pending = await count_pending_likes(message.from_user.id)
        await message.answer(
            "С возвращением! Можешь искать пару или посмотреть входящие лайки.",
            reply_markup=main_menu_keyboard(has_profile=True, pending_likes=pending),
        )
    else:
        await message.answer(
            "Привет! Я помогу найти партнёра по РП.\nЖми кнопку, чтобы заполнить анкету.",
            reply_markup=main_menu_keyboard(has_profile=False),
        )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Нечего отменять.")
        return
    await state.clear()
    await message.answer("Заполнение анкеты отменено. /start — начать заново.")


@router.message(Command("me"))
async def cmd_me(message: Message) -> None:
    profile = await get_profile(message.from_user.id)
    if not profile:
        await message.answer("У тебя ещё нет анкеты. Жми /start.")
        return
    await message.answer(
        "Твоя анкета:\n\n"
        f"• Пол персонажа: <b>{profile['char_gender']}</b>\n"
        f"• Ищет: <b>{profile['preferred_gender']}</b>\n"
        f"• Юз: {profile['username']}\n"
        f"• Анкета: {profile['profile_link']}"
    )


# --- Заполнение анкеты ----------------------------------------------------


@router.callback_query(F.data == "fill")
async def start_filling(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not await is_chat_member(bot, cb.from_user.id):
        await cb.message.answer(not_a_member_text())
        await cb.answer()
        return
    await state.set_state(Form.char_gender)
    await cb.message.answer(
        "1/4. Какой пол у твоего персонажа?",
        reply_markup=gender_keyboard("char"),
    )
    await cb.answer()


@router.callback_query(Form.char_gender, F.data.startswith("char:"))
async def set_char_gender(cb: CallbackQuery, state: FSMContext) -> None:
    code = cb.data.split(":", 1)[1]
    if code not in GENDER_LABELS:
        await cb.answer("Неизвестный выбор", show_alert=True)
        return
    await state.update_data(char_gender=GENDER_LABELS[code])
    await state.set_state(Form.preferred_gender)
    await cb.message.edit_text(
        f"1/4. Пол персонажа: <b>{GENDER_LABELS[code]}</b>"
    )
    await cb.message.answer(
        "2/4. Какой пол партнёра предпочитаешь?",
        reply_markup=gender_keyboard("pref", include_any=True),
    )
    await cb.answer()


@router.callback_query(Form.preferred_gender, F.data.startswith("pref:"))
async def set_preferred_gender(cb: CallbackQuery, state: FSMContext) -> None:
    code = cb.data.split(":", 1)[1]
    if code not in PREFERRED_LABELS:
        await cb.answer("Неизвестный выбор", show_alert=True)
        return
    await state.update_data(preferred_gender=PREFERRED_LABELS[code])
    await state.set_state(Form.username)
    await cb.message.edit_text(
        f"2/4. Предпочитаемый пол: <b>{PREFERRED_LABELS[code]}</b>"
    )
    await cb.message.answer(
        "3/4. Напиши свой юзернейм (например, <code>@nickname</code>)."
    )
    await cb.answer()


@router.message(Form.username)
async def set_username(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Юзернейм не может быть пустым. Попробуй ещё раз.")
        return

    m = TME_RE.search(" " + text)
    if m:
        username = "@" + m.group(1)
    elif re.fullmatch(r"[A-Za-z0-9_]{4,32}", text):
        username = "@" + text
    else:
        await message.answer(
            "Не могу разобрать юзернейм. Пришли в формате <code>@nickname</code>."
        )
        return

    await state.update_data(username=username)
    await state.set_state(Form.profile_link)
    await message.answer(
        "4/4. Скинь <b>ссылку</b> на анкету.\n"
        "Это должна быть именно ссылка — не сам текст анкеты."
    )


@router.message(Form.profile_link)
async def set_profile_link(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()

    url_match = URL_RE.search(text)
    if not url_match:
        await message.answer(
            "Это не похоже на ссылку. Пришли URL, начинающийся с <code>http://</code> или <code>https://</code>.\n"
            "Сам текст анкеты сюда не нужен — только ссылка на неё."
        )
        return

    url = url_match.group(0).rstrip(".,;)]}>'\"")

    text_without_url = URL_RE.sub("", text).strip()
    if len(text_without_url) > 100:
        await message.answer(
            "Похоже, ты вставила сам текст анкеты. Мне нужна <b>только ссылка</b> на анкету, без описания.\n"
            "Пришли ещё раз — одной ссылкой."
        )
        return

    data = await state.get_data()
    await upsert_profile(
        user_id=message.from_user.id,
        char_gender=data["char_gender"],
        preferred_gender=data["preferred_gender"],
        username=data["username"],
        profile_link=url,
    )
    await state.clear()

    await message.answer(
        "Готово! Анкета сохранена:\n\n"
        f"• Пол персонажа: <b>{data['char_gender']}</b>\n"
        f"• Ищет: <b>{data['preferred_gender']}</b>\n"
        f"• Юз: {data['username']}\n"
        f"• Анкета: {url}\n\n"
        "Теперь можно искать пару!",
        reply_markup=main_menu_keyboard(has_profile=True),
    )


# --- Входящие лайки -------------------------------------------------------


async def _show_next_pending_like(bot: Bot, chat_id: int, user_id: int) -> None:
    liker = await find_next_pending_like(user_id)
    if not liker:
        await bot.send_message(
            chat_id,
            "Нет новых входящих лайков.",
            reply_markup=main_menu_keyboard(has_profile=True),
        )
        return
    await bot.send_message(
        chat_id,
        "❤️ <b>Этот человек лайкнул твою анкету:</b>\n\n"
        + candidate_card_text(liker).replace("<b>Анкета кандидата:</b>\n\n", ""),
        reply_markup=swipe_keyboard(liker["user_id"], source="likes"),
    )


@router.message(Command("likes"))
async def cmd_likes(message: Message, bot: Bot) -> None:
    if not await is_chat_member(bot, message.from_user.id):
        await message.answer(not_a_member_text())
        return
    if not await get_profile(message.from_user.id):
        await message.answer(
            "Сначала заполни анкету.",
            reply_markup=main_menu_keyboard(has_profile=False),
        )
        return
    await _show_next_pending_like(bot, message.chat.id, message.from_user.id)


@router.callback_query(F.data == "likes")
async def cb_likes(cb: CallbackQuery, bot: Bot) -> None:
    if not await is_chat_member(bot, cb.from_user.id):
        await cb.message.answer(not_a_member_text())
        await cb.answer()
        return
    if not await get_profile(cb.from_user.id):
        await cb.message.answer(
            "Сначала заполни анкету.",
            reply_markup=main_menu_keyboard(has_profile=False),
        )
        await cb.answer()
        return
    await _show_next_pending_like(bot, cb.message.chat.id, cb.from_user.id)
    await cb.answer()


# --- Поиск пары ----------------------------------------------------------


@router.message(Command("find"))
async def cmd_find(message: Message, bot: Bot) -> None:
    if not await is_chat_member(bot, message.from_user.id):
        await message.answer(not_a_member_text())
        return
    if not await get_profile(message.from_user.id):
        await message.answer(
            "Сначала заполни свою анкету.",
            reply_markup=main_menu_keyboard(has_profile=False),
        )
        return
    await _show_next_candidate(bot, message.chat.id, message.from_user.id)


@router.callback_query(F.data == "find")
async def cb_find(cb: CallbackQuery, bot: Bot) -> None:
    if not await is_chat_member(bot, cb.from_user.id):
        await cb.message.answer(not_a_member_text())
        await cb.answer()
        return
    if not await get_profile(cb.from_user.id):
        await cb.message.answer(
            "Сначала заполни свою анкету.",
            reply_markup=main_menu_keyboard(has_profile=False),
        )
        await cb.answer()
        return
    await _show_next_candidate(bot, cb.message.chat.id, cb.from_user.id)
    await cb.answer()


async def _show_next_candidate(bot: Bot, chat_id: int, user_id: int) -> None:
    candidate = await find_next_candidate(user_id)
    if not candidate:
        await bot.send_message(
            chat_id,
            "Пока никого подходящего нет. Загляни попозже — когда добавятся новые анкеты, "
            "продолжим поиск.",
            reply_markup=main_menu_keyboard(has_profile=True),
        )
        return
    await bot.send_message(
        chat_id,
        candidate_card_text(candidate),
        reply_markup=swipe_keyboard(candidate["user_id"], source="feed"),
    )


@router.callback_query(F.data == "swipe:stop")
async def cb_swipe_stop(cb: CallbackQuery) -> None:
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(
        "Поиск остановлен. Можешь продолжить в любой момент.",
        reply_markup=main_menu_keyboard(has_profile=True),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("swipe:"))
async def cb_swipe(cb: CallbackQuery, bot: Bot) -> None:
    parts = cb.data.split(":")
    # Поддерживаем старый формат swipe:like:<id> и новый swipe:like:<id>:<source>
    if len(parts) not in (3, 4) or parts[1] not in ("like", "pass"):
        await cb.answer("Неизвестное действие", show_alert=True)
        return

    action = parts[1]
    try:
        candidate_id = int(parts[2])
    except ValueError:
        await cb.answer("Невалидный кандидат", show_alert=True)
        return

    source = parts[3] if len(parts) == 4 else "feed"

    me_id = cb.from_user.id
    me = await get_profile(me_id)
    if not me:
        await cb.answer("Сначала заполни анкету", show_alert=True)
        return

    existing = await get_swipe(me_id, candidate_id)
    await record_swipe(me_id, candidate_id, action)

    # Убираем кнопки у текущей карточки
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    matched = False
    # Обрабатываем лайк только если раньше не лайкали этого кандидата
    if action == "like" and existing != "like":
        if await has_liked_me(candidate_id, me_id):
            candidate = await get_profile(candidate_id)
            if candidate:
                await _notify_match(bot, me, candidate)
                matched = True
        else:
            await _notify_someone_liked(bot, target_user_id=candidate_id, liker_id=me_id)

    if source == "feed":
        await _show_next_candidate(bot, cb.message.chat.id, me_id)
    elif source == "likes":
        # Листаем по входящим лайкам
        if not matched:
            await _show_next_pending_like(bot, cb.message.chat.id, me_id)
    else:
        # Пришли из уведомления о лайке (source='like') — не листаем автоматически
        if not matched:
            pending = await count_pending_likes(me_id)
            note = (
                "Ок, лайк поставлен. Если она/он взаимно лайкнет — я сообщу."
                if action == "like"
                else "Ок, пропустила."
            )
            await cb.message.answer(
                note,
                reply_markup=main_menu_keyboard(has_profile=True, pending_likes=pending),
            )

    await cb.answer("❤️" if action == "like" else "👎")


@router.callback_query(F.data.startswith("view_like:"))
async def cb_view_like(cb: CallbackQuery, bot: Bot) -> None:
    try:
        liker_id = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer("Невалидный идентификатор", show_alert=True)
        return

    me_id = cb.from_user.id
    if not await get_profile(me_id):
        await cb.answer("Сначала заполни анкету (/start)", show_alert=True)
        return

    liker = await get_profile(liker_id)
    if not liker:
        await cb.answer("Этот пользователь больше не в базе", show_alert=True)
        return

    existing = await get_swipe(me_id, liker_id)
    if existing == "like":
        await cb.answer("Ты уже лайкнула этого пользователя", show_alert=True)
        return
    if existing == "pass":
        await cb.answer("Ты уже пропустила этого пользователя. Но можешь передумать", show_alert=False)

    await cb.message.answer(
        candidate_card_text(liker),
        reply_markup=swipe_keyboard(liker_id, source="like"),
    )
    await cb.answer()


async def _notify_match(bot: Bot, user_a: dict, user_b: dict) -> None:
    """Отправить уведомление о взаимном лайке обоим + пост в РП-чат."""
    text_for_a = (
        "🎉 <b>Взаимный лайк!</b>\n\n"
        f"Партнёр: {user_b['username']}\n"
        f"Анкета: {user_b['profile_link']}\n\n"
        "Удачи в РП! 💞"
    )
    text_for_b = (
        "🎉 <b>Взаимный лайк!</b>\n\n"
        f"Партнёр: {user_a['username']}\n"
        f"Анкета: {user_a['profile_link']}\n\n"
        "Удачи в РП! 💞"
    )
    for uid, text in [(user_a["user_id"], text_for_a), (user_b["user_id"], text_for_b)]:
        try:
            await bot.send_message(uid, text)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            logger.warning("Не смог отправить уведомление о мэтче user=%s: %s", uid, e)

    # Пост в РП-чат с тегом обоих
    chat_text = (
        f"🎉 Поздравляем {user_a['username']} и {user_b['username']} с мэтчем! "
        "Удачи в РП! 💞"
    )
    try:
        await bot.send_message(REQUIRED_CHAT_ID, chat_text)
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        logger.warning("Не смог отправить поздравление в чат: %s", e)


async def _notify_someone_liked(bot: Bot, target_user_id: int, liker_id: int) -> None:
    """Сообщить пользователю, что его лайкнули — без раскрытия юзернейма."""
    try:
        await bot.send_message(
            target_user_id,
            "🔔 <b>Кто-то лайкнул твою анкету!</b>\n\n"
            "Нажми кнопку, чтобы посмотреть анкету. Если лайкнешь в ответ — будет мэтч 💞",
            reply_markup=view_like_keyboard(liker_id),
        )
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        logger.warning(
            "Не смог отправить уведомление о лайке user=%s: %s", target_user_id, e
        )


# --- Запуск --------------------------------------------------------------


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
