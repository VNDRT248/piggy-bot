import asyncio
import csv
import io
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TZ = ZoneInfo(os.getenv("TZ", "Europe/Moscow"))
DB_PATH = "piggy.db"

dp = Dispatcher()


class MoneyInput(StatesGroup):
    deposit = State()
    withdraw = State()


# ---------- Утилиты ----------
def parse_money(text: str) -> int | None:
    try:
        d = Decimal(text.replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        return None
    if d <= 0:
        return None
    return int((d * 100).to_integral_value())


def fmt(cents: int) -> str:
    return f"{cents / 100:,.2f}".replace(",", " ")


def progress_bar(bal: int, target: int | None, width: int = 10) -> str:
    if not target:
        return ""
    pct = min(100, round(bal * 100 / target))
    filled = max(0, min(width, round(pct * width / 100)))
    return "▓" * filled + "░" * (width - filled) + f" {pct}%"


def local_dt(iso_utc: str, fmt_str: str = "%d.%m %H:%M") -> str:
    try:
        return (
            datetime.fromisoformat(iso_utc)
            .replace(tzinfo=ZoneInfo("UTC"))
            .astimezone(TZ)
            .strftime(fmt_str)
        )
    except Exception:
        return iso_utc[:16]


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            target_cents INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tx (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount_cents INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('in','out')),
            created_at TEXT NOT NULL,
            FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE CASCADE
        );
        """)
        await db.commit()


async def goal_balance(db, goal_id: int) -> int:
    cur = await db.execute(
        "SELECT COALESCE(SUM(CASE WHEN kind='in' THEN amount_cents ELSE -amount_cents END),0) "
        "FROM tx WHERE goal_id=?",
        (goal_id,),
    )
    row = await cur.fetchone()
    return row[0]


async def total_balance(db, user_id: int) -> int:
    cur = await db.execute(
        "SELECT COALESCE(SUM(CASE WHEN kind='in' THEN amount_cents ELSE -amount_cents END),0) "
        "FROM tx WHERE user_id=?",
        (user_id,),
    )
    row = await cur.fetchone()
    return row[0]


# ---------- Клавиатуры ----------
def kb_main():
    b = InlineKeyboardBuilder()
    b.button(text="💰 Мои копилки", callback_data="list")
    b.button(text="➕ Новая копилка", callback_data="new_help")
    b.adjust(1)
    return b.as_markup()


def kb_goals(pairs):
    b = InlineKeyboardBuilder()
    for g, bal in pairs:
        if g["target_cents"]:
            pct = min(100, round(bal * 100 / g["target_cents"]))
            label = f"🆔{g['id']} {g['title']} — {fmt(bal)} ({pct}%)"
        else:
            label = f"🆔{g['id']} {g['title']} — {fmt(bal)}"
        b.button(text=label, callback_data=f"g:{g['id']}")
    b.adjust(1)
    return b.as_markup()


def kb_bank(gid):
    b = InlineKeyboardBuilder()
    b.button(text="➕ Пополнить", callback_data=f"dep:{gid}")
    b.button(text="➖ Снять", callback_data=f"wd:{gid}")
    b.button(text="📜 История", callback_data=f"hist:{gid}")
    b.button(text="🗑 Удалить", callback_data=f"del:{gid}")
    b.button(text="◀️ К списку", callback_data="list")
    b.adjust(2, 2, 1)
    return b.as_markup()


def kb_back(gid):
    b = InlineKeyboardBuilder()
    b.button(text="◀️ К копилке", callback_data=f"g:{gid}")
    b.adjust(1)
    return b.as_markup()


def kb_confirm_del(gid):
    b = InlineKeyboardBuilder()
    b.button(text="✅ Удалить", callback_data=f"delconf:{gid}")
    b.button(text="❌ Отмена", callback_data=f"g:{gid}")
    b.adjust(2)
    return b.as_markup()


# ---------- Рендер ----------
def bank_text(goal, bal: int) -> str:
    text = f"💰 <b>{goal['title']}</b> (🆔 {goal['id']})\n\n"
    text += f"Накоплено: <b>{fmt(bal)}</b>"
    if goal["target_cents"]:
        text += f" / {fmt(goal['target_cents'])}\n\n{progress_bar(bal, goal['target_cents'])}"
    return text


async def reply(target: Message, text: str, markup=None, edit: bool = False):
    if edit:
        try:
            await target.edit_text(text, reply_markup=markup)
            return
        except Exception:
            pass
    await target.answer(text, reply_markup=markup)


async def show_list(target: Message, user_id: int, edit: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM goals WHERE user_id=? ORDER BY id", (user_id,))
        goals = await cur.fetchall()
        if not goals:
            await reply(
                target,
                "У тебя пока нет копилок.\nСоздай командой: /new Название [цель]\n"
                "Пример: /new Отпуск 50000",
                kb_main(),
                edit,
            )
            return
        pairs = []
        for g in goals:
            bal = await goal_balance(db, g["id"])
            pairs.append((g, bal))
        total = await total_balance(db, user_id)

    text = "💰 Твои копилки — выбери для действий:\n"
    text += f"\nВсего накоплено: <b>{fmt(total)}</b>"
    await reply(target, text, kb_goals(pairs), edit)


async def show_bank(target: Message, user_id: int, gid: int, edit: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM goals WHERE id=? AND user_id=?", (gid, user_id)
        )
        goal = await cur.fetchone()
        if not goal:
            await reply(target, "Копилка не найдена.", kb_main(), edit)
            return
        bal = await goal_balance(db, gid)
    await reply(target, bank_text(goal, bal), kb_bank(gid), edit)


# ---------- Команды ----------
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "💰 Привет! Я бот-копилка.\n\n"
        "Жми кнопки ниже или используй команды:\n"
        "/new <название> [цель] — создать копилку\n"
        "/list — список копилок\n"
        "/export — выгрузить историю в CSV\n"
        "/help — помощь",
        reply_markup=kb_main(),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    await cmd_start(message, state)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    data = await state.get_data()
    gid = data.get("goal_id")
    await state.clear()
    if gid:
        await show_bank(message, message.from_user.id, gid)
    else:
        await message.answer("Отменено.", reply_markup=kb_main())


@dp.message(Command("new"))
async def cmd_new(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Использование: /new <название> [цель]\nПример: /new Отпуск 50000")
        return

    parts = command.args.split(maxsplit=1)
    title = parts[0][:80]
    target_cents = None
    if len(parts) == 2:
        target_cents = parse_money(parts[1])
        if target_cents is None:
            await message.answer("Цель должна быть положительным числом.")
            return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO goals(user_id, title, target_cents, created_at) VALUES(?,?,?,?)",
            (message.from_user.id, title, target_cents, datetime.utcnow().isoformat()),
        )
        await db.commit()
        goal_id = cur.lastrowid

    target_txt = f" (цель: {fmt(target_cents)})" if target_cents else ""
    await message.answer(f"✅ Копилка создана: 🆔 {goal_id} — {title}{target_txt}")
    await show_bank(message, message.from_user.id, goal_id)


@dp.message(Command("list"))
async def cmd_list(message: Message, state: FSMContext):
    await state.clear()
    await show_list(message, message.from_user.id)


@dp.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject):
    """Резервная команда — работает как раньше."""
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].isdigit():
        await message.answer("Использование: /add <id> <сумма>\nИли жми /list и кнопки.")
        return
    gid, cents = int(args[0]), parse_money(args[1])
    if cents is None:
        await message.answer("Сумма должна быть положительным числом.")
        return
    await _apply_tx(message, message.from_user.id, gid, cents, "in")


@dp.message(Command("take"))
async def cmd_take(message: Message, command: CommandObject):
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].isdigit():
        await message.answer("Использование: /take <id> <сумма>\nИли жми /list и кнопки.")
        return
    gid, cents = int(args[0]), parse_money(args[1])
    if cents is None:
        await message.answer("Сумма должна быть положительным числом.")
        return
    await _apply_tx(message, message.from_user.id, gid, cents, "out")


async def _apply_tx(message: Message, user_id: int, gid: int, cents: int, kind: str):
    """Общая логика пополнения/снятия. Возвращает None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM goals WHERE id=? AND user_id=?", (gid, user_id)
        )
        goal = await cur.fetchone()
        if not goal:
            await message.answer("Копилка не найдена.", reply_markup=kb_main())
            return
        old_bal = await goal_balance(db, gid)
        if kind == "out" and cents > old_bal:
            await message.answer(
                f"Недостаточно средств. В «{goal['title']}» сейчас {fmt(old_bal)}.",
                reply_markup=kb_bank(gid),
            )
            return
        await db.execute(
            "INSERT INTO tx(goal_id, user_id, amount_cents, kind, created_at) "
            "VALUES(?,?,?,?,?)",
            (gid, user_id, cents, kind, datetime.utcnow().isoformat()),
        )
        await db.commit()
        new_bal = await goal_balance(db, gid)

    sign = "➕" if kind == "in" else "➖"
    await message.answer(
        f"{sign} {'Добавлено' if kind == 'in' else 'Снято'} {fmt(cents)}\n\n"
        + bank_text(goal, new_bal),
        reply_markup=kb_bank(gid),
    )
    if kind == "in" and goal["target_cents"] and old_bal < goal["target_cents"] <= new_bal:
        await message.answer(
            f"🎉🎉🎉 Поздравляю! Цель «{goal['title']}» достигнута!\n"
            f"Накоплено {fmt(new_bal)} из {fmt(goal['target_cents'])}. Можно тратить 😎"
        )


@dp.message(Command("history"))
async def cmd_history(message: Message, command: CommandObject):
    uid = message.from_user.id
    gid = None
    if command.args:
        if command.args.strip().isdigit():
            gid = int(command.args.strip())
        else:
            await message.answer("Использование: /history [id]")
            return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if gid is not None:
            sql = (
                "SELECT t.amount_cents, t.kind, t.created_at, g.title "
                "FROM tx t JOIN goals g ON g.id=t.goal_id "
                "WHERE t.user_id=? AND t.goal_id=? ORDER BY t.id DESC LIMIT 15"
            )
            cur = await db.execute(sql, (uid, gid))
        else:
            sql = (
                "SELECT t.amount_cents, t.kind, t.created_at, g.title "
                "FROM tx t JOIN goals g ON g.id=t.goal_id "
                "WHERE t.user_id=? ORDER BY t.id DESC LIMIT 15"
            )
            cur = await db.execute(sql, (uid,))
        rows = await cur.fetchall()

    if not rows:
        await message.answer("Операций пока нет.")
        return

    lines = ["📜 Последние операции:"]
    for r in rows:
        sign = "➕" if r["kind"] == "in" else "➖"
        lines.append(
            f"{sign} {fmt(r['amount_cents'])} — «{r['title']}» — {local_dt(r['created_at'])}"
        )
    await message.answer("\n".join(lines))


@dp.message(Command("export"))
async def cmd_export(message: Message, command: CommandObject):
    uid = message.from_user.id
    gid = None
    if command.args:
        if command.args.strip().isdigit():
            gid = int(command.args.strip())
        else:
            await message.answer("Использование: /export [id]")
            return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = (
            "SELECT g.title, t.kind, t.amount_cents, t.created_at "
            "FROM tx t JOIN goals g ON g.id=t.goal_id WHERE t.user_id=?"
        )
        params: list = [uid]
        if gid is not None:
            sql += " AND t.goal_id=?"
            params.append(gid)
        sql += " ORDER BY t.id"
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()

    if not rows:
        await message.answer("Нечего выгружать — операций нет.")
        return

    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Копилка", "Тип", "Сумма", "Дата и время"])
    for r in rows:
        writer.writerow([
            r["title"],
            "Пополнение" if r["kind"] == "in" else "Снятие",
            f"{r['amount_cents'] / 100:.2f}".replace(".", ","),
            local_dt(r["created_at"], "%Y-%m-%d %H:%M"),
        ])

    filename = f"piggy_{datetime.now(TZ).strftime('%Y%m%d_%H%M')}.csv"
    file = BufferedInputFile(buf.getvalue().encode("utf-8"), filename=filename)
    await message.answer_document(file, caption=f"📤 История операций ({len(rows)} шт.)")


@dp.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Использование: /del <id>\nИли жми кнопки в /list.")
        return
    gid = int(arg)
    await _delete_goal(message, message.from_user.id, gid)


async def _delete_goal(message: Message, user_id: int, gid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, title FROM goals WHERE id=? AND user_id=?", (gid, user_id)
        )
        goal = await cur.fetchone()
        if not goal:
            await message.answer("Копилка не найдена.")
            return
        await db.execute("DELETE FROM tx WHERE goal_id=?", (gid,))
        await db.execute("DELETE FROM goals WHERE id=?", (gid,))
        await db.commit()
    await message.answer(f"🗑 Копилка «{goal['title']}» удалена.", reply_markup=kb_main())


# ---------- Инлайн-кнопки: callbacks ----------
@dp.callback_query(F.data == "list")
async def cb_list(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_list(cq.message, cq.from_user.id, edit=True)
    await cq.answer()


@dp.callback_query(F.data == "new_help")
async def cb_new_help(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.message.edit_text(
        "Создать копилку:\n"
        "/new <название> [цель]\n\n"
        "Примеры:\n"
        "/new Отпуск 50000\n"
        "/new Подушка безопасности",
        reply_markup=kb_main(),
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("g:"))
async def cb_bank(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    gid = int(cq.data.split(":")[1])
    await show_bank(cq.message, cq.from_user.id, gid, edit=True)
    await cq.answer()


@dp.callback_query(F.data.startswith("dep:"))
async def cb_dep(cq: CallbackQuery, state: FSMContext):
    gid = int(cq.data.split(":")[1])
    await state.set_state(MoneyInput.deposit)
    await state.update_data(goal_id=gid)
    await cq.message.edit_text(
        "➕ <b>Сколько положить?</b>\n\n"
        "Отправь число: например <code>1500</code> или <code>1500,50</code>\n\n"
        "/cancel — отменить",
        reply_markup=kb_back(gid),
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("wd:"))
async def cb_wd(cq: CallbackQuery, state: FSMContext):
    gid = int(cq.data.split(":")[1])
    await state.set_state(MoneyInput.withdraw)
    await state.update_data(goal_id=gid)
    await cq.message.edit_text(
        "➖ <b>Сколько снять?</b>\n\n"
        "Отправь число: например <code>500</code> или <code>500,50</code>\n\n"
        "/cancel — отменить",
        reply_markup=kb_back(gid),
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("hist:"))
async def cb_hist(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    gid = int(cq.data.split(":")[1])
    uid = cq.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT t.amount_cents, t.kind, t.created_at "
            "FROM tx t WHERE t.user_id=? AND t.goal_id=? ORDER BY t.id DESC LIMIT 15",
            (uid, gid),
        )
        rows = await cur.fetchall()

    if not rows:
        text = "📜 Операций пока нет."
    else:
        lines = ["📜 Последние операции:"]
        for r in rows:
            sign = "➕" if r["kind"] == "in" else "➖"
            lines.append(f"{sign} {fmt(r['amount_cents'])} — {local_dt(r['created_at'])}")
        text = "\n".join(lines)

    await cq.message.edit_text(text, reply_markup=kb_back(gid))
    await cq.answer()


@dp.callback_query(F.data.startswith("del:"))
async def cb_del(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    gid = int(cq.data.split(":")[1])
    await cq.message.edit_text(
        f"🗑 Удалить копилку 🆔{gid}?\nВсе операции тоже удалятся безвозвратно.",
        reply_markup=kb_confirm_del(gid),
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("delconf:"))
async def cb_delconf(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    gid = int(cq.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tx WHERE goal_id=?", (gid,))
        await db.execute("DELETE FROM goals WHERE id=? AND user_id=?", (gid, cq.from_user.id))
        await db.commit()
    await cq.message.edit_text("🗑 Копилка удалена.", reply_markup=kb_main())
    await cq.answer("Удалено")


# ---------- Ввод суммы из FSM ----------
@dp.message(MoneyInput.deposit, ~F.text.startswith("/"))
async def inp_deposit(message: Message, state: FSMContext):
    cents = parse_money(message.text)
    if cents is None:
        await message.answer("Введи положительное число, например 1500:")
        return
    data = await state.get_data()
    gid = data.get("goal_id")
    await state.clear()
    await _apply_tx(message, message.from_user.id, gid, cents, "in")


@dp.message(MoneyInput.withdraw, ~F.text.startswith("/"))
async def inp_withdraw(message: Message, state: FSMContext):
    cents = parse_money(message.text)
    if cents is None:
        await message.answer("Введи положительное число, например 500:")
        return
    data = await state.get_data()
    gid = data.get("goal_id")
    await state.clear()
    await _apply_tx(message, message.from_user.id, gid, cents, "out")


# ---------- Запуск ----------
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в .env")
    await init_db()
    bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())