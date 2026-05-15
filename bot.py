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

# ---------------- STATE ----------------
workouts = {}


# ---------------- START ----------------

@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "🏋️ Voice Gym Bot\n\n"
        "🎤 Команды:\n"
        "• начать / старт / начать тренировку\n"
        "• конец / финиш / конец тренировки\n"
        "• отмена / удалить / назад\n\n"
        "🎤 Примеры упражнений:\n"
        "• жим 60 кг 10\n"
        "• пресс 15\n"
        "• 10 (добавится к последнему упражнению)\n\n"
        "📄 После 'конец' будет PDF"
    )


# ---------------- COMMAND NORMALIZER ----------------

def normalize_command(text: str):
    text = text.lower().strip()

    start_cmds = ["start", "начать", "начать тренировку", "старт"]
    finish_cmds = ["finish", "конец", "конец тренировки", "финиш", "закончить", "закончить тренировку"]
    undo_cmds = ["undo", "отмена", "удалить", "назад"]

    if text in start_cmds:
        return "start"
    if text in finish_cmds:
        return "finish"
    if text in undo_cmds:
        return "undo"

    return None


# ---------------- FINISH ----------------

async def finish_workout(message: Message):
    user_id = message.from_user.id

    if user_id not in workouts or not workouts[user_id]["data"]:
        await message.answer("❌ Нет активной тренировки.")
        return

    data = workouts[user_id]["data"]

    max_sets = max(len(v) for v in data.values())

    table_data = [["Exercise"] + [f"Set {i+1}" for i in range(max_sets)]]

    for exercise, sets in data.items():
        row = [exercise]

        for s in sets:
            reps = s["reps"]
            weight = s["weight"]

            if weight:
                row.append(f"{weight}kg × {reps}")
            else:
                row.append(str(reps))

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


# ---------------- VOICE ----------------

@dp.message(F.voice)
async def voice_handler(message: Message):
    user_id = message.from_user.id
    voice = message.voice

    file = await bot.get_file(voice.file_id)

    os.makedirs("voices", exist_ok=True)
    path = f"voices/{voice.file_id}.ogg"

    await bot.download_file(file.file_path, path)

    await message.answer("🎤 Processing...")

    audio = open(path, "rb")

    transcription = client.audio.transcriptions.create(
        model="gpt-4o-mini-transcribe",
        file=audio
    )

    text = transcription.text
    audio.close()
    os.remove(path)

    # ---------------- LOCAL COMMANDS (NO GPT) ----------------

    cmd = normalize_command(text)

    if user_id not in workouts:
        workouts[user_id] = {
            "data": {},
            "current": None
        }

    state = workouts[user_id]

    if cmd == "start":
        workouts[user_id] = {"data": {}, "current": None}
        return await message.answer("🏋️ Тренировка начата")

    if cmd == "finish":
        return await finish_workout(message)

    if cmd == "undo":
        if state["data"]:
            last_ex = list(state["data"].keys())[-1]
            state["data"][last_ex].pop()

            if not state["data"][last_ex]:
                del state["data"][last_ex]

            await message.answer("↩️ Последний подход удалён")
        return

    # ---------------- GPT ONLY FOR EXERCISES ----------------

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {
                "role": "system",
                "content": """
You extract workout data.

Return ONLY JSON:

{
  "sets": [
    {
      "exercise": "string",
      "reps": number,
      "weight": number or null
    }
  ]
}

If no exercise found return empty list.
"""
            },
            {"role": "user", "content": text}
        ]
    )

    parsed = json.loads(response.choices[0].message.content)

    sets = parsed.get("sets", [])

    for item in sets:
        exercise = item["exercise"].lower().strip()
        reps = item.get("reps")
        weight = item.get("weight")

        if exercise not in state["data"]:
            state["data"][exercise] = []

        state["data"][exercise].append({
            "reps": reps,
            "weight": weight
        })

        await message.answer(
            f"🏋️ {exercise}\n"
            f"💪 {weight}kg × {reps}"
        )


# ---------------- MAIN ----------------

async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())