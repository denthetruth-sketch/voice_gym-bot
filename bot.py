import asyncio
import os
import json

from reportlab.platypus import SimpleDocTemplate, Table
from reportlab.lib import colors
from reportlab.platypus.tables import TableStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import CommandStart

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

pdfmetrics.registerFont(TTFont("DejaVu", "DejaVuSans.ttf"))

workouts = {}

# ── intent keywords ───────────────────────────────────────────────────────────

START_KEYWORDS = {
    "начать", "начать тренировку", "старт", "поехали",
    "погнали", "начинаем", "старт тренировки", "start", "lets go", "let's go"
}

FINISH_KEYWORDS = {
    "финиш", "конец", "конец тренировки", "стоп", "закончить",
    "завершить", "финиш тренировки", "finish", "всё", "все", "закончи"
}

UNDO_KEYWORDS = {
    "отменить", "удалить", "убрать", "undo", "отмена",
    "назад", "удали", "отмени", "убери"
}


def detect_intent(text: str) -> str:
    normalized = text.strip().lower()
    for phrase in sorted(START_KEYWORDS, key=len, reverse=True):
        if phrase in normalized:
            return "start"
    for phrase in sorted(FINISH_KEYWORDS, key=len, reverse=True):
        if phrase in normalized:
            return "finish"
    for phrase in sorted(UNDO_KEYWORDS, key=len, reverse=True):
        if phrase in normalized:
            return "undo"
    return "exercise"


# ── GPT prompt ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You extract workout data from user messages (Russian or English).

Return ONLY a valid JSON array, no markdown, no explanation.

Each element represents one exercise entry:
- "exercise": canonical exercise name in lowercase Russian/English.
  IMPORTANT: always use the SAME canonical name for the same exercise.
  For example "жим лежа", "жим", "жим лёжа" → always return "жим лежа".
  Return null only if no exercise was mentioned at all (e.g. user said only a number).
- "reps": number of repetitions (integer)
- "weight": weight in kg (number) ONLY if explicitly mentioned by user, otherwise null.
  If user did not say weight — return null, do NOT invent it.

The user may say one or multiple exercises — return ALL of them.

Examples:

Input: "жим лежа 60кг 10 раз"
Output: [{"exercise": "жим лежа", "reps": 10, "weight": 60}]

Input: "жим лежа 30 раз"
Output: [{"exercise": "жим лежа", "reps": 30, "weight": null}]

Input: "жим 8 раз"
Output: [{"exercise": "жим лежа", "reps": 8, "weight": null}]

Input: "жим лежа 60кг 20 раз, отжимания 20 раз, приседания 30кг 10 раз"
Output: [
  {"exercise": "жим лежа",   "reps": 20, "weight": 60},
  {"exercise": "отжимания",  "reps": 20, "weight": null},
  {"exercise": "приседания", "reps": 10, "weight": 30}
]

Input: "20 раз"
Output: [{"exercise": null, "reps": 20, "weight": null}]

Input: "присед 80кг 5, 5, 5"
Output: [
  {"exercise": "присед", "reps": 5, "weight": 80},
  {"exercise": "присед", "reps": 5, "weight": 80},
  {"exercise": "присед", "reps": 5, "weight": 80}
]
"""


# ── helpers ───────────────────────────────────────────────────────────────────

def apply_entry(user_workout: dict, exercise: str, reps: int, weight) -> tuple[int, any]:
    """
    Добавляет подход в тренировку с правильной логикой веса.
    Возвращает (set_number, weight_использованный).
    """
    current_ex = user_workout["current_exercise"]

    if exercise != current_ex:
        # ── новое упражнение ──────────────────────────────────────────────────
        user_workout["current_exercise"] = exercise
        # вес: берём из сообщения, если не сказан — сбрасываем (новое упражнение)
        user_workout["current_weight"] = weight
        if exercise not in user_workout["data"]:
            user_workout["data"][exercise] = []
    else:
        # ── то же упражнение ──────────────────────────────────────────────────
        if weight is None:
            # вес не назван → берём предыдущий
            weight = user_workout["current_weight"]
        else:
            # назван новый вес → обновляем
            user_workout["current_weight"] = weight

    entry = {"reps": reps, "weight": weight}
    user_workout["data"][exercise].append(entry)
    user_workout["history"].append((exercise, entry))

    set_number = len(user_workout["data"][exercise])
    return set_number, weight


# ── handlers ──────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def start_handler(message: Message):
    user_id = message.from_user.id
    workouts[user_id] = {
        "current_exercise": None,
        "current_weight": None,
        "data": {},
        "history": []
    }
    await message.answer(
        "🏋️ <b>Тренировка начата!</b>\n\n"
        "Отправляй голосовые сообщения с упражнениями.\n\n"
        "<b>Примеры:</b>\n"
        "  • «жим лежа 60кг 10 раз»\n"
        "  • «жим лежа 30 раз» — второй подход, вес подтянется автоматически\n"
        "  • «присед 80кг 5, пресс 20, отжимания 15»\n\n"
        "<b>Голосовые команды:</b>\n"
        "  • <b>Старт</b> / поехали / начать — начать тренировку\n"
        "  • <b>Финиш</b> / конец / стоп — завершить и получить PDF\n"
        "  • <b>Отменить</b> / удалить / убрать — удалить последний подход\n\n"
        "<b>Текстовые команды:</b>\n"
        "  /finish — завершить тренировку\n"
        "  /undo — удалить последний подход",
        parse_mode="HTML"
    )


async def finish_workout(message: Message):
    user_id = message.from_user.id

    if user_id not in workouts or not workouts[user_id]["data"]:
        await message.answer("❌ Нет активной тренировки.")
        return

    workout_data = workouts[user_id]["data"]
    max_sets = max(len(sets) for sets in workout_data.values())

    table_data = [["Exercise"] + [f"Set {i + 1}" for i in range(max_sets)]]

    for exercise, sets in workout_data.items():
        row = [exercise]
        for entry in sets:
            reps = entry["reps"]
            weight = entry["weight"]
            row.append(f"{weight}kg × {reps}" if weight is not None else str(reps))
        while len(row) < max_sets + 1:
            row.append("")
        table_data.append(row)

    pdf_path = f"workout_{user_id}.pdf"
    doc = SimpleDocTemplate(pdf_path)
    table = Table(table_data)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 1, colors.black),
        ("FONTNAME", (0, 0), (-1, -1), "DejaVu"),
    ]))
    doc.build([table])

    await message.answer("📊 Тренировка завершена! Генерирую PDF...")
    await message.answer_document(FSInputFile(pdf_path))
    os.remove(pdf_path)
    del workouts[user_id]


async def undo_last(message: Message):
    user_id = message.from_user.id

    if user_id not in workouts:
        await message.answer("❌ Нет активной тренировки.")
        return

    history = workouts[user_id]["history"]

    if not history:
        await message.answer("❌ Нечего отменять.")
        return

    exercise, entry = history.pop()
    sets = workouts[user_id]["data"].get(exercise, [])

    for i in range(len(sets) - 1, -1, -1):
        if sets[i] == entry:
            sets.pop(i)
            break

    if not sets:
        del workouts[user_id]["data"][exercise]
        if workouts[user_id]["current_exercise"] == exercise:
            workouts[user_id]["current_exercise"] = None
            workouts[user_id]["current_weight"] = None

    weight_str = f"{entry['weight']}kg × " if entry["weight"] is not None else ""
    await message.answer(
        f"↩️ Удалён последний подход:\n"
        f"🏋️ {exercise} | 💪 {weight_str}{entry['reps']} reps"
    )


# ── команды ───────────────────────────────────────────────────────────────────

@dp.message(F.text == "/finish")
async def cmd_finish(message: Message):
    await finish_workout(message)


@dp.message(F.text == "/undo")
async def cmd_undo(message: Message):
    await undo_last(message)


# ── голосовой хендлер ─────────────────────────────────────────────────────────

@dp.message(F.voice)
async def voice_handler(message: Message):
    voice = message.voice
    file_id = voice.file_id
    file = await bot.get_file(file_id)

    os.makedirs("voices", exist_ok=True)
    save_path = f"voices/{file_id}.ogg"
    await bot.download_file(file.file_path, save_path)

    await message.answer("🎤 Транскрибирую...")

    with open(save_path, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=audio_file
        )
    text = transcription.text
    os.remove(save_path)

    # ── intent без GPT ────────────────────────────────────────────────────────
    intent = detect_intent(text)

    if intent == "start":
        await start_handler(message)
        return

    if intent == "finish":
        await finish_workout(message)
        return

    if intent == "undo":
        await undo_last(message)
        return

    # ── проверяем что тренировка начата ───────────────────────────────────────
    user_id = message.from_user.id

    if user_id not in workouts:
        await message.answer(
            "⚠️ Тренировка не начата.\n"
            "Скажи «начать» или отправь /start."
        )
        return

    # ── GPT парсинг упражнений ────────────────────────────────────────────────
    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": text}
        ]
    )

    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    entries = json.loads(raw)

    user_workout = workouts[user_id]
    reply_lines = []

    for parsed in entries:
        exercise = parsed.get("exercise")
        reps = parsed.get("reps")
        weight = parsed.get("weight")

        if exercise:
            exercise = exercise.lower().strip()

        # упражнение не названо → берём текущее
        if not exercise or exercise == "null":
            exercise = user_workout["current_exercise"]

        if not exercise:
            await message.answer("❌ Сначала назови упражнение.")
            return

        set_number, used_weight = apply_entry(user_workout, exercise, reps, weight)

        weight_str = f"{used_weight}kg × " if used_weight is not None else ""
        reply_lines.append(
            f"🏋️ {exercise} | Set {set_number} | 💪 {weight_str}{reps} reps"
        )

    await message.answer("\n".join(reply_lines))


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())