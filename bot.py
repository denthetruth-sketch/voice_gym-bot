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

# Регистрируем шрифт для русского текста в PDF
pdfmetrics.registerFont(TTFont("DejaVu", "DejaVuSans.ttf"))

# Хранилище тренировок
workouts = {}


@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "🏋️ Workout bot is ready!\n\n"
        "Send me a voice message with your exercise."
    )


@dp.message(F.text == "/finish")
async def finish_workout(message: Message):
    user_id = message.from_user.id

    if user_id not in workouts:
        await message.answer("❌ No active workout.")
        return

    workout_data = workouts[user_id]["data"]

    # Максимальное количество подходов
    max_sets = max(len(sets) for sets in workout_data.values())

    # Таблица
    table_data = [["Exercise"]]

    for i in range(max_sets):
        table_data[0].append(f"Set {i + 1}")

    for exercise, sets in workout_data.items():
        row = [exercise]

        for reps in sets:
            row.append(str(reps))

        while len(row) < max_sets + 1:
            row.append("")

        table_data.append(row)

    # PDF
    pdf_path = f"workout_{user_id}.pdf"

    doc = SimpleDocTemplate(pdf_path)
    table = Table(table_data)

    style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 1, colors.black),
        ("FONTNAME", (0, 0), (-1, -1), "DejaVu"),
    ])

    table.setStyle(style)
    elements = [table]

    doc.build(elements)

    # Отправляем PDF
    pdf_file = FSInputFile(pdf_path)
    await message.answer_document(pdf_file)

    os.remove(pdf_path)

    # Очищаем тренировку
    del workouts[user_id]


@dp.message(F.voice)
async def voice_handler(message: Message):
    voice = message.voice

    file_id = voice.file_id
    file = await bot.get_file(file_id)
    file_path = file.file_path

    os.makedirs("voices", exist_ok=True)
    save_path = f"voices/{file_id}.ogg"

    # Скачиваем голосовое
    await bot.download_file(file_path, save_path)

    await message.answer("🎤 Voice received. Transcribing...")

    # Speech-to-text
    audio_file = open(save_path, "rb")

    transcription = client.audio.transcriptions.create(
        model="gpt-4o-mini-transcribe",
        file=audio_file
    )

    text = transcription.text

    audio_file.close()
    os.remove(save_path)

    # GPT parsing
    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {
                "role": "system",
                "content": """
You extract workout data from user messages.

Return ONLY valid JSON.

If the user only says a number,
exercise should be null.

Format:
{
  "exercise": "string or null",
  "reps": number
}
"""
            },
            {
                "role": "user",
                "content": text
            }
        ]
    )

    result = response.choices[0].message.content
    parsed = json.loads(result)

    user_id = message.from_user.id
    exercise = parsed["exercise"]
    reps = parsed["reps"]

    # Нормализация названия упражнения
    if exercise:
        exercise = exercise.lower().strip()

    # Создаем тренировку пользователя
    if user_id not in workouts:
        workouts[user_id] = {
            "current_exercise": None,
            "data": {}
        }

    user_workout = workouts[user_id]

    # Если exercise пустой — используем текущее
    if not exercise or str(exercise).lower() == "null":
        exercise = user_workout["current_exercise"]

    # Если пользователь сказал только число
    if not exercise:
        await message.answer("❌ Say an exercise first.")
        return

    # Новое упражнение
    if exercise != user_workout["current_exercise"]:
        user_workout["current_exercise"] = exercise

        if exercise not in user_workout["data"]:
            user_workout["data"][exercise] = []

    # Добавляем подход
    user_workout["data"][exercise].append(reps)

    set_number = len(user_workout["data"][exercise])

    # Ответ
    await message.answer(
        f"🏋️ {exercise}\n"
        f"🔁 Set {set_number}\n"
        f"💪 {reps} reps"
    )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())