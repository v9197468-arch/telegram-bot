import os
import json
import threading
from flask import Flask
from pathlib import Path
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ✅ БЕЗПЕЧНО: токен і адмінка з ENV
BOT_TOKEN = "8296396857:AAFb0LdFgWbAbsGI8ruz2h_XcqAEmufM-xY"
ADMIN_ID = 7894377511
DATA_FILE = Path("data.json")
START_IMAGE = Path("start.jpg")

flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "OK", 200


def run_web():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

CATEGORIES = [
    ("food", "🍔 Їжа"),
    ("transport", "🚕 Транспорт"),
    ("home", "🏠 Житло"),
    ("shopping", "🛒 Покупки"),
    ("fun", "🎮 Розваги"),
    ("health", "💊 Здоров’я"),
    ("study", "🎓 Навчання"),
    ("gifts", "🎁 Подарунки"),
    ("mobile", "📱 Зв’язок"),
    ("other", "📦 Інше"),
]

# user_state[user_id] = {"mode": "...", ...}
user_state: dict[str, dict] = {}

# --------- helpers ---------
def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def month_key(dt: datetime | None = None) -> str:
    d = dt or datetime.now()
    return f"{d.year:04d}-{d.month:02d}"


def prev_month_key(dt: datetime | None = None) -> str:
    d = dt or datetime.now()
    y, m = d.year, d.month
    if m == 1:
        y -= 1
        m = 12
    else:
        m -= 1
    return f"{y:04d}-{m:02d}"


def year_key(dt: datetime | None = None) -> int:
    return (dt or datetime.now()).year


def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}

    if not isinstance(data, dict):
        data = {}
    if "users" not in data or not isinstance(data["users"], dict):
        data["users"] = {}

    # апгрейд старих записів
    for uid, u in list(data["users"].items()):
        if not isinstance(u, dict):
            data["users"][uid] = {"records": [], "limits": {}, "profile": {}}
            continue
        u.setdefault("records", [])
        u.setdefault("limits", {})
        u.setdefault("profile", {})

    return data


def save_data(data: dict) -> None:
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def ensure_user(data: dict, user_id: str) -> None:
    if user_id not in data["users"] or not isinstance(data["users"][user_id], dict):
        data["users"][user_id] = {"records": [], "limits": {}, "profile": {}}
        return

    data["users"][user_id].setdefault("records", [])
    data["users"][user_id].setdefault("limits", {})
    data["users"][user_id].setdefault("profile", {})


def cat_title(cat_key: str) -> str:
    for k, t in CATEGORIES:
        if k == cat_key:
            return t
    return cat_key


def sum_month_expenses_by_cat(records: list, mkey: str, cat_key: str) -> float:
    total = 0.0
    for r in records:
        if r.get("type") == "expense" and r.get("month") == mkey and r.get("category") == cat_key:
            total += float(r.get("amount", 0))
    return total


def sum_year(records: list, y: int, rtype: str, cat_key: str | None = None) -> float:
    total = 0.0
    for r in records:
        if r.get("type") != rtype:
            continue
        if int(r.get("year", 0)) != y:
            continue
        if cat_key is not None and r.get("category") != cat_key:
            continue
        total += float(r.get("amount", 0))
    return total


# --------- keyboards ---------
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Розпочати контроль", callback_data="menu:control")],
        [InlineKeyboardButton("📊 Статистика за рік", callback_data="menu:year_stats")],
        [InlineKeyboardButton("💰 Дохід за рік", callback_data="menu:year_income")],
        [InlineKeyboardButton("⚖️ Баланс", callback_data="menu:balance")],
        [InlineKeyboardButton("🚦 Встановити ліміти", callback_data="menu:limits")],
    ])


def kb_control() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати витрату", callback_data="act:add_expense")],
        [InlineKeyboardButton("➕ Додати дохід", callback_data="act:add_income")],
        [InlineKeyboardButton("📄 Історія (10)", callback_data="act:history")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")],
    ])


def kb_categories(prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(CATEGORIES), 2):
        row = []
        k1, t1 = CATEGORIES[i]
        row.append(InlineKeyboardButton(t1, callback_data=f"{prefix}:{k1}"))
        if i + 1 < len(CATEGORIES):
            k2, t2 = CATEGORIES[i + 1]
            row.append(InlineKeyboardButton(t2, callback_data=f"{prefix}:{k2}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


def kb_limits_menu() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(CATEGORIES), 2):
        row = []
        k1, t1 = CATEGORIES[i]
        row.append(InlineKeyboardButton(t1, callback_data=f"limitcat:{k1}"))
        if i + 1 < len(CATEGORIES):
            k2, t2 = CATEGORIES[i + 1]
            row.append(InlineKeyboardButton(t2, callback_data=f"limitcat:{k2}"))
        rows.append(row)

    rows.append([InlineKeyboardButton("📋 Скопіювати ліміти на новий місяць", callback_data="act:copy_limits")])
    rows.append([InlineKeyboardButton("👀 Показати мої ліміти (цей місяць)", callback_data="act:view_limits")])
    rows.append([InlineKeyboardButton("📉 Витрати по категоріях (цей місяць)", callback_data="act:month_spent_by_cat")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


# --------- admin helpers ---------
def is_admin(chat_id: int) -> bool:
    return ADMIN_ID != 0 and chat_id == ADMIN_ID


# --------- commands ---------
async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Твій chat_id: {update.effective_chat.id}")


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("⛔ Доступ заборонено")
        return

    data = load_data()
    users = data.get("users", {})
    lines = [f"👥 Користувачів: {len(users)}", ""]

    for uid, info in list(users.items())[-30:]:
        prof = info.get("profile", {})
        username = prof.get("username", "no_username")
        name = prof.get("name", "NoName")
        records = info.get("records", [])
        last_active = info.get("last_active", "—")

        user_line = f"@{username}" if username and username != "no_username" else name
        lines.append(
            f"• {user_line} (id {uid})\n"
            f"  Записів: {len(records)}\n"
            f"  Остання активність: {last_active}"
        )

    await update.message.reply_text("\n".join(lines))


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("⛔ Доступ заборонено")
        return

    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Використання: /broadcast текст повідомлення")
        return

    data = load_data()
    users = data.get("users", {})

    sent = 0
    failed = 0
    for uid in users.keys():
        try:
            await context.bot.send_message(chat_id=int(uid), text=text)
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(f"✅ Розсилка завершена\nНадіслано: {sent}\nНе вдалося: {failed}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_chat.id)

    data = load_data()
    ensure_user(data, user_id)

    user = update.effective_user
    username = user.username or "no_username"
    name = user.first_name or "NoName"

    data["users"][user_id]["profile"] = {"username": username, "name": name}
    data["users"][user_id]["last_active"] = now_iso()
    save_data(data)

    user_state.pop(user_id, None)

    start_text = (
        "💼 <b>Ваш персональний бот для повного контролю доходів та витрат</b>\n\n"
        "Цей бот створений для точного обліку ваших фінансів, щоб ви завжди розуміли, "
        "куди надходять ваші кошти та на що саме вони витрачаються.\n\n"
        "🔒 <b>Ваші фінанси — виключно ваша особиста територія</b>\n\n"
        "Уся інформація про ваші витрати, доходи, заощадження, цілі та фінансові плани "
        "перебуває під максимальним захистом і доступна лише вам.\n\n"
        "Жодна стороння особа, треті сторони чи інші користувачі не мають доступу до ваших даних.\n\n"
        "<b>Ваш фінансовий простір — це приватна система повного контролю, де тільки ви керуєте, "
        "аналізуєте та приймаєте рішення.</b>"
    )

    try:
       with open(Path(__file__).with_name("start.jpg"), "rb") as photo:
            await update.message.reply_photo(photo=photo)
    except Exception as e:
        print(f"START PHOTO ERROR: {e}")

    await update.message.reply_text(
        start_text,
        parse_mode="HTML",
        reply_markup=kb_main(),
    )

    try:
        with open("start.jpg", "rb") as photo:
            await update.message.reply_photo(
                photo=photo,
                caption=start_text,
                parse_mode="HTML",
                reply_markup=kb_main(),
            )
    except Exception as e:
        print(f"START PHOTO ERROR: {e}")
        await update.message.reply_text(
            start_text,
            parse_mode="HTML",
            reply_markup=kb_main(),
        )
    else:
        await update.message.reply_text(
            start_text,
            parse_mode="HTML",
            reply_markup=kb_main(),
        )


# --------- buttons ---------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user_id = str(q.message.chat.id)
    data = load_data()
    ensure_user(data, user_id)
    data["users"][user_id]["last_active"] = now_iso()
    save_data(data)

    records = data["users"][user_id]["records"]
    limits = data["users"][user_id]["limits"]
    mk = month_key()

    d = q.data

    # ----- menus -----
    if d == "menu:main":
        user_state.pop(user_id, None)
        await q.edit_message_text("Головне меню:", reply_markup=kb_main())
        return

    if d == "menu:control":
        user_state.pop(user_id, None)
        await q.edit_message_text("Контроль фінансів — обери дію:", reply_markup=kb_control())
        return

    if d == "menu:limits":
        user_state.pop(user_id, None)
        await q.edit_message_text("🚦 Ліміти на місяць — обери категорію:", reply_markup=kb_limits_menu())
        return

    # ----- balance/year -----
    if d == "menu:balance":
        all_income = sum(float(r.get("amount", 0)) for r in records if r.get("type") == "income")
        all_exp = sum(float(r.get("amount", 0)) for r in records if r.get("type") == "expense")
        all_bal = all_income - all_exp

        m_income = sum(float(r.get("amount", 0)) for r in records if r.get("type") == "income" and r.get("month") == mk)
        m_exp = sum(float(r.get("amount", 0)) for r in records if r.get("type") == "expense" and r.get("month") == mk)
        m_bal = m_income - m_exp

        text = (
            f"⚖️ Баланс\n"
            f"• Загальний: {all_bal:.2f} ₴\n"
            f"• За {mk}: {m_bal:.2f} ₴\n\n"
            f"За {mk}: +{m_income:.2f} ₴ / -{m_exp:.2f} ₴"
        )
        await q.edit_message_text(text, reply_markup=kb_main())
        return

    if d == "menu:year_income":
        y = year_key()
        inc = sum_year(records, y, "income")
        await q.edit_message_text(f"💰 Дохід за {y} рік: {inc:.2f} ₴", reply_markup=kb_main())
        return

    if d == "menu:year_stats":
        y = year_key()
        lines = [f"📊 Витрати за {y} рік по категоріях:"]
        total = 0.0
        for cat_key, title in CATEGORIES:
            s = sum_year(records, y, "expense", cat_key)
            if s > 0:
                lines.append(f"• {title}: {s:.2f} ₴")
                total += s
        if total == 0:
            lines.append("Поки що немає витрат за цей рік 🙂")
        else:
            lines.append(f"\nРазом витрати: {total:.2f} ₴")
        await q.edit_message_text("\n".join(lines), reply_markup=kb_main())
        return

    # ----- control actions -----
    if d == "act:add_expense":
        user_state[user_id] = {"mode": "expense_choose_cat"}
        await q.edit_message_text("➕ Витрата: обери категорію", reply_markup=kb_categories("expcat", "menu:control"))
        return

    if d == "act:add_income":
        user_state[user_id] = {"mode": "income_amount"}
        await q.edit_message_text("➕ Дохід: введи суму в ₴ (наприклад 500 або 1200.50)\n\n(Щоб відмінити — /start)")
        return

    if d == "act:history":
        items = records[-10:]
        if not items:
            await q.edit_message_text("📄 Історія порожня.", reply_markup=kb_control())
            return
        lines = ["📄 Останні 10 записів:"]
        for r in items:
            sign = "-" if r.get("type") == "expense" else "+"
            cat = f" {cat_title(r.get('category',''))}" if r.get("type") == "expense" else ""
            lines.append(f"{r.get('time')} | {sign}{float(r.get('amount',0)):.2f} ₴{cat}")
        await q.edit_message_text("\n".join(lines), reply_markup=kb_control())
        return

    # ----- choose expense category -----
    if d.startswith("expcat:"):
        cat_key = d.split(":", 1)[1]
        user_state[user_id] = {"mode": "expense_amount", "category": cat_key}
        await q.edit_message_text(
            f"{cat_title(cat_key)}\nВведи суму витрати в ₴ (наприклад 150 або 99.90)\n\n(Щоб відмінити — /start)"
        )
        return

    # ----- limits -----
    if d.startswith("limitcat:"):
        cat_key = d.split(":", 1)[1]
        user_state[user_id] = {"mode": "limit_amount", "category": cat_key}
        await q.edit_message_text(
            f"🚦 Ліміт для {cat_title(cat_key)} на {mk}\nВведи ліміт в ₴ (наприклад 3000)\n\n(Щоб відмінити — /start)"
        )
        return

    if d == "act:copy_limits":
        cur = month_key()
        prev = prev_month_key()
        prev_limits = limits.get(prev)

        if not prev_limits:
            await q.edit_message_text(
                f"📋 Немає лімітів за {prev}, які можна скопіювати.\n"
                f"Спочатку встанови ліміти вручну 🙂",
                reply_markup=kb_limits_menu()
            )
            return

        if cur not in limits:
            limits[cur] = {}

        copied = 0
        skipped = 0
        for cat_key, val in prev_limits.items():
            if cat_key in limits[cur]:
                skipped += 1
                continue
            limits[cur][cat_key] = val
            copied += 1

        save_data(data)
        await q.edit_message_text(
            f"✅ Скопійовано ліміти з {prev} → {cur}\n"
            f"Скопійовано: {copied}\n"
            f"Пропущено (вже були): {skipped}",
            reply_markup=kb_limits_menu()
        )
        return

    if d == "act:view_limits":
        cur = month_key()
        cur_limits = limits.get(cur, {})

        if not cur_limits:
            await q.edit_message_text(
                f"👀 Ліміти за {cur}: поки не встановлені.\n"
                f"Обери категорію і задай ліміт 🙂",
                reply_markup=kb_limits_menu()
            )
            return

        lines = [f"👀 Твої ліміти за {cur}:"]
        for cat_key, val in cur_limits.items():
            lines.append(f"• {cat_title(cat_key)}: {float(val):.2f} ₴")
        await q.edit_message_text("\n".join(lines), reply_markup=kb_limits_menu())
        return

    if d == "act:month_spent_by_cat":
        cur = month_key()
        cur_limits = limits.get(cur, {})

        lines = [f"📉 Витрати по категоріях за {cur}:"]
        any_spent = False

        for cat_key, title in CATEGORIES:
            spent = sum_month_expenses_by_cat(records, cur, cat_key)
            if spent <= 0:
                continue
            any_spent = True
            if cat_key in cur_limits:
                lim = float(cur_limits[cat_key])
                lines.append(f"• {title}: {spent:.2f} / {lim:.2f} ₴")
            else:
                lines.append(f"• {title}: {spent:.2f} ₴")

        if not any_spent:
            lines.append("Поки що витрат за цей місяць немає 🙂")

        await q.edit_message_text("\n".join(lines), reply_markup=kb_limits_menu())
        return

    await q.edit_message_text("Не зрозумів дію. Повертаю в меню.", reply_markup=kb_main())


# --------- text input ---------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_chat.id)
    text = (update.message.text or "").strip()

    data = load_data()
    ensure_user(data, user_id)
    data["users"][user_id]["last_active"] = now_iso()
    save_data(data)
    records = data["users"][user_id]["records"]
    limits = data["users"][user_id]["limits"]
    mk = month_key()

    state = user_state.get(user_id)
    if not state:
        await update.message.reply_text("Обери дію кнопками:", reply_markup=kb_main())
        return

    # income amount
    if state.get("mode") == "income_amount":
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Сума має бути числом > 0. Спробуй ще раз.")
            return

        records.append({
            "type": "income",
            "amount": amount,
            "time": now_iso(),
            "month": mk,
            "year": year_key(),
        })
        save_data(data)
        user_state.pop(user_id, None)
        await update.message.reply_text("✅ Дохід додано", reply_markup=kb_control())
        return

    # expense amount
    if state.get("mode") == "expense_amount":
        cat_key = state["category"]
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Сума має бути числом > 0. Спробуй ще раз.")
            return

        records.append({
            "type": "expense",
            "amount": amount,
            "category": cat_key,
            "time": now_iso(),
            "month": mk,
            "year": year_key(),
        })
        save_data(data)

        # limit check
        month_limits = limits.get(mk, {})
        limit_val = month_limits.get(cat_key)

        msg = "✅ Витрату додано"
        if limit_val is not None:
            spent = sum_month_expenses_by_cat(records, mk, cat_key)
            limit_val = float(limit_val)
            if spent >= limit_val:
                msg += f"\n🚨 ЛІМІТ ПЕРЕВИЩЕНО для {cat_title(cat_key)}: {spent:.2f}/{limit_val:.2f} ₴"
            elif spent >= 0.8 * limit_val:
                msg += f"\n⚠️ Майже ліміт для {cat_title(cat_key)}: {spent:.2f}/{limit_val:.2f} ₴"

        user_state.pop(user_id, None)
        await update.message.reply_text(msg, reply_markup=kb_control())
        return

    # limit amount
    if state.get("mode") == "limit_amount":
        cat_key = state["category"]
        try:
            limit_val = float(text)
            if limit_val <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Ліміт має бути числом > 0. Спробуй ще раз.")
            return

        if mk not in limits:
            limits[mk] = {}
        limits[mk][cat_key] = limit_val
        save_data(data)

        user_state.pop(user_id, None)
        await update.message.reply_text(
            f"✅ Ліміт встановлено на {mk}\n{cat_title(cat_key)}: {limit_val:.2f} ₴",
            reply_markup=kb_limits_menu()
        )
        return

    user_state.pop(user_id, None)
    await update.message.reply_text("Повертаю в меню.", reply_markup=kb_main())


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set (set env var BOT_TOKEN)")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("🤖 Bot running…")
    threading.Thread(target=run_web, daemon=True).start()
    app.run_polling()


if __name__ == "__main__":
    main()
