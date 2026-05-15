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
        "Examples:\n"
        "  «жим лежа 60кг 10 раз»\n"
        "  «жим лежа 60кг 20 раз, отжимания 20 раз, приседания 30кг 10 раз»"
    )


@dp.message(F.text == "/finish")
async def finish_workout(message: Message):
    user_id = message.from_user.id

    if user_id not in workouts:
        await message.answer("❌ No active workout.")
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

    await message.answer_document(FSInputFile(pdf_path))
    os.remove(pdf_path)
    del workouts[user_id]


@dp.message(F.voice)
async def voice_handler(message: Message):
    voice = message.voice
    file_id = voice.file_id
    file = await bot.get_file(file_id)

    os.makedirs("voices", exist_ok=True)
    save_path = f"voices/{file_id}.ogg"
    await bot.download_file(file.file_path, save_path)

    await message.answer("🎤 Voice received. Transcribing...")

    with open(save_path, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=audio_file
        )
    text = transcription.text
    os.remove(save_path)

    # ── GPT parsing → всегда возвращает массив ────────────────────────────────
    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {
                "role": "system",
                "content": """
You extract workout data from user messages (Russian or English).

Return ONLY a valid JSON array, no markdown, no explanation.

Each element represents one exercise entry:
- "exercise": exercise name in lowercase, or null if not mentioned
- "reps": number of repetitions (integer)
- "weight": weight in kg (number) if mentioned, otherwise null

The user may say one or multiple exercises in a single message — return ALL of them.

Examples:

Input: "жим лежа 60кг 10 раз"
Output: [{"exercise": "жим лежа", "reps": 10, "weight": 60}]

Input: "жим лежа 60кг 20 раз, отжимания 20 раз, приседания 30кг 10 раз"
Output: [
  {"exercise": "жим лежа",    "reps": 20, "weight": 60},
  {"exercise": "отжимания",   "reps": 20, "weight": null},
  {"exercise": "приседания",  "reps": 10, "weight": 30}
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
            },
            {"role": "user", "content": text}
        ]
    )

    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    entries = json.loads(raw)  # теперь всегда список

    user_id = message.from_user.id

    if user_id not in workouts:
        workouts[user_id] = {
            "current_exercise": None,
            "current_weight": None,
            "data": {}
        }

    user_workout = workouts[user_id]
    reply_lines = []

    for parsed in entries:
        exercise = parsed.get("exercise")
        reps = parsed.get("reps")
        weight = parsed.get("weight")

        if exercise:
            exercise = exercise.lower().strip()

        # Если упражнение не названо — берём текущее
        if not exercise or str(exercise).lower() == "null":
            exercise = user_workout["current_exercise"]

        if not exercise:
            await message.answer("❌ Say an exercise first.")
            return

        # Новое упражнение
        if exercise != user_workout["current_exercise"]:
            user_workout["current_exercise"] = exercise
            user_workout["current_weight"] = weight
            if exercise not in user_workout["data"]:
                user_workout["data"][exercise] = []
        else:
            # То же упражнение: вес не назван → берём предыдущий
            if weight is None:
                weight = user_workout["current_weight"]
            else:
                user_workout["current_weight"] = weight

        user_workout["data"][exercise].append({"reps": reps, "weight": weight})
        set_number = len(user_workout["data"][exercise])

        weight_str = f"{weight}kg × " if weight is not None else ""
        reply_lines.append(f"🏋️ {exercise} | Set {set_number} | 💪 {weight_str}{reps} reps")

    await message.answer("\n".join(reply_lines))


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())