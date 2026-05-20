import asyncio
import os
import json
from datetime import datetime

from reportlab.platypus import SimpleDocTemplate, Table, Spacer, Paragraph
from reportlab.platypus.tables import TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
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
  Return null only if no exercise was mentioned at all.
- "reps": number of repetitions (integer)
- "weight": weight in kg (number) ONLY if explicitly mentioned, otherwise null.

Examples:

Input: "жим лежа 60кг 10 раз"
Output: [{"exercise": "жим лежа", "reps": 10, "weight": 60}]

Input: "жим лежа 30 раз"
Output: [{"exercise": "жим лежа", "reps": 30, "weight": null}]

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

# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main() -> InlineKeyboardMarkup:
    """Главное меню — показывается на старте."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏋️ Начать тренировку", callback_data="start_workout")],
        [InlineKeyboardButton(text="⭐ Поддержать бота",   callback_data="support")],
    ])


def kb_workout() -> InlineKeyboardMarkup:
    """Меню во время тренировки."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Закончить тренировку",    callback_data="finish_workout")],
        [InlineKeyboardButton(text="❌ Удалить последний подход", callback_data="undo_last")],
    ])


# ── core logic ────────────────────────────────────────────────────────────────

def apply_entry(user_workout: dict, exercise: str, reps: int, weight) -> tuple[int, any]:
    current_ex = user_workout["current_exercise"]

    if exercise != current_ex:
        user_workout["current_exercise"] = exercise
        user_workout["current_weight"] = weight
        if exercise not in user_workout["data"]:
            user_workout["data"][exercise] = []
    else:
        if weight is None:
            weight = user_workout["current_weight"]
        else:
            user_workout["current_weight"] = weight

    entry = {"reps": reps, "weight": weight}
    user_workout["data"][exercise].append(entry)
    user_workout["history"].append((exercise, entry))

    return len(user_workout["data"][exercise]), weight


def init_workout(user_id: int) -> None:
    workouts[user_id] = {
        "current_exercise": None,
        "current_weight": None,
        "data": {},
        "history": [],
        "started_at": datetime.now(),
    }


# ── PDF generation ────────────────────────────────────────────────────────────

def build_pdf(user_id: int) -> str:
    workout_data = workouts[user_id]["data"]
    started_at: datetime = workouts[user_id].get("started_at", datetime.now())
    max_sets = max(len(s) for s in workout_data.values())

    pdf_path = f"workout_{user_id}.pdf"
    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    elements = []

    # ── заголовок ─────────────────────────────────────────────────────────────
    title_style = ParagraphStyle(
        "title",
        fontName="DejaVu",
        fontSize=20,
        textColor=colors.HexColor("#1a1a2e"),
        spaceAfter=4,
    )
    subtitle_style = ParagraphStyle(
        "subtitle",
        fontName="DejaVu",
        fontSize=10,
        textColor=colors.HexColor("#666666"),
        spaceAfter=20,
    )

    elements.append(Paragraph("💪 Workout Summary", title_style))
    elements.append(Paragraph(
        f"Дата: {started_at.strftime('%d %B %Y')}  ·  "
        f"Начало: {started_at.strftime('%H:%M')}  ·  "
        f"Конец: {datetime.now().strftime('%H:%M')}",
        subtitle_style,
    ))

    # ── таблица ───────────────────────────────────────────────────────────────
    header = ["Упражнение"] + [f"Сет {i + 1}" for i in range(max_sets)]
    table_data = [header]

    for exercise, sets in workout_data.items():
        row = [exercise.title()]
        for entry in sets:
            reps = entry["reps"]
            weight = entry["weight"]
            row.append(f"{weight}кг × {reps}" if weight is not None else str(reps))
        while len(row) < max_sets + 1:
            row.append("—")
        table_data.append(row)

    # ширины колонок
    ex_col_w = 5.5 * cm
    set_col_w = (A4[0] - 4 * cm - ex_col_w) / max(max_sets, 1)
    col_widths = [ex_col_w] + [set_col_w] * max_sets

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        # шапка
        ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#2d2d5e")),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "DejaVu"),
        ("FONTSIZE",      (0, 0), (-1, 0), 11),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 10),
        ("TOPPADDING",    (0, 0), (-1, 0), 10),
        # колонка упражнений
        ("BACKGROUND",    (0, 1), (0, -1), colors.HexColor("#f0f0f8")),
        ("FONTNAME",      (0, 1), (0, -1), "DejaVu"),
        ("FONTSIZE",      (0, 1), (0, -1), 10),
        # данные
        ("FONTNAME",      (1, 1), (-1, -1), "DejaVu"),
        ("FONTSIZE",      (1, 1), (-1, -1), 10),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f8f8fc")]),
        # сетка
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#ccccdd")),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("ALIGN",         (0, 1), (0, -1),  "LEFT"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 1), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
    ]))

    elements.append(table)

    # ── итог ──────────────────────────────────────────────────────────────────
    elements.append(Spacer(1, 16))
    footer_style = ParagraphStyle(
        "footer",
        fontName="DejaVu",
        fontSize=8,
        textColor=colors.HexColor("#aaaaaa"),
    )
    total_sets = sum(len(s) for s in workout_data.values())
    elements.append(Paragraph(
        f"Упражнений: {len(workout_data)}  ·  Всего подходов: {total_sets}  ·  VoiceGymBot",
        footer_style,
    ))

    doc.build(elements)
    return pdf_path


# ── action handlers ───────────────────────────────────────────────────────────

async def action_start(message: Message) -> None:
    user_id = message.from_user.id
    init_workout(user_id)
    await message.answer(
        "🏋️ <b>Тренировка начата!</b>\n\n"
        "Отправляй голосовые сообщения с упражнениями.\n\n"
        "<b>Примеры:</b>\n"
        "  🎙 «жим лежа 60кг 10 раз»\n"
        "  🎙 «жим лежа 30 раз» — подтянет вес автоматически\n"
        "  🎙 «присед 80кг 5, пресс 20, отжимания 15»\n\n"
        "<b>Голосом:</b>\n"
        "  • <i>финиш / конец / стоп</i> → завершить\n"
        "  • <i>отменить / убрать</i> → удалить последний подход",
        parse_mode="HTML",
        reply_markup=kb_workout(),
    )


async def action_finish(message: Message) -> None:
    user_id = message.from_user.id

    if user_id not in workouts or not workouts[user_id]["data"]:
        await message.answer("❌ Нет активной тренировки.")
        return

    await message.answer("📊 Генерирую PDF...")

    try:
        pdf_path = build_pdf(user_id)
        total_sets = sum(len(s) for s in workouts[user_id]["data"].values())
        duration = datetime.now() - workouts[user_id]["started_at"]
        minutes = int(duration.total_seconds() // 60)

        await message.answer_document(
            FSInputFile(pdf_path),
            caption=(
                f"✅ <b>Тренировка завершена!</b>\n\n"
                f"⏱ Длительность: <b>{minutes} мин</b>\n"
                f"🏋️ Упражнений: <b>{len(workouts[user_id]['data'])}</b>\n"
                f"🔁 Всего подходов: <b>{total_sets}</b>\n\n"
                f"Отличная работа! 💪"
            ),
            parse_mode="HTML",
        )
        os.remove(pdf_path)
    except Exception as e:
        await message.answer(f"❌ Ошибка генерации PDF: {e}")
    finally:
        workouts.pop(user_id, None)

    await message.answer(
        "Начать новую тренировку?",
        reply_markup=kb_main(),
    )


async def action_undo(message: Message) -> None:
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

    weight_str = f"{entry['weight']}кг × " if entry["weight"] is not None else ""
    await message.answer(
        f"↩️ <b>Удалён последний подход:</b>\n"
        f"🏋️ {exercise.title()} — {weight_str}{entry['reps']} раз",
        parse_mode="HTML",
        reply_markup=kb_workout(),
    )


# ── telegram handlers ─────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
        "Я помогу тебе вести тренировки голосом.\n\n"
        "<b>Как это работает:</b>\n"
        "  1️⃣ Нажми «Начать тренировку»\n"
        "  2️⃣ Говори упражнения голосом\n"
        "  3️⃣ Получи красивый PDF в конце\n\n"
        "<b>Примеры фраз:</b>\n"
        "  🎙 «жим лежа 60кг 10 раз»\n"
        "  🎙 «присед 80кг 5, пресс 20, отжимания 15»\n"
        "  🎙 «финиш» — завершить тренировку",
        parse_mode="HTML",
        reply_markup=kb_main(),
    )


@dp.message(F.text == "/finish")
async def cmd_finish(message: Message):
    await action_finish(message)


@dp.message(F.text == "/undo")
async def cmd_undo(message: Message):
    await action_undo(message)


# ── callback handlers ─────────────────────────────────────────────────────────

@dp.callback_query(F.data == "start_workout")
async def cb_start(callback: CallbackQuery):
    await callback.answer()
    await action_start(callback.message)


@dp.callback_query(F.data == "finish_workout")
async def cb_finish(callback: CallbackQuery):
    await callback.answer()
    await action_finish(callback.message)


@dp.callback_query(F.data == "undo_last")
async def cb_undo(callback: CallbackQuery):
    await callback.answer()
    # undo работает по user_id — берём из callback
    callback.message.from_user = callback.from_user
    await action_undo(callback.message)


@dp.callback_query(F.data == "support")
async def cb_support(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "⭐ <b>Спасибо за поддержку!</b>\n\n"
        "Если бот тебе полезен — поделись с друзьями 🙌",
        parse_mode="HTML",
    )


# ── voice handler ─────────────────────────────────────────────────────────────

@dp.message(F.voice)
async def voice_handler(message: Message):
    file_id = message.voice.file_id
    file = await bot.get_file(file_id)

    os.makedirs("voices", exist_ok=True)
    save_path = f"voices/{file_id}.ogg"
    await bot.download_file(file.file_path, save_path)

    status_msg = await message.answer("🎤 Распознаю речь...")

    with open(save_path, "rb") as f:
        transcription = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=f,
        )
    text = transcription.text
    os.remove(save_path)

    await status_msg.delete()

    # ── intent без GPT ────────────────────────────────────────────────────────
    intent = detect_intent(text)

    if intent == "start":
        await action_start(message)
        return
    if intent == "finish":
        await action_finish(message)
        return
    if intent == "undo":
        await action_undo(message)
        return

    # ── тренировка должна быть начата ─────────────────────────────────────────
    user_id = message.from_user.id

    if user_id not in workouts:
        await message.answer(
            "⚠️ Тренировка не начата.\n"
            "Скажи «начать» или нажми кнопку ниже.",
            reply_markup=kb_main(),
        )
        return

    # ── GPT парсинг упражнений ────────────────────────────────────────────────
    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": text},
        ]
    )

    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    entries = json.loads(raw)

    user_workout = workouts[user_id]
    reply_lines = [f"🗣 <i>«{text}»</i>\n"]

    for parsed in entries:
        exercise = parsed.get("exercise")
        reps = parsed.get("reps")
        weight = parsed.get("weight")

        if exercise:
            exercise = exercise.lower().strip()

        if not exercise or exercise == "null":
            exercise = user_workout["current_exercise"]

        if not exercise:
            await message.answer(
                "❌ Сначала назови упражнение.\n"
                "Например: «жим лежа 60кг 10 раз»"
            )
            return

        set_number, used_weight = apply_entry(user_workout, exercise, reps, weight)

        weight_str = f"{used_weight}кг × " if used_weight is not None else ""
        reply_lines.append(
            f"✅ <b>{exercise.title()}</b>\n"
            f"   Сет {set_number}  ·  {weight_str}{reps} раз"
        )

    reply_lines.append("\n<i>Скажи следующий подход или нажми кнопку ниже.</i>")

    await message.answer(
        "\n".join(reply_lines),
        parse_mode="HTML",
        reply_markup=kb_workout(),
    )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())