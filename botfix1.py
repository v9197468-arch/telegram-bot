import os
import json
import threading
from pathlib import Path
from datetime import datetime, timedelta

from flask import Flask

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --------- settings ---------
BOT_TOKEN = "8296396857:AAFb0LdFgWbAbsGI8ruz2h_XcqAEmufM-xY"
ADMIN_ID = 7894377511

DATA_FILE = Path("data.json")
START_IMAGE = Path("d3e540ff-0d1b-44a1-9305-fd9c564a9d5c.png")

PAYMENT_CARD = "5232 4410 1215 9542"
PREMIUM_PLANS = {
    "30": {"title": "1 місяць", "price": "100 грн", "days": 30},
    "90": {"title": "3 місяці", "price": "200 грн", "days": 90},
    "365": {"title": "1 рік", "price": "600 грн", "days": 365},
}

# --------- small web server for Render ---------
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "OK", 200


def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)


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

    for uid, u in list(data["users"].items()):
        if not isinstance(u, dict):
            data["users"][uid] = {
                "records": [],
                "limits": {},
                "profile": {},
                "last_active": "—",
                "premium_until": "",
                "goals": [],
                "pending_payment": {},
            }
            continue

        u.setdefault("records", [])
        u.setdefault("limits", {})
        u.setdefault("profile", {})
        u.setdefault("last_active", "—")

    return data


def save_data(data: dict) -> None:
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def ensure_user(data: dict, user_id: str) -> None:
    if user_id not in data["users"] or not isinstance(data["users"][user_id], dict):
        data["users"][user_id] = {
            "records": [],
            "limits": {},
            "profile": {},
            "last_active": "—",
            "premium_until": "",
            "goals": [],
            "pending_payment": {},
        }
        return

    data["users"][user_id].setdefault("records", [])
    data["users"][user_id].setdefault("limits", {})
    data["users"][user_id].setdefault("profile", {})
    data["users"][user_id].setdefault("last_active", "—")
    data["users"][user_id].setdefault("premium_until", "")
    data["users"][user_id].setdefault("goals", [])
    data["users"][user_id].setdefault("pending_payment", {})


def is_premium_user(user_data: dict) -> bool:
    premium_until = user_data.get("premium_until", "")
    if not premium_until:
        return False

    try:
        until = datetime.fromisoformat(premium_until)
    except Exception:
        return False

    return until > datetime.now()


def premium_until_text(user_data: dict) -> str:
    if not is_premium_user(user_data):
        return "не активний"

    until = datetime.fromisoformat(user_data.get("premium_until"))
    return until.strftime("%d.%m.%Y %H:%M")


def activate_premium(data: dict, user_id: str, days: int) -> datetime:
    ensure_user(data, user_id)
    current = data["users"][user_id].get("premium_until", "")

    try:
        current_until = datetime.fromisoformat(current) if current else datetime.now()
    except Exception:
        current_until = datetime.now()

    base = current_until if current_until > datetime.now() else datetime.now()
    new_until = base + timedelta(days=days)

    data["users"][user_id]["premium_until"] = new_until.isoformat(timespec="seconds")
    data["users"][user_id]["pending_payment"] = {}
    save_data(data)
    return new_until


def require_premium_text() -> str:
    return (
        "🔒 Ця функція доступна тільки у Premium.\n\n"
        "Натисніть 💳 Купити Premium, щоб відкрити доступ."
    )


def calc_month_total(records: list, mkey: str, rtype: str) -> float:
    return sum(float(r.get("amount", 0)) for r in records if r.get("type") == rtype and r.get("month") == mkey)


def get_top_expense_category(records: list, mkey: str) -> tuple[str, float]:
    best_title = "—"
    best_sum = 0.0

    for cat_key, title in CATEGORIES:
        s = sum_month_expenses_by_cat(records, mkey, cat_key)
        if s > best_sum:
            best_sum = s
            best_title = title

    return best_title, best_sum


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
        [InlineKeyboardButton("💎 Premium", callback_data="menu:premium")],
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



def kb_premium_menu(is_premium: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("💳 Купити Premium", callback_data="premium:buy")],
        [InlineKeyboardButton("🎯 Мої фінансові цілі", callback_data="premium:goals")],
        [InlineKeyboardButton("📈 Аналітика Pro", callback_data="premium:analytics")],
        [InlineKeyboardButton("👑 Фінансова дисципліна", callback_data="premium:discipline")],
        [InlineKeyboardButton("📄 Premium звіт", callback_data="premium:report")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(rows)


def kb_buy_premium() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я оплатив 1 місяць", callback_data="premium:paid:30")],
        [InlineKeyboardButton("✅ Я оплатив 3 місяці", callback_data="premium:paid:90")],
        [InlineKeyboardButton("✅ Я оплатив 1 рік", callback_data="premium:paid:365")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:premium")],
    ])


def kb_goals_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати ціль", callback_data="goal:add")],
        [InlineKeyboardButton("📋 Показати цілі", callback_data="goal:list")],
        [InlineKeyboardButton("💰 Поповнити ціль", callback_data="goal:add_money")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:premium")],
    ])


# --------- admin helpers ---------
def is_admin(chat_id: int) -> bool:
    return ADMIN_ID != 0 and chat_id == ADMIN_ID


# --------- commands ---------
async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Твій chat_id: {update.effective_chat.id}")


async def photo_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        return

    if not update.message.photo:
        await update.message.reply_text("Пришли картинку как фото.")
        return

    file_id = update.message.photo[-1].file_id
    await update.message.reply_text(f"FILE_ID:\n{file_id}")


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

        who = f"@{username}" if username and username != "no_username" else name

        lines.append(
            f"• {who} (id {uid})\n"
            f"  записів: {len(records)}\n"
            f"  активність: {last_active}"
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
        except Exception as e:
            print(f"BROADCAST ERROR user={uid}: {e}")
            failed += 1

    await update.message.reply_text(f"✅ Розсилка завершена\nВідправлено: {sent}\nПомилок: {failed}")



async def givepremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("⛔ Доступ заборонено")
        return

    if not context.args:
        await update.message.reply_text("Використання: /givepremium user_id [days]")
        return

    target_id = str(context.args[0])
    days = 30

    if len(context.args) >= 2:
        try:
            days = int(context.args[1])
        except ValueError:
            await update.message.reply_text("Кількість днів має бути числом.")
            return

    data = load_data()
    ensure_user(data, target_id)
    until = activate_premium(data, target_id, days)

    await update.message.reply_text(
        f"✅ Premium активовано\n"
        f"ID: {target_id}\n"
        f"Днів: {days}\n"
        f"До: {until.strftime('%d.%m.%Y %H:%M')}"
    )

    try:
        await context.bot.send_message(
            chat_id=int(target_id),
            text=(
                "✅ <b>Premium активовано!</b>\n\n"
                f"Доступ дійсний до: <b>{until.strftime('%d.%m.%Y %H:%M')}</b>"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        print(f"GIVE PREMIUM NOTIFY ERROR: {e}")


async def removepremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("⛔ Доступ заборонено")
        return

    if not context.args:
        await update.message.reply_text("Використання: /removepremium user_id")
        return

    target_id = str(context.args[0])

    data = load_data()
    ensure_user(data, target_id)
    data["users"][target_id]["premium_until"] = ""
    data["users"][target_id]["pending_payment"] = {}
    save_data(data)

    await update.message.reply_text(f"✅ Premium вимкнено для ID: {target_id}")

    try:
        await context.bot.send_message(
            chat_id=int(target_id),
            text="⚠️ Ваш Premium-доступ вимкнено.",
        )
    except Exception as e:
        print(f"REMOVE PREMIUM NOTIFY ERROR: {e}")


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
        if START_IMAGE.exists():
            with open(START_IMAGE, "rb") as photo:
                await update.message.reply_photo(photo=photo)
        else:
            print(f"START PHOTO ERROR: file not found: {START_IMAGE}")
    except Exception as e:
        print(f"START PHOTO ERROR: {e}")

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


    # ----- premium menu -----
    if d == "menu:premium":
        user_data = data["users"][user_id]
        status = "✅ активний" if is_premium_user(user_data) else "❌ не активний"
        until = premium_until_text(user_data)

        text = (
            "💎 <b>Premium</b>\n\n"
            f"Статус: <b>{status}</b>\n"
            f"Дійсний до: <b>{until}</b>\n\n"
            "Premium відкриває:\n"
            "🎯 фінансові цілі\n"
            "📈 аналітику Pro\n"
            "👑 фінансову дисципліну\n"
            "📄 Premium-звіт"
        )

        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb_premium_menu(is_premium_user(user_data)))
        return

    if d == "premium:buy":
        text = (
            "💳 <b>Купити Premium</b>\n\n"
            "Тарифи:\n"
            "• 1 місяць — <b>100 грн</b>\n"
            "• 3 місяці — <b>200 грн</b>\n"
            "• 1 рік — <b>600 грн</b>\n\n"
            "Оплата на карту:\n"
            f"<code>{PAYMENT_CARD}</code>\n\n"
            "Після оплати натисніть відповідну кнопку нижче.\n"
            "Я отримаю заявку і вручну підтверджу Premium."
        )
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb_buy_premium())
        return

    if d.startswith("premium:paid:"):
        plan_days = d.split(":")[-1]
        plan = PREMIUM_PLANS.get(plan_days)

        if not plan:
            await q.edit_message_text("Не зрозумів тариф.", reply_markup=kb_buy_premium())
            return

        user = q.from_user
        username = f"@{user.username}" if user.username else "без username"
        name = user.first_name or "NoName"

        data["users"][user_id]["pending_payment"] = {
            "plan_days": plan_days,
            "title": plan["title"],
            "price": plan["price"],
            "requested_at": now_iso(),
        }
        save_data(data)

        await q.edit_message_text(
            "✅ Заявку на Premium відправлено.\n\n"
            "Після перевірки оплати доступ буде активовано.",
            reply_markup=kb_premium_menu(is_premium_user(data["users"][user_id]))
        )

        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "💎 <b>Нова заявка на Premium</b>\n\n"
                    f"👤 Користувач: {name} ({username})\n"
                    f"🆔 ID: <code>{user_id}</code>\n"
                    f"📦 Тариф: <b>{plan['title']}</b>\n"
                    f"💰 Сума: <b>{plan['price']}</b>\n"
                    f"🕒 Час: {now_iso()}\n\n"
                    f"Після перевірки оплати виконай:\n"
                    f"<code>/givepremium {user_id} {plan['days']}</code>"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            print(f"PREMIUM ADMIN NOTIFY ERROR: {e}")

        return

    if d == "premium:goals":
        if not is_premium_user(data["users"][user_id]):
            await q.edit_message_text(require_premium_text(), reply_markup=kb_premium_menu(False))
            return

        await q.edit_message_text("🎯 <b>Мої фінансові цілі</b>\n\nОберіть дію:", parse_mode="HTML", reply_markup=kb_goals_menu())
        return

    if d == "goal:add":
        if not is_premium_user(data["users"][user_id]):
            await q.edit_message_text(require_premium_text(), reply_markup=kb_premium_menu(False))
            return

        user_state[user_id] = {"mode": "goal_name"}
        await q.edit_message_text("🎯 Введіть назву цілі.\n\nНаприклад: Авто, Відпочинок, Подушка безпеки")
        return

    if d == "goal:list":
        if not is_premium_user(data["users"][user_id]):
            await q.edit_message_text(require_premium_text(), reply_markup=kb_premium_menu(False))
            return

        goals = data["users"][user_id].get("goals", [])
        if not goals:
            await q.edit_message_text("🎯 У вас поки немає фінансових цілей.", reply_markup=kb_goals_menu())
            return

        lines = ["🎯 <b>Ваші фінансові цілі:</b>", ""]
        for i, g in enumerate(goals, start=1):
            title = g.get("title", "Ціль")
            target = float(g.get("target", 0))
            saved = float(g.get("saved", 0))
            left = max(target - saved, 0)
            percent = (saved / target * 100) if target > 0 else 0

            lines.append(
                f"{i}. <b>{title}</b>\n"
                f"   Ціль: {target:.2f} ₴\n"
                f"   Накоплено: {saved:.2f} ₴\n"
                f"   Залишилось: {left:.2f} ₴\n"
                f"   Прогрес: {percent:.1f}%\n"
            )

        await q.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=kb_goals_menu())
        return

    if d == "goal:add_money":
        if not is_premium_user(data["users"][user_id]):
            await q.edit_message_text(require_premium_text(), reply_markup=kb_premium_menu(False))
            return

        goals = data["users"][user_id].get("goals", [])
        if not goals:
            await q.edit_message_text("Спочатку створіть хоча б одну ціль.", reply_markup=kb_goals_menu())
            return

        user_state[user_id] = {"mode": "goal_choose_add_money"}
        lines = ["💰 Введіть номер цілі та суму через пробіл.", "", "Приклад: 1 500", ""]
        for i, g in enumerate(goals, start=1):
            lines.append(f"{i}. {g.get('title', 'Ціль')}")
        await q.edit_message_text("\n".join(lines))
        return

    if d == "premium:analytics":
        if not is_premium_user(data["users"][user_id]):
            await q.edit_message_text(require_premium_text(), reply_markup=kb_premium_menu(False))
            return

        cur = month_key()
        prev = prev_month_key()

        cur_exp = calc_month_total(records, cur, "expense")
        prev_exp = calc_month_total(records, prev, "expense")
        cur_inc = calc_month_total(records, cur, "income")
        balance = cur_inc - cur_exp

        top_cat, top_sum = get_top_expense_category(records, cur)

        if prev_exp > 0:
            diff_percent = ((cur_exp - prev_exp) / prev_exp) * 100
            trend = f"{diff_percent:+.1f}% до минулого місяця"
        else:
            trend = "немає даних для порівняння"

        text = (
            "📈 <b>Аналітика Pro</b>\n\n"
            f"Місяць: <b>{cur}</b>\n"
            f"Доходи: <b>{cur_inc:.2f} ₴</b>\n"
            f"Витрати: <b>{cur_exp:.2f} ₴</b>\n"
            f"Баланс: <b>{balance:.2f} ₴</b>\n\n"
            f"Найбільша категорія: <b>{top_cat}</b> — {top_sum:.2f} ₴\n"
            f"Тренд витрат: <b>{trend}</b>"
        )
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb_premium_menu(True))
        return

    if d == "premium:discipline":
        if not is_premium_user(data["users"][user_id]):
            await q.edit_message_text(require_premium_text(), reply_markup=kb_premium_menu(False))
            return

        cur = month_key()
        cur_limits = limits.get(cur, {})
        score = 100

        # penalties for exceeded limits
        for cat_key, limit_val in cur_limits.items():
            spent = sum_month_expenses_by_cat(records, cur, cat_key)
            try:
                limit_val = float(limit_val)
            except Exception:
                continue
            if limit_val > 0 and spent > limit_val:
                score -= 10

        cur_exp = calc_month_total(records, cur, "expense")
        cur_inc = calc_month_total(records, cur, "income")
        if cur_inc > 0 and cur_exp > cur_inc:
            score -= 15

        month_records = [r for r in records if r.get("month") == cur]
        if len(month_records) < 5:
            score -= 10

        score = max(0, min(100, score))

        if score >= 85:
            label = "Відмінний контроль 🟢"
        elif score >= 60:
            label = "Нормально, але є що покращити 🟡"
        else:
            label = "Потрібен жорсткіший контроль 🔴"

        text = (
            "👑 <b>Фінансова дисципліна</b>\n\n"
            f"Ваш рейтинг: <b>{score}/100</b>\n"
            f"{label}"
        )
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb_premium_menu(True))
        return

    if d == "premium:report":
        if not is_premium_user(data["users"][user_id]):
            await q.edit_message_text(require_premium_text(), reply_markup=kb_premium_menu(False))
            return

        cur = month_key()
        cur_inc = calc_month_total(records, cur, "income")
        cur_exp = calc_month_total(records, cur, "expense")
        balance = cur_inc - cur_exp
        top_cat, top_sum = get_top_expense_category(records, cur)

        goals = data["users"][user_id].get("goals", [])
        goals_text = "Немає цілей"
        if goals:
            chunks = []
            for g in goals[:5]:
                title = g.get("title", "Ціль")
                target = float(g.get("target", 0))
                saved = float(g.get("saved", 0))
                percent = (saved / target * 100) if target > 0 else 0
                chunks.append(f"• {title}: {percent:.1f}%")
            goals_text = "\n".join(chunks)

        text = (
            "📄 <b>Premium-звіт</b>\n\n"
            f"Період: <b>{cur}</b>\n"
            f"Доходи: <b>{cur_inc:.2f} ₴</b>\n"
            f"Витрати: <b>{cur_exp:.2f} ₴</b>\n"
            f"Баланс: <b>{balance:.2f} ₴</b>\n"
            f"Найбільша категорія: <b>{top_cat}</b> — {top_sum:.2f} ₴\n\n"
            f"🎯 Цілі:\n{goals_text}"
        )
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb_premium_menu(True))
        return

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

    if d.startswith("expcat:"):
        cat_key = d.split(":", 1)[1]
        user_state[user_id] = {"mode": "expense_amount", "category": cat_key}
        await q.edit_message_text(
            f"{cat_title(cat_key)}\nВведи суму витрати в ₴ (наприклад 150 або 99.90)\n\n(Щоб відмінити — /start)"
        )
        return

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


    # premium goal creation
    if state.get("mode") == "goal_name":
        if not is_premium_user(data["users"][user_id]):
            user_state.pop(user_id, None)
            await update.message.reply_text(require_premium_text(), reply_markup=kb_premium_menu(False))
            return

        if not text:
            await update.message.reply_text("Назва цілі не може бути порожньою.")
            return

        user_state[user_id] = {"mode": "goal_target", "title": text}
        await update.message.reply_text("Введіть суму цілі в ₴.\n\nНаприклад: 50000")
        return

    if state.get("mode") == "goal_target":
        if not is_premium_user(data["users"][user_id]):
            user_state.pop(user_id, None)
            await update.message.reply_text(require_premium_text(), reply_markup=kb_premium_menu(False))
            return

        try:
            target = float(text)
            if target <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Сума цілі має бути числом > 0. Спробуйте ще раз.")
            return

        user_state[user_id] = {
            "mode": "goal_saved",
            "title": state["title"],
            "target": target,
        }
        await update.message.reply_text("Скільки вже накопичено? Якщо нічого — введіть 0.")
        return

    if state.get("mode") == "goal_saved":
        if not is_premium_user(data["users"][user_id]):
            user_state.pop(user_id, None)
            await update.message.reply_text(require_premium_text(), reply_markup=kb_premium_menu(False))
            return

        try:
            saved = float(text)
            if saved < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Сума має бути числом від 0.")
            return

        data["users"][user_id]["goals"].append({
            "title": state["title"],
            "target": state["target"],
            "saved": saved,
            "created_at": now_iso(),
        })
        save_data(data)

        user_state.pop(user_id, None)
        await update.message.reply_text("✅ Фінансову ціль додано.", reply_markup=kb_goals_menu())
        return

    if state.get("mode") == "goal_choose_add_money":
        if not is_premium_user(data["users"][user_id]):
            user_state.pop(user_id, None)
            await update.message.reply_text(require_premium_text(), reply_markup=kb_premium_menu(False))
            return

        parts = text.replace(",", ".").split()
        if len(parts) != 2:
            await update.message.reply_text("Формат: номер_цілі сума\nНаприклад: 1 500")
            return

        try:
            idx = int(parts[0]) - 1
            amount = float(parts[1])
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Номер цілі та сума мають бути числами.")
            return

        goals = data["users"][user_id].get("goals", [])
        if idx < 0 or idx >= len(goals):
            await update.message.reply_text("Такого номера цілі немає.")
            return

        goals[idx]["saved"] = float(goals[idx].get("saved", 0)) + amount
        save_data(data)

        user_state.pop(user_id, None)
        await update.message.reply_text("✅ Ціль поповнено.", reply_markup=kb_goals_menu())
        return


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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f"ERROR: {context.error}")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set")

    threading.Thread(target=run_web, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("givepremium", givepremium))
    app.add_handler(CommandHandler("removepremium", removepremium))
    app.add_handler(MessageHandler(filters.PHOTO, photo_id))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)

    print("🤖 Bot running…")
    app.run_polling()


if __name__ == "__main__":
    main()
