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


# ---------------- START ----------------

@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "🏋️ Voice Gym Bot\n\n"
        "How to use:\n\n"
        "🎤 Say exercises:\n"
        "• Bench press 60kg 10 reps\n"
        "• Push ups 15\n"
        "• 10 (adds to last exercise)\n\n"
        "🎤 Voice commands:\n"
        "• start workout\n"
        "• finish workout\n"
        "• undo last\n\n"
        "📄 Send voice → get PDF workout"
    )


# ---------------- FINISH ----------------

async def finish_workout(message: Message):
    user_id = message.from_user.id

    if user_id not in workouts:
        await message.answer("❌ No active workout.")
        return

    data = workouts[user_id]["data"]

    max_sets = max((len(v) for v in data.values()), default=0)

    table_data = [["Exercise"] + [f"Set {i+1}" for i in range(max_sets)]]

    for exercise, sets in data.items():
        row = [exercise]

        for s in sets:
            if isinstance(s, dict):
                reps = s.get("reps", "")
                weight = s.get("weight", "")
                row.append(f"{weight}x{reps}" if weight else str(reps))
            else:
                row.append(str(s))

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

    # ---------------- GPT ----------------

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {
                "role": "system",
                "content": """
You are a fitness assistant.

Return ONLY valid JSON.

You may return:

1) Multiple sets:
{
  "type": "sets",
  "sets": [
    {
      "exercise": "string",
      "reps": number,
      "weight": number or null
    }
  ]
}

2) Command:
{
  "type": "command",
  "command": "start" | "finish" | "undo"
}
"""
            },
            {"role": "user", "content": text}
        ]
    )

    parsed = json.loads(response.choices[0].message.content)

    # ---------------- INIT ----------------

    if user_id not in workouts:
        workouts[user_id] = {
            "current_exercise": None,
            "data": [],
            "history": []
        }

    state = workouts[user_id]

    # ---------------- COMMANDS ----------------

    if parsed["type"] == "command":
        cmd = parsed["command"]

        if cmd == "finish":
            return await finish_workout(message)

        if cmd == "start":
            workouts[user_id] = {
                "current_exercise": None,
                "data": [],
                "history": []
            }
            return await message.answer("🏋️ Workout started")

        if cmd == "undo":
            if state["data"]:
                last = state["data"].pop()
                await message.answer("↩️ Last set removed")
            return

    # ---------------- SETS ----------------

    sets = parsed.get("sets", [])

    for item in sets:
        exercise = item["exercise"].lower().strip()
        reps = item.get("reps")
        weight = item.get("weight")

        state["data"].append({
            "exercise": exercise,
            "reps": reps,
            "weight": weight
        })

        state["history"].append(item)

        await message.answer(
            f"🏋️ {exercise}\n"
            f"💪 {weight}kg × {reps}"
        )


# ---------------- MAIN ----------------

async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())