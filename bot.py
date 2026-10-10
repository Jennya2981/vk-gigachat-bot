# -*- coding: utf-8 -*-
"""Чат-бот ВКонтакте «Кристи» для сообщества «Астропазл».

Бот слушает личные сообщения сообщества ВКонтакте (Long Poll API)
и отвечает через GigaChat API. Все секреты берутся из переменных
окружения (файл .env), в код они не записываются.
"""

import os
import sys
import random
import logging
import traceback

from dotenv import load_dotenv
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole

from system_prompt import build_system_prompt

load_dotenv()

# Чтобы русские логи корректно отображались в консоли Windows.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ============================ Конфигурация ============================

def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "да")


# --- GigaChat ---
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2")
GIGACHAT_BASE_URL = os.getenv("GIGACHAT_BASE_URL", "https://api.giga.chat/v1")
GIGACHAT_CA_BUNDLE_FILE = os.getenv("GIGACHAT_CA_BUNDLE_FILE")
GIGACHAT_VERIFY_SSL = env_bool("GIGACHAT_VERIFY_SSL", True)

# --- ВКонтакте ---
VK_TOKEN = os.getenv("VK_TOKEN")
VK_GROUP_ID = os.getenv("VK_GROUP_ID")

# --- Поведение бота ---
MAX_HISTORY_MESSAGES = 12

KNOWLEDGE_BASE_PATH = os.path.join(os.path.dirname(__file__), "knowledge_base.md")


def load_knowledge_base() -> str:
    """Загружает базу знаний из файла knowledge_base.md."""
    try:
        with open(KNOWLEDGE_BASE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        logger.info("База знаний загружена (%d символов)", len(content))
        return content
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось загрузить базу знаний: %s", exc)
        return ""


def create_gigachat_client() -> GigaChat:
    """Создаёт клиент GigaChat с учётом настроек TLS из окружения."""
    kwargs = {
        "base_url": GIGACHAT_BASE_URL,
        "credentials": GIGACHAT_CREDENTIALS,
        "scope": GIGACHAT_SCOPE,
    }

    if GIGACHAT_CA_BUNDLE_FILE:
        # Рекомендуемый безопасный вариант: корневой сертификат НУЦ Минцифры.
        kwargs["ca_bundle_file"] = GIGACHAT_CA_BUNDLE_FILE
    elif not GIGACHAT_VERIFY_SSL:
        # Отключение проверки TLS (только если так задано в .env).
        kwargs["verify_ssl_certs"] = False

    return GigaChat(**kwargs)


# ============================ Логика ответов ============================

WELCOME_TEXT = (
    "Здравствуйте! Меня зовут Кристи, я ассистент астрокоуча Евгении Вереникиной "
    "из сообщества «Астропазл» ✨\n\n"
    "Помогу разобраться в форматах работы и подобрать подходящую консультацию. "
    "Расскажите, что вас сейчас волнует?"
)

HELP_TEXT = (
    "Я могу рассказать про услуги астрокоуча Евгении Вереникиной:\n"
    "• бесплатную первичную консультацию (30 минут);\n"
    "• консультацию «Актуальный вопрос» — 2 000 ₽;\n"
    "• полную расшифровку натальной карты — 5 000 ₽;\n"
    "• тематические сессии — 4 000 ₽;\n"
    "• сопровождение «Живу по карте» — 15 000 ₽/мес;\n"
    "• программу «Глубокое погружение» — 50 000 ₽.\n\n"
    "Напишите, какой у вас запрос, — и я подскажу, с чего лучше начать."
)

RESET_TEXT = "Хорошо, начнём диалог заново. Чем я могу вам помочь?"

ERROR_TEXT = (
    "Прошу прощения, у меня возникла небольшая техническая заминка. "
    "Пожалуйста, напишите ещё раз через минуту или свяжитесь с Евгенией напрямую."
)


def resolve_group_id(vk, raw_group: str) -> int:
    """Приводит VK_GROUP_ID к числу.

    Принимает числовой ID (можно со знаком минус) или короткое имя
    сообщества (screen name), например «astropuzzzle».
    """
    raw_group = raw_group.strip()
    if raw_group.lstrip("-").isdigit():
        return abs(int(raw_group))
    group_info = vk.groups.getById(group_id=raw_group)[0]
    return int(group_info["id"])


def build_chat(system_prompt: str, history: list) -> Chat:
    """Собирает объект Chat из системного промпта и истории пользователя."""
    messages = [Messages(role=MessagesRole.SYSTEM, content=system_prompt)]
    for item in history:
        role = MessagesRole.USER if item["role"] == "user" else MessagesRole.ASSISTANT
        messages.append(Messages(role=role, content=item["content"]))
    return Chat(model=GIGACHAT_MODEL, messages=messages)


def ask_gigachat(client: GigaChat, system_prompt: str, history: list) -> str:
    """Отправляет историю в GigaChat и возвращает текст ответа Кристи."""
    chat = build_chat(system_prompt, history)
    response = client.chat(chat)
    return response.choices[0].message.content.strip()


# ============================ Точка входа ============================

def main() -> None:
    logger.info("=== Запуск бота Кристи ===")

    missing = [
        name
        for name, value in (
            ("GIGACHAT_CREDENTIALS", GIGACHAT_CREDENTIALS),
            ("VK_TOKEN", VK_TOKEN),
            ("VK_GROUP_ID", VK_GROUP_ID),
        )
        if not value
    ]
    if missing:
        logger.error(
            "Не заданы обязательные переменные окружения: %s. "
            "Заполните файл .env по образцу .env.example.",
            ", ".join(missing),
        )
        return

    knowledge_base = load_knowledge_base()
    if not knowledge_base:
        logger.error("База знаний пуста. Проверьте файл knowledge_base.md.")
        return
    system_prompt = build_system_prompt(knowledge_base)

    try:
        gigachat_client = create_gigachat_client()
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось создать клиент GigaChat: %s", exc)
        return

    try:
        vk_session = vk_api.VkApi(token=VK_TOKEN)
        vk = vk_session.get_api()
        group_id = resolve_group_id(vk, VK_GROUP_ID)
        group_info = vk.groups.getById(group_id=group_id)[0]
        logger.info("ВК подключён. Сообщество: %s (id=%s)", group_info["name"], group_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось подключиться к ВКонтакте: %s", exc)
        return

    conversation_history: dict[int, list] = {}
    longpoll = VkBotLongPoll(vk_session, group_id)
    logger.info("Слушаю сообщения сообщества...")

    for event in longpoll.listen():
        try:
            if event.type != VkBotEventType.MESSAGE_NEW:
                # Служебные события (набор текста, прочтение и т.п.) — только в DEBUG.
                logger.debug("Событие (не сообщение): %s", event.type)
                continue

            message = event.message
            user_id = message["from_id"]
            text = (message.get("text") or "").strip()
            logger.info(
                "Событие message_new: from_id=%s, out=%s, text=%r",
                user_id,
                message.get("out"),
                text,
            )
            if not text:
                continue

            history = conversation_history.setdefault(user_id, [])
            command = text.lower()

            if command in ("/start", "начать", "привет", "здравствуйте", "здравствуй"):
                history.clear()
                reply = WELCOME_TEXT
            elif command in ("/help", "помощь"):
                reply = HELP_TEXT
            elif command in ("/reset", "сброс"):
                history.clear()
                reply = RESET_TEXT
            else:
                history.append({"role": "user", "content": text})
                try:
                    reply = ask_gigachat(
                        gigachat_client, system_prompt, history[-MAX_HISTORY_MESSAGES:]
                    )
                    history.append({"role": "assistant", "content": reply})
                    # Не даём истории расти бесконечно.
                    if len(history) > MAX_HISTORY_MESSAGES * 2:
                        del history[:-MAX_HISTORY_MESSAGES]
                except Exception as exc:  # noqa: BLE001
                    logger.error("Ошибка GigaChat: %s\n%s", exc, traceback.format_exc())
                    reply = ERROR_TEXT

            logger.info("<<< для %s: %s", user_id, reply[:80].replace("\n", " "))
            vk.messages.send(
                user_id=user_id,
                message=reply,
                random_id=random.randint(1, 2**31),
            )

        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка обработки события: %s\n%s", exc, traceback.format_exc())


if __name__ == "__main__":
    main()
