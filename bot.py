import logging
import os
import json
from pathlib import Path
from typing import Dict, Any, Set, Optional
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    JobQueue,
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
from telegram.ext import ConversationHandler


# Загрузка переменных окружения из .env файла
load_dotenv()


class Config:
    """Конфигурация приложения"""

    # Настройки Google Sheets
    GOOGLE_CREDENTIALS = Path(os.getenv("GOOGLE_CREDENTIALS", "credentials.json"))
    SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")

    # Настройки Telegram
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")

    # Система доступа
    ALLOWED_USERS_FILE = Path(os.getenv("ALLOWED_USERS_FILE", "allowed_users.json"))
    ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

    # Конфигурация листов
    SHEET_CONFIG = {
        "income": {
            "sheet_name": "Лист1",
            "search_phrase": "Всего доходы",
            "title": "доходы",
        },
        "consumption": {
            "sheet_name": "Лист2",
            "search_phrase": "Всего расходы",
            "title": "расходы",
        },
        "cash": {
            "sheet_name": "Лист3",
            "search_phrase": "Всего в кассе",
            "title": "в кассе",
        },
    }

    # Настройки кеширования
    CACHE_UPDATE_INTERVAL = int(os.getenv("CACHE_UPDATE_INTERVAL", 60))  # 5 минут

    # Тексты интерфейса
    TEXTS = {
        "start_message": "Нажмите кнопку:",
        "main_button": "📊 Получить итог",
        "add_button": "➕ Добавить пользователя",
        "remove_button": "➖ Удалить пользователя",
        "response_template": "Всего {title}: {value}",
        "error_message": "🚫 Произошла ошибка при обработке",
        "na_value": "Н/Д",
        "access_denied": "🚫 Доступ запрещен! Ваш ID: {user_id}",
        "admin_only": "🚫 Только для администратора!",
        "add_user_message": "Введите ID пользователя:",
        "remove_user_message": "Введите ID пользователя:",
        "invalid_id": "❌ Неверный формат ID. Введите целое число:",
        "cancel_message": "🚫 Операция отменена",
    }


class States:
    AWAIT_USER_ID_ADD = 1
    AWAIT_USER_ID_REMOVE = 2


# Инициализация кеша данных
cell_cache: Dict[str, Dict[str, Any]] = {
    key: {"sheet": None, "pos": None, "value": None} for key in Config.SHEET_CONFIG
}


async def create_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    """Создание клавиатуры в зависимости от прав пользователя"""
    user_data = load_users()
    is_admin = user_id == user_data["admin_id"]

    buttons = [[Config.TEXTS["main_button"]]]  # Закрывающая скобка для первого элемента

    if is_admin:
        buttons.append([Config.TEXTS["add_button"], Config.TEXTS["remove_button"]])

    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=False)


def load_users() -> Dict[str, Any]:
    """Загрузка списка пользователей из JSON-файла"""
    try:
        if not Config.ALLOWED_USERS_FILE.exists():
            return {"admin_id": Config.ADMIN_ID, "allowed_users": []}

        with open(Config.ALLOWED_USERS_FILE, "r") as f:
            data = json.load(f)
            return {
                "admin_id": int(data.get("admin_id", Config.ADMIN_ID)),
                "allowed_users": [int(u) for u in data.get("allowed_users", [])],
            }
    except Exception as e:
        logging.error(f"Ошибка загрузки пользователей: {str(e)}")
        return {"admin_id": Config.ADMIN_ID, "allowed_users": []}


def save_users(data: Dict[str, Any]) -> None:
    """Сохранение списка пользователей в файл"""
    try:
        with open(Config.ALLOWED_USERS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logging.error(f"Ошибка сохранения пользователей: {str(e)}")


async def check_access(update: Update) -> bool:
    """Проверка прав доступа пользователя"""
    user_data = load_users()
    user_id = update.effective_user.id

    if user_id != user_data["admin_id"] and user_id not in user_data["allowed_users"]:
        await update.message.reply_text(
            Config.TEXTS["access_denied"].format(user_id=user_id)
        )
        return False
    return True


def initialize_google_sheets():
    """Инициализация подключения к Google Sheets"""
    try:
        if not Config.GOOGLE_CREDENTIALS.exists():
            raise FileNotFoundError(
                f"Файл учетных данных не найден: {Config.GOOGLE_CREDENTIALS}"
            )

        creds = ServiceAccountCredentials.from_json_keyfile_name(
            str(Config.GOOGLE_CREDENTIALS),
            [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        client = gspread.authorize(creds)
        return client.open_by_key(Config.SPREADSHEET_ID)
    except Exception as e:
        logging.error(f"Ошибка инициализации Google Sheets: {str(e)}")
        raise


async def init_cache(context: ContextTypes.DEFAULT_TYPE):
    """Инициализация кеша при старте приложения"""
    try:
        spreadsheet = initialize_google_sheets()

        for key, config in Config.SHEET_CONFIG.items():
            try:
                sheet = spreadsheet.worksheet(config["sheet_name"])
                cell = sheet.find(
                    config["search_phrase"], in_column=None, case_sensitive=False
                )

                cell_cache[key].update(
                    {
                        "sheet": sheet,
                        "pos": (cell.row, cell.col + 1),
                        "value": sheet.cell(cell.row, cell.col + 1).value,
                    }
                )

            except gspread.exceptions.CellNotFound:
                logging.warning(f"Ячейка для {key} не найдена")
            except Exception as e:
                logging.error(f"Ошибка инициализации кеша {key}: {str(e)}")

    except Exception as e:
        logging.error(f"Ошибка инициализации кеша: {str(e)}")


async def update_cache(context: ContextTypes.DEFAULT_TYPE):
    """Обновление значений в кеше"""
    try:
        for key in Config.SHEET_CONFIG:
            if cell_cache[key]["pos"] and cell_cache[key]["sheet"]:
                cell_cache[key]["value"] = (
                    cell_cache[key]["sheet"].cell(*cell_cache[key]["pos"]).value
                )
    except Exception as e:
        logging.error(f"Ошибка обновления кеша: {str(e)}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    try:
        keyboard = await create_keyboard(update.effective_user.id)
        await update.message.reply_text(
            text=Config.TEXTS["start_message"], reply_markup=keyboard
        )
    except Exception as e:
        logging.error(f"Ошибка в команде /start: {str(e)}")
        await update.message.reply_text(Config.TEXTS["error_message"])


async def get_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    try:
        keyboard = await create_keyboard(update.effective_user.id)
        response = []
        for key, config in Config.SHEET_CONFIG.items():
            value = cell_cache[key].get("value", Config.TEXTS["na_value"])
            response.append(
                Config.TEXTS["response_template"].format(
                    title=config["title"], value=value or Config.TEXTS["na_value"]
                )
            )

        await update.message.reply_text("\n".join(response), reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Ошибка получения данных: {str(e)}")
        await update.message.reply_text(Config.TEXTS["error_message"])


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало процедуры добавления пользователя"""

    # Сохраняем состояние в context.chat_data
    keyboard = await create_keyboard(update.effective_user.id)

    context.chat_data["current_state"] = States.AWAIT_USER_ID_ADD
    user_data = load_users()
    users_list = (
        "📋 Текущий список пользователей:\n"
        + "\n".join(f"▫️ {user_id}" for user_id in user_data["allowed_users"])
        if user_data["allowed_users"]
        else "📋 Список пользователей пуст"
    )

    message = f"{users_list}\n " f"{Config.TEXTS['add_user_message']}"

    await update.message.reply_text(message, reply_markup=keyboard)
    return States.AWAIT_USER_ID_ADD


async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало процедуры удаления пользователя"""

    # Сохраняем состояние в context.chat_data
    keyboard = await create_keyboard(update.effective_user.id)

    context.chat_data["current_state"] = States.AWAIT_USER_ID_REMOVE
    user_data = load_users()
    users_list = (
        "📋 Текущий список пользователей:\n"
        + "\n".join(f"▫️ {user_id}" for user_id in user_data["allowed_users"])
        if user_data["allowed_users"]
        else "📋 Список пользователей пуст"
    )

    message = f"{users_list}\n " f"{Config.TEXTS['remove_user_message']}"
    await update.message.reply_text(message, reply_markup=keyboard)
    return States.AWAIT_USER_ID_REMOVE


async def handle_user_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ввода ID пользователя"""

    try:
        user_id = int(update.message.text)
        return await process_user_id(update, context, user_id)
    except ValueError:
        await update.message.reply_text(Config.TEXTS["invalid_id"])
        # Возвращаем сохраненное состояние из context.chat_data
        return context.chat_data.get("current_state", ConversationHandler.END)


async def process_user_id(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
):
    """Обработка полученного ID"""

    current_state = context.chat_data.get("current_state")
    logging.info(f"Processing user ID {user_id} for state {current_state}")

    user_data = load_users()

    if current_state == States.AWAIT_USER_ID_ADD:
        if user_id in user_data["allowed_users"]:
            await update.message.reply_text("ℹ️ Пользователь уже существует")
        else:
            user_data["allowed_users"].append(user_id)
            save_users(user_data)
            await update.message.reply_text(f"✅ Пользователь {user_id} добавлен")
        # Очищаем состояние после завершения
        del context.chat_data["current_state"]
        return ConversationHandler.END

    elif current_state == States.AWAIT_USER_ID_REMOVE:
        if user_id in user_data["allowed_users"]:
            user_data["allowed_users"].remove(user_id)
            save_users(user_data)
            await update.message.reply_text(f"✅ Пользователь {user_id} удален")
        else:
            await update.message.reply_text("ℹ️ Пользователь не найден")
        # Очищаем состояние после завершения
        del context.chat_data["current_state"]
        return ConversationHandler.END

    await update.message.reply_text("⚠️ Неизвестное состояние")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена операции"""
    if "current_state" in context.chat_data:
        del context.chat_data["current_state"]
    await update.message.reply_text(
        Config.TEXTS["cancel_message"], reply_markup=keyboard
    )
    return ConversationHandler.END


def setup_job_queue(application: Application):
    """Настройка периодических задач"""
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(
            update_cache,
            interval=Config.CACHE_UPDATE_INTERVAL,
            first=10,  # Первое обновление через 10 секунд после старта
        )


def configure_logging():
    """Настройка системы логирования"""
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.DEBUG,
        handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
    )


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = load_users()
    user_id = update.effective_user.id
    text = update.message.text

    if text in [Config.TEXTS["add_button"], Config.TEXTS["remove_button"]]:
        if user_id != user_data["admin_id"]:
            await update.message.reply_text(Config.TEXTS["admin_only"])
            return

    if text == Config.TEXTS["main_button"]:
        return await get_result(update, context)
    elif text == Config.TEXTS["add_button"]:
        return await add_user(update, context)
    elif text == Config.TEXTS["remove_button"]:
        return await remove_user(update, context)


def main():
    """Основная функция приложения"""
    configure_logging()

    try:
        application = (
            Application.builder()
            .token(Config.TELEGRAM_TOKEN)
            .post_init(init_cache)
            .build()
        )

        setup_job_queue(application)

        conv_handler = ConversationHandler(
            entry_points=[
                MessageHandler(
                    filters.TEXT & filters.Regex(rf"^{Config.TEXTS['add_button']}$"),
                    add_user,
                ),
                MessageHandler(
                    filters.TEXT & filters.Regex(rf"^{Config.TEXTS['remove_button']}$"),
                    remove_user,
                ),
            ],
            states={
                States.AWAIT_USER_ID_ADD: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND, handle_user_id_input
                    )
                ],
                States.AWAIT_USER_ID_REMOVE: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND, handle_user_id_input
                    )
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        application.add_handler(conv_handler)
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons)
        )
        application.add_handler(CommandHandler("start", start))
        application.add_handler(
            MessageHandler(
                filters.TEXT & filters.Regex(rf"^{Config.TEXTS['main_button']}$"),
                get_result,
            )
        )

        application.run_polling()

    except Exception as e:
        logging.error(f"Критическая ошибка: {str(e)}")
        raise


if __name__ == "__main__":
    main()
