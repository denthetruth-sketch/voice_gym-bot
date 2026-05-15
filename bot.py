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


@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "🏋️ Workout bot is ready!\n\n"
        "Send me a voice message with your exercise.\n"
        "Example: «жим лежа 60кг 10 раз»"
    )


@dp.message(F.text == "/finish")
async def finish_workout(message: Message):
    user_id = message.from_user.id

    if user_id not in workouts:
        await message.answer("❌ No active workout.")
        return

    workout_data = workouts[user_id]["data"]

    max_sets = max(len(sets) for sets in workout_data.values())

    # Заголовок таблицы
    table_data = [["Exercise"] + [f"Set {i + 1}" for i in range(max_sets)]]

    for exercise, sets in workout_data.items():
        row = [exercise]
        for entry in sets:
            # entry = {"reps": 10, "weight": 60} or {"reps": 10, "weight": null}
            reps = entry["reps"]
            weight = entry["weight"]
            if weight is not None:
                row.append(f"{weight}kg × {reps}")
            else:
                row.append(str(reps))
        # Дополняем пустыми ячейками
        while len(row) < max_sets + 1:
            row.append("")
        table_data.append(row)

    pdf_path = f"workout_{user_id}.pdf"
    doc = SimpleDocTemplate(pdf_path)
    table = Table(table_data)

    style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 1, colors.black),
        ("FONTNAME", (0, 0), (-1, -1), "DejaVu"),
    ])
    table.setStyle(style)
    doc.build([table])

    pdf_file = FSInputFile(pdf_path)
    await message.answer_document(pdf_file)

    os.remove(pdf_path)
    del workouts[user_id]


@dp.message(F.voice)
async def voice_handler(message: Message):
    voice = message.voice
    file_id = voice.file_id
    file = await bot.get_file(file_id)
    file_path = file.file_path

    os.makedirs("voices", exist_ok=True)
    save_path = f"voices/{file_id}.ogg"

    await bot.download_file(file_path, save_path)
    await message.answer("🎤 Voice received. Transcribing...")

    audio_file = open(save_path, "rb")
    transcription = client.audio.transcriptions.create(
        model="gpt-4o-mini-transcribe",
        file=audio_file
    )
    text = transcription.text
    audio_file.close()
    os.remove(save_path)

    # ── GPT parsing ───────────────────────────────────────────────────────────
    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {
                "role": "system",
                "content": """
You extract workout data from user messages (Russian or English).

Return ONLY valid JSON, no markdown.

Rules:
- "exercise": exercise name in lowercase, or null if only a number/weight was said
- "reps": number of repetitions (integer)
- "weight": weight in kg (number) if mentioned, otherwise null

Examples:
  "жим лежа 60кг 10 раз" → {"exercise": "жим лежа", "reps": 10, "weight": 60}
  "присед 80 килограмм 5" → {"exercise": "присед", "reps": 5, "weight": 80}
  "20 раз"                → {"exercise": null, "reps": 20, "weight": null}
  "15"                    → {"exercise": null, "reps": 15, "weight": null}
  "отжимания 20"          → {"exercise": "отжимания", "reps": 20, "weight": null}

Format:
{
  "exercise": "string or null",
  "reps": number,
  "weight": number or null
}
"""
            },
            {
                "role": "user",
                "content": text
            }
        ]
    )

    result = response.choices[0].message.content.strip()
    # Защита от markdown-обёрток
    result = result.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(result)

    user_id = message.from_user.id
    exercise = parsed.get("exercise")
    reps = parsed.get("reps")
    weight = parsed.get("weight")  # float или None

    if exercise:
        exercise = exercise.lower().strip()

    # Инициализируем тренировку пользователя
    if user_id not in workouts:
        workouts[user_id] = {
            "current_exercise": None,
            "current_weight": None,   # ← запоминаем последний вес
            "data": {}
        }

    user_workout = workouts[user_id]

    # Если упражнение не названо — берём текущее
    if not exercise or str(exercise).lower() == "null":
        exercise = user_workout["current_exercise"]

    if not exercise:
        await message.answer("❌ Say an exercise first.")
        return

    # Новое упражнение → обновляем current_exercise и сбрасываем вес
    if exercise != user_workout["current_exercise"]:
        user_workout["current_exercise"] = exercise
        user_workout["current_weight"] = weight  # запоминаем вес нового упражнения
        if exercise not in user_workout["data"]:
            user_workout["data"][exercise] = []
    else:
        # То же упражнение:
        # если вес не назван — используем вес предыдущего подхода
        if weight is None:
            weight = user_workout["current_weight"]
        else:
            # назвали новый вес — обновляем
            user_workout["current_weight"] = weight

    # Сохраняем подход как dict
    entry = {"reps": reps, "weight": weight}
    user_workout["data"][exercise].append(entry)

    set_number = len(user_workout["data"][exercise])

    # Формируем ответ
    weight_str = f"{weight}kg × " if weight is not None else ""
    await message.answer(
        f"🏋️ {exercise}\n"
        f"🔁 Set {set_number}\n"
        f"💪 {weight_str}{reps} reps"
    )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())