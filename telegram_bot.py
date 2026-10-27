"""Telegram bot for Deye solar inverter monitoring notifications."""
import json
import os
import time
import logging
import threading
from datetime import datetime, date
from calendar import monthrange
import requests

from storage import atomic_write_json, load_json
try:
    from poems import get_poem
except ImportError:
    get_poem = None

logger = logging.getLogger(__name__)

# 1800s literary Ukrainian style messages
MESSAGES_BATTERY_LOW = [
    "Шановне панство! Сповіщаю з превеликим жалем, що сховище наше знесилене — "
    "лишилось йому сили на <b>{soc}%</b>. Як козак без шаблі, так і хата без струму — біда, та й годі!",

    "Вельмишановні добродії! Сховище наше, мов чумак у степу безводному, "
    "ледве животіє — заряду лишилось <b>{soc}%</b>. Готуйте свічки та лучину, бо темрява надходить!",

    "Панове та паніматки! Маю честь доповісти, що сховище наше зовсім "
    "захиріло — <b>{soc}%</b> і то ледь дише. Як казав мій дід: без сили нема й долі!",
]

MESSAGES_GRID_RESTORED = [
    "Радійте, люди добрі! Електрика, мов блудний син, повернулася до нашої оселі! "
    "Знову тече струм по дротах, як мед по вусах — хвала небесам і {provider_name}!",

    "Ура, панове! Струм повернувся, наче козак із походу — з перемогою! "
    "{provider_name} змилувалось над нами грішними. Вмикайте самовари та електричні машини!",

    "Слава! Слава! Тричі слава! Електрика знов освітила наші палати! "
    "Годі вже при свічках куняти — цивілізація повернулась до нашого маєтку!",
]

MESSAGES_TEST_BATTERY = (
    "[ ТЕСТ ] Шановне панство! Це є випробування сповіщення про сховище енергії. "
    "Уявіть собі: сховище наше, мов старий дідуган, "
    "ледве тримається на ногах — заряду йому лишилось аж 15%! "
    "Як казав славетний Котляревський: «Еней був парубок моторний» — "
    "а наше сховище вже ні!"
)

MESSAGES_TEST_GRID = (
    "[ ТЕСТ ] Гей, панове-товариство! Це є випробування сповіщення про електрику. "
    "Уявіть: струм повернувся! Мов Прометей вогонь людям приніс, "
    "так і {provider_name} нам знову електрику подало! "
    "Припиніть голосити та ховати сало — світло є!"
)

# --- Outage schedule messages ---

MESSAGES_OUTAGE_ACTIVE = [
    "Терпіння, добродію! За графіком {provider_name}, світло має повернутись о <b>{end_time}</b>. "
    "Лишилось чекати <b>{remaining}</b>. Як казав Шевченко: «Борітеся — поборете!»",

    "Тримайтесь, панове! Темрява панує, але не вічно — о <b>{end_time}</b> має бути світло. "
    "Ще <b>{remaining}</b> і знову заживемо як люди! Козак терпів і нам велів!",

    "Не журіться, шановне панство! {provider_name} обіцяє повернути струм о <b>{end_time}</b>. "
    "Лишилось <b>{remaining}</b>. Як то кажуть: ніч найтемніша перед світанком!",
]

MESSAGES_UPCOMING_BATTERY_OK = [
    "Світло є, панове! За графіком темрява прийде з <b>{start_time}</b> до <b>{end_time}</b>. "
    "Але не журіться — сховище наше на <b>{soc}%</b>, як добрий козак при повній зброї. "
    "Вистачить з лишком!",

    "Струм тече, хвала небесам! Відключення заплановано з <b>{start_time}</b> до <b>{end_time}</b>. "
    "Сховище на <b>{soc}%</b> — це як повний льох перед зимою. Переживемо!",

    "Електрика є, панове-товариство! {provider_name} планує темряву з <b>{start_time}</b> до <b>{end_time}</b>. "
    "Але сховище на <b>{soc}%</b> — це нам як козакові шабля при боці. Нічого не страшно!",
]

MESSAGES_UPCOMING_BATTERY_TIGHT = [
    "Світло є, але обережно! Темрява прийде з <b>{start_time}</b> до <b>{end_time}</b>, "
    "а сховище лише на <b>{soc}%</b>. При теперішньому споживанні (<b>{load}W</b>) може не дотягнути. "
    "Вимикайте зайве, панове!",

    "Струм поки є, але хмари збираються! З <b>{start_time}</b> до <b>{end_time}</b> — відключення. "
    "Сховище на <b>{soc}%</b>, а споживання <b>{load}W</b> — на межі. "
    "Як казав мій дід: «Бережи сало смолоду!»",

    "Електрика є, та не розслабляйтесь! З <b>{start_time}</b> до <b>{end_time}</b> буде темно. "
    "Сховище на <b>{soc}%</b> при споживанні <b>{load}W</b> — це як йти в дорогу з малою торбою. "
    "Зменшіть апетити, добродії!",
]

MESSAGES_UPCOMING_BATTERY_LOW = [
    "Світло є, але біда на порозі! З <b>{start_time}</b> до <b>{end_time}</b> — відключення, "
    "а сховище на жалюгідних <b>{soc}%</b>. При споживанні <b>{load}W</b> це як йти в бій "
    "з порожніми кишенями!",

    "Струм поки тече, але лихо чекає! З <b>{start_time}</b> до <b>{end_time}</b> {provider_name} вимкне світло. "
    "Сховище лише на <b>{soc}%</b>, а споживання <b>{load}W</b> — не вистачить, хоч плач! "
    "Готуйте свічки та лучину!",

    "Електрика є, та ненадовго! З <b>{start_time}</b> до <b>{end_time}</b> прийде темрява. "
    "Сховище на <b>{soc}%</b> при <b>{load}W</b> — це як чумак без волів у степу. "
    "Будьте готові до найгіршого, панове!",
]

MESSAGES_OUTAGE_CLEAR = [
    "Радійте, панове! Сьогодні {provider_name} милостиве — жодних відключень "
    "для нашої групи! Живемо як пани!",

    "Слава! Сьогодні {provider_name} дарує нам спокій — жодних відключень! "
    "Користуйтесь електрикою на повну, як пан у своєму маєтку!",

    "Гарна новина, добродії! Сьогодні графік відключень нас оминає! "
    "Можна жити спокійно, як за гетьмана Мазепи у мирні часи!",
]

MESSAGES_GRID_DOWN = [
    "Панове, світло зникло! {schedule_info} "
    "Сховище на <b>{soc}%</b> — {battery_verdict}",

    "Увага, добродії! Електрику вимкнули! {schedule_info} "
    "Сховище наше на <b>{soc}%</b> — {battery_verdict}",

    "Біда, панове-товариство! Струм пропав! {schedule_info} "
    "Заряд сховища <b>{soc}%</b> — {battery_verdict}",
]

MESSAGES_OUTAGE_UNKNOWN = [
    "Перепрошую, добродію! Не вдалося дізнатись графік відключень — "
    "зв'язок із {provider_name} загубився, як лист у бурю. "
    "Перевірте самостійно на сайті постачальника.",

    "Вибачайте, панове! Графік відключень недоступний — "
    "мабуть, і в {provider_name} світло вимкнули! "
    "Спробуйте пізніше.",
]


_APP_DIR = os.path.dirname(os.path.abspath(__file__))
# In data/ when it exists (persistent Docker volume), like the other data files
BATTERY_REPORT_DIR = os.path.join(_APP_DIR, "data", "battery_reports") if os.path.isdir(os.path.join(_APP_DIR, "data")) \
    else os.path.join(_APP_DIR, "battery_reports")
BATTERY_REPORTS_KEEP = 50  # newest report files kept; older ones are deleted

# Low-battery alert fires below LOW and re-arms only at/above REARM, so a SOC
# hovering around the threshold doesn't send the alert again and again
BATTERY_LOW_SOC = 30
BATTERY_LOW_REARM_SOC = 35


# Inverter data older than this (from the dashboard's poller) is treated as unavailable
INVERTER_DATA_MAX_AGE = 180  # seconds


class TelegramBot:
    def __init__(self, token, allowed_users, inverter,
                 outage_poller=None, state_file=None, grid_daily_log_file=None,
                 weather_poller=None, is_public=False, inverter_poller=None, readiness=None):
        self.token = token
        self.allowed_users = set(allowed_users)
        self.is_public = is_public
        self.inverter = inverter
        self.inverter_poller = inverter_poller
        # () -> the dashboard's outage readiness (app.current_outage_readiness):
        # the outage message's verdict, so it matches the dashboard's card
        self.readiness = readiness
        self.outage_poller = outage_poller
        self.grid_daily_log_file = grid_daily_log_file
        self.weather_poller = weather_poller
        self.api_url = f"https://api.telegram.org/bot{token}"
        self.last_update_id = 0
        self.message_index = 0

        # Monitoring state
        self.battery_low_notified = False
        self.grid_down_since = None
        self.grid_up_since = None
        self.grid_confirmed_down = False

        self._running = False
        self._thread = None
        self.state_file = state_file
        self._load_state()

    def _load_state(self):
        """Load monitoring state from file if available."""
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            state = load_json(self.state_file, {})
            self.grid_confirmed_down = state.get("grid_confirmed_down", False)
            self.battery_low_notified = state.get("battery_low_notified", False)
            self.grid_down_since = state.get("grid_down_since")
            self.grid_up_since = state.get("grid_up_since")
            self.last_update_id = state.get("last_update_id", 0)
            logger.info("Loaded bot state from %s", self.state_file)
        except Exception:
            logger.exception("Failed to load bot state from %s, using defaults", self.state_file)

    def _save_state(self):
        """Persist monitoring state to file."""
        if not self.state_file:
            return
        state = {
            "grid_confirmed_down": self.grid_confirmed_down,
            "battery_low_notified": self.battery_low_notified,
            "grid_down_since": self.grid_down_since,
            "grid_up_since": self.grid_up_since,
            "last_update_id": self.last_update_id,
        }
        # Called every few seconds; only write when something changed (SD card wear)
        if state == getattr(self, "_last_saved_state", None):
            return
        try:
            atomic_write_json(self.state_file, state, indent=2)
            self._last_saved_state = state
        except Exception:
            logger.exception("Failed to save bot state to %s", self.state_file)

    def _save_battery_report(self, data, trigger):
        """Save a debug report file with all inverter data when a battery report is sent."""
        os.makedirs(BATTERY_REPORT_DIR, exist_ok=True)
        ts = datetime.now()
        filename = ts.strftime(f"%Y-%m-%d_%H-%M-%S_{trigger}.json")
        filepath = os.path.join(BATTERY_REPORT_DIR, filename)
        report = {
            "timestamp": ts.isoformat(),
            "trigger": trigger,
            "inverter_data": data,
        }
        smoother = getattr(self.inverter, "battery_smoother", None)
        if smoother is not None:
            report["battery_smoothing"] = smoother.snapshot()
        try:
            atomic_write_json(filepath, report, indent=2, default=str)
            logger.info("Battery report saved to %s", filepath)
        except Exception:
            logger.exception("Failed to save battery report to %s", filepath)
        self._prune_battery_reports()

    def _prune_battery_reports(self):
        """Keep only the newest BATTERY_REPORTS_KEEP report files (names sort by time)."""
        try:
            reports = sorted(f for f in os.listdir(BATTERY_REPORT_DIR) if f.endswith(".json"))
            for old in reports[:-BATTERY_REPORTS_KEEP]:
                os.remove(os.path.join(BATTERY_REPORT_DIR, old))
        except OSError:
            logger.exception("Failed to prune battery reports")

    def _get_provider_name(self):
        """Return human-readable name of the outage provider."""
        if self.outage_poller and hasattr(self.outage_poller, 'provider'):
            return self.outage_poller.provider.display_name
        return "енергопостачальник"

    def _is_authorized(self, user_id):
        """Check if user is authorized to use the bot."""
        return self.is_public or user_id in self.allowed_users

    def _pick_message(self, messages, **kwargs):
        """Pick next message from rotation and format it."""
        msg = messages[self.message_index % len(messages)]
        self.message_index += 1
        return msg.format(**kwargs)

    def _format_poem(self):
        """Get a formatted poem based on current weather data."""
        if get_poem is None:
            return ""
        weather_code = None
        sunrise = None
        sunset = None
        if self.weather_poller:
            data = self.weather_poller.data
            if data:
                weather_code = data.get("weather_code")
                sunrise = data.get("sunrise")
                sunset = data.get("sunset")
        return get_poem(weather_code, sunrise, sunset)

    def _append_poem(self, msg):
        """Append a poem to a message string."""
        poem = self._format_poem()
        if poem:
            return msg + "\n\n" + poem
        return msg

    def send_message(self, chat_id, text, reply_markup=None):
        """Send a message to a specific chat with exponential backoff retry."""
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup

        for attempt in range(4):  # up to 4 attempts: 0s, 2s, 4s, 8s
            backoff = 2 ** attempt
            try:
                resp = requests.post(
                    f"{self.api_url}/sendMessage",
                    json=payload,
                    timeout=10,
                )
                if resp.ok:
                    return True
                logger.error("Failed to send message (attempt %d): %s %s",
                             attempt + 1, resp.status_code, resp.text[:200])
                if resp.status_code == 429:
                    # Rate limited: Telegram says how long to wait
                    try:
                        backoff = int(resp.json()["parameters"]["retry_after"])
                    except (ValueError, KeyError, TypeError):
                        pass
                elif 400 <= resp.status_code < 500:
                    # Bad request, bot blocked by the user, etc.: retrying won't help
                    return False
            except requests.RequestException as e:
                logger.warning("Error sending Telegram message (attempt %d): %s", attempt + 1, type(e).__name__)
            if attempt < 3:
                logger.info("Retrying send_message in %ds (attempt %d/4)", backoff, attempt + 2)
                time.sleep(backoff)

        logger.error("Failed to send message after 4 attempts to chat %s", chat_id)
        return False

    def broadcast(self, text):
        """Send a message to all allowed users."""
        for user_id in self.allowed_users:
            self.send_message(user_id, text)

    def poll_commands(self):
        """Check for incoming bot commands with backoff retry."""
        updates = None
        for attempt in range(3):  # up to 3 attempts: 0s, 2s, 4s
            try:
                resp = requests.get(
                    f"{self.api_url}/getUpdates",
                    params={"offset": self.last_update_id + 1, "timeout": 0},
                    timeout=10,
                )
                if resp.ok:
                    updates = resp.json().get("result", [])
                    self._poll_failures = 0
                    break
                logger.warning("Telegram getUpdates failed (attempt %d): %s", attempt + 1, resp.status_code)
            except Exception:
                logger.warning("Error polling Telegram updates (attempt %d)", attempt + 1)
            if attempt < 2:
                time.sleep(2 ** attempt)

        if updates is None:
            # Capped: the backoff tops out at 60 s long before this
            self._poll_failures = min(getattr(self, '_poll_failures', 0) + 1, 10)
            return

        for update in updates:
            self.last_update_id = update["update_id"]
            try:
                self._dispatch_update(update)
            except Exception:
                # Log and move on: the offset above is already advanced (and saved
                # by the loop), so a message that crashes a handler isn't replayed
                logger.exception("Error handling Telegram update %s", update.get("update_id"))

    def _dispatch_update(self, update):
        """Route one Telegram update to its command handler."""
        message = update.get("message")
        if not message or not message.get("text"):
            return

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = message["text"].strip()

        if text == "/start":
            self._handle_start(chat_id, user_id)
        elif text == "/test":
            self._handle_test(chat_id, user_id)
        elif text in ("/battery", "⚡ Сховище енергії"):
            self._handle_battery(chat_id, user_id)
        elif text in ("/outage", "💡 Коли включать світло?"):
            self._handle_outage(chat_id, user_id)
        elif text in ("/grid", "📊 Спожито з мережі"):
            self._handle_grid_consumption(chat_id, user_id)

    def _main_keyboard(self):
        """Return the persistent reply keyboard."""
        return {
            "keyboard": [
                [{"text": "⚡ Сховище енергії"}],
                [{"text": "💡 Коли включать світло?"}],
                [{"text": "📊 Спожито з мережі"}],
            ],
            "resize_keyboard": True,
        }

    def _handle_start(self, chat_id, user_id):
        """Handle /start command."""
        if user_id in self.allowed_users:
            msg = (
                f"Вітаю, добродію! Ваш Telegram ID: {user_id}\n"
                f"Ви у списку дозволених. Сповіщення увімкнено."
            )
            self.send_message(
                chat_id,
                self._append_poem(msg),
                reply_markup=self._main_keyboard(),
            )
        elif self.is_public:
            msg = (
                f"Вітаю, добродію! Цей бот працює у публічному режимі.\n"
                f"Використовуйте кнопки нижче для перевірки стану."
            )
            self.send_message(
                chat_id,
                self._append_poem(msg),
                reply_markup=self._main_keyboard(),
            )
        else:
            self.send_message(
                chat_id,
                f"Ваш Telegram ID: {user_id}\n"
                f"Додайте цей ID до змінної TELEGRAM_ALLOWED_USERS щоб отримувати сповіщення.",
            )

    def _handle_test(self, chat_id, user_id):
        """Handle /test command — send both sample messages."""
        if not self._is_authorized(user_id):
            self.send_message(chat_id, f"Ваш ID ({user_id}) не у списку дозволених.")
            return

        self.send_message(chat_id, self._append_poem(MESSAGES_TEST_BATTERY))
        self.send_message(chat_id, self._append_poem(
            MESSAGES_TEST_GRID.format(provider_name=self._get_provider_name())
        ))

    def _handle_battery(self, chat_id, user_id):
        """Handle battery status request."""
        if not self._is_authorized(user_id):
            self.send_message(chat_id, f"Ваш ID ({user_id}) не у списку дозволених.")
            return

        if not self.inverter.config.has_battery:
            self.send_message(chat_id, "Сховище енергії не налаштовано для цього інвертора.")
            return

        try:
            data = self._get_inverter_data()
        except Exception:
            self.send_message(chat_id, "Не вдалося зчитати дані з інвертора.")
            return

        if data.get("error"):
            self.send_message(chat_id, "Не вдалося зчитати дані з інвертора.")
            return

        self._save_battery_report(data, "user_request")

        soc = data.get("battery_soc", 0)
        voltage = data.get("battery_voltage", 0)
        power = data.get("battery_power", 0)
        status = data.get("battery_status", "Невідомо")

        if soc >= 80:
            mood = "Сховище наше повне сил, мов козак після відпочинку! 💪"
        elif soc >= 50:
            mood = "Сховище тримається молодцем, ще повоює! ⚡"
        elif soc >= 30:
            mood = "Сховище починає втомлюватись, варто придивитись... 👀"
        else:
            mood = "Сховище ледве дише, як чумак у пустелі! 🫠"

        msg = (
            f"🔋 Заряд: <b>{soc}%</b>\n"
            f"⚡ Напруга: <b>{voltage:.1f}V</b>\n"
            f"🔌 Потужність: <b>{power}W</b>\n"
            f"📊 Стан: {status}\n\n"
            f"{mood}"
        )
        self.send_message(chat_id, self._append_poem(msg))

    def _handle_outage(self, chat_id, user_id):
        """Handle outage schedule request."""
        if not self._is_authorized(user_id):
            self.send_message(chat_id, f"Ваш ID ({user_id}) не у списку дозволених.")
            return

        if not self.outage_poller:
            self.send_message(chat_id, "Моніторинг графіку відключень не налаштовано.")
            return

        status = self.outage_poller.get_outage_status()

        if status["status"] == "active":
            end_time = status["end_time"].strftime("%H:%M")
            remaining_min = status["remaining_minutes"]
            hours = remaining_min // 60
            mins = remaining_min % 60
            if hours > 0:
                remaining = f"{hours} год {mins} хв"
            else:
                remaining = f"{mins} хв"
            msg = self._pick_message(
                MESSAGES_OUTAGE_ACTIVE, end_time=end_time, remaining=remaining,
                provider_name=self._get_provider_name(),
            )

        elif status["status"] == "upcoming":
            windows = status["upcoming_windows"]
            start_dt, end_dt = windows[0]
            start_time = start_dt.strftime("%H:%M")
            end_time = end_dt.strftime("%H:%M")

            # The battery verdict is the dashboard's own (outage_readiness.py: the
            # real capacity, the shutdown SOC, the recent average load)
            readiness = {}
            if self.inverter.config.has_battery and self.readiness:
                try:
                    readiness = self.readiness() or {}
                except Exception:
                    logger.warning("Outage readiness unavailable for the outage message")
            verdict_messages = {"enough": MESSAGES_UPCOMING_BATTERY_OK,
                                "tight": MESSAGES_UPCOMING_BATTERY_TIGHT,
                                "short": MESSAGES_UPCOMING_BATTERY_LOW}

            if not self.inverter.config.has_battery:
                msg = (
                    f"Увага! Відключення заплановано з <b>{start_time}</b> "
                    f"до <b>{end_time}</b>. Сховища немає — чекаємо на мережу."
                )
            elif readiness.get("status") == "ok" and readiness.get("verdict") in verdict_messages:
                msg = self._pick_message(
                    verdict_messages[readiness["verdict"]],
                    start_time=start_time, end_time=end_time,
                    soc=readiness.get("soc", 0), load=readiness.get("average_load_w", 0),
                    provider_name=self._get_provider_name(),
                )
            else:
                # No verdict (no recent load average or battery data yet): just the facts
                data = self._get_inverter_data() or {}
                soc = data.get("battery_soc") if not data.get("error") else None
                msg = (
                    f"Увага! Відключення заплановано з <b>{start_time}</b> до <b>{end_time}</b>."
                    + (f" Сховище на <b>{soc}%</b>." if soc is not None else "")
                )

            # If there are more windows today, append them
            if len(windows) > 1:
                extra = ", ".join(
                    f"з {s.strftime('%H:%M')} до {e.strftime('%H:%M')}"
                    for s, e in windows[1:]
                )
                msg += f"\n\nТакож заплановано: {extra}"

        elif status["status"] == "clear":
            msg = self._pick_message(MESSAGES_OUTAGE_CLEAR,
                                     provider_name=self._get_provider_name())

        else:
            msg = self._pick_message(MESSAGES_OUTAGE_UNKNOWN,
                                     provider_name=self._get_provider_name())

        self.send_message(chat_id, self._append_poem(msg))

    def _load_grid_daily_log(self):
        """Load grid daily import log from file."""
        if not self.grid_daily_log_file:
            return {}
        try:
            return load_json(self.grid_daily_log_file, {})
        except Exception:
            logger.exception("Failed to load grid daily log")
            return {}

    def _sum_month(self, log, year, month):
        """Sum daily grid import values for a given year/month. Returns (total_kwh, days_covered, first_day, last_day)."""
        prefix = f"{year:04d}-{month:02d}-"
        days = []
        total = 0.0
        for day_str, kwh in log.items():
            if day_str.startswith(prefix):
                days.append(day_str)
                total += kwh
        if not days:
            return 0.0, 0, None, None
        days.sort()
        return total, len(days), days[0], days[-1]

    def _handle_grid_consumption(self, chat_id, user_id):
        """Handle grid consumption request — show monthly totals."""
        if not self._is_authorized(user_id):
            self.send_message(chat_id, f"Ваш ID ({user_id}) не у списку дозволених.")
            return

        log = self._load_grid_daily_log()
        if not log:
            self.send_message(
                chat_id,
                "Поки що немає даних про споживання з мережі. "
                "Дані почнуть збиратися автоматично.",
            )
            return

        MONTH_NAMES = {
            1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень",
            5: "Травень", 6: "Червень", 7: "Липень", 8: "Серпень",
            9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень",
        }
        MONTH_NAMES_GEN = {
            1: "січня", 2: "лютого", 3: "березня", 4: "квітня",
            5: "травня", 6: "червня", 7: "липня", 8: "серпня",
            9: "вересня", 10: "жовтня", 11: "листопада", 12: "грудня",
        }

        today = date.today()
        cur_year, cur_month = today.year, today.month

        # Previous month
        if cur_month == 1:
            prev_year, prev_month = cur_year - 1, 12
        else:
            prev_year, prev_month = cur_year, cur_month - 1

        cur_total, cur_days, cur_first, cur_last = self._sum_month(log, cur_year, cur_month)
        prev_total, prev_days, prev_first, prev_last = self._sum_month(log, prev_year, prev_month)

        lines = ["📊 Споживання з мережі\n"]

        # Current month
        month_name = MONTH_NAMES[cur_month]
        if cur_days > 0:
            first_day = int(cur_first.split("-")[2])
            last_day = int(cur_last.split("-")[2])
            gen_name = MONTH_NAMES_GEN[cur_month]
            lines.append(f"{month_name} {cur_year} (поточний):")
            lines.append(f"<b>{cur_total:.1f} кВт·год</b> ({first_day}-{last_day} {gen_name})")
        else:
            lines.append(f"{month_name} {cur_year} (поточний):")
            lines.append("Ще немає даних")

        lines.append("")

        # Previous month
        prev_name = MONTH_NAMES[prev_month]
        if prev_days > 0:
            _, max_day = monthrange(prev_year, prev_month)
            if prev_days >= max_day - 1:
                lines.append(f"{prev_name} {prev_year}:")
                lines.append(f"<b>{prev_total:.1f} кВт·год</b>")
            else:
                first_day = int(prev_first.split("-")[2])
                last_day = int(prev_last.split("-")[2])
                gen_name = MONTH_NAMES_GEN[prev_month]
                lines.append(f"{prev_name} {prev_year} (неповний):")
                lines.append(f"<b>{prev_total:.1f} кВт·год</b> ({first_day}-{last_day} {gen_name})")
        else:
            lines.append(f"{prev_name} {prev_year}:")
            lines.append("Немає даних")

        self.send_message(chat_id, self._append_poem("\n".join(lines)))

    def _broadcast_grid_down(self, soc):
        """Broadcast grid-down notification with schedule and battery info."""
        # Get schedule info
        provider_name = self._get_provider_name()
        schedule_info = ""
        if self.outage_poller:
            status = self.outage_poller.get_outage_status()
            if status["status"] == "active":
                end_time = status["end_time"].strftime("%H:%M")
                remaining_min = status["remaining_minutes"]
                hours = remaining_min // 60
                mins = remaining_min % 60
                if hours > 0:
                    remaining = f"{hours} год {mins} хв"
                else:
                    remaining = f"{mins} хв"
                schedule_info = (
                    f"За графіком {provider_name}, світло має повернутись о <b>{end_time}</b> "
                    f"(ще <b>{remaining}</b>)."
                )
            else:
                schedule_info = (
                    f"Цього відключення немає у графіку {provider_name} — "
                    "можливо, аварійне."
                )
        else:
            schedule_info = "Графік відключень недоступний."

        # Battery verdict
        if not self.inverter.config.has_battery:
            battery_verdict = "сховища немає, чекаємо на мережу."
        elif soc >= 70:
            battery_verdict = "тримаємось як козаки, вистачить надовго!"
        elif soc >= 40:
            battery_verdict = "протримаємось, але без зайвого марнотратства."
        elif soc >= 20:
            battery_verdict = "маловато буде, панове. Економте!"
        else:
            battery_verdict = "зовсім кепсько, готуйте свічки!"

        msg = self._pick_message(
            MESSAGES_GRID_DOWN,
            schedule_info=schedule_info, soc=soc, battery_verdict=battery_verdict,
        )
        self.broadcast(self._append_poem(msg))

    def _get_inverter_data(self):
        """Latest inverter data from the dashboard's poller (no extra inverter reads).

        The poller already reads the inverter every 30 s; reading it again from
        the bot would hold the inverter lock and add load on the logger. Falls back
        to a direct read only when no poller is wired in.
        """
        if self.inverter_poller is None:
            return self.inverter.read_all_data()
        data = self.inverter_poller.data
        if not data:
            return {"error": "no inverter data yet"}
        try:
            if isinstance(data.get("timestamp"), (int, float)):
                age = time.time() - data["timestamp"]   # epoch: unaffected by DST changes
            else:
                age = (datetime.now() - datetime.fromisoformat(data["last_updated"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = None
        if age is None or age > INVERTER_DATA_MAX_AGE:
            return {"error": "inverter data is stale"}
        return data

    def check_inverter(self):
        """Read inverter data and check alert conditions."""
        try:
            data = self._get_inverter_data()
        except Exception:
            logger.exception("Error reading inverter data for Telegram bot")
            return

        if "error" in data and data["error"]:
            return

        grid_voltage = data.get("grid_voltage", 230)
        has_battery = self.inverter.config.has_battery
        now = time.time()

        # --- Battery monitoring ---
        if has_battery:
            soc = data.get("battery_soc", 100)
            battery_voltage = data.get("battery_voltage", 0)

            # Sanity check: if battery voltage reads as 0 or near-0, the inverter
            # returned a glitched value. Skip this reading to avoid false alerts.
            if battery_voltage < 10:
                logger.warning(
                    "Skipping inverter check: battery voltage %.1fV looks like a glitch (SOC=%s%%)",
                    battery_voltage, soc,
                )
                return

            if soc < BATTERY_LOW_SOC and not self.battery_low_notified:
                self._save_battery_report(data, "battery_low_alert")
                msg = self._pick_message(MESSAGES_BATTERY_LOW, soc=soc)
                self.broadcast(self._append_poem(msg))
                self.battery_low_notified = True
                logger.info("Battery low notification sent (SOC=%s%%)", soc)
            elif soc >= BATTERY_LOW_REARM_SOC and self.battery_low_notified:
                self.battery_low_notified = False

        # --- Grid monitoring with 2-minute debounce ---
        # grid_online: from the inverter's grid relay (on battery although a phase
        # may still show voltage); older data without it: voltage
        grid_online = data.get("grid_online")
        grid_is_down = (not grid_online) if isinstance(grid_online, bool) else grid_voltage < 50
        soc_for_grid = data.get("battery_soc", 0) if has_battery else 0

        if grid_is_down:
            self.grid_up_since = None
            if self.grid_down_since is None:
                self.grid_down_since = now
            elif not self.grid_confirmed_down and (now - self.grid_down_since) >= 120:
                self.grid_confirmed_down = True
                self._broadcast_grid_down(soc_for_grid)
                logger.info("Grid confirmed down (voltage=%.1fV)", grid_voltage)
        else:
            self.grid_down_since = None
            if self.grid_confirmed_down:
                if self.grid_up_since is None:
                    self.grid_up_since = now
                elif (now - self.grid_up_since) >= 60:
                    msg = self._pick_message(MESSAGES_GRID_RESTORED,
                                             provider_name=self._get_provider_name())
                    self.broadcast(self._append_poem(msg))
                    self.grid_confirmed_down = False
                    self.grid_up_since = None
                    logger.info("Grid restored notification sent (voltage=%.1fV)", grid_voltage)

    def run(self, inverter_interval=120, command_interval=5):
        """Main loop: poll commands frequently, check inverter less often."""
        self._running = True
        self._poll_failures = 0
        logger.info(
            "Telegram bot started (commands every %ds, inverter every %ds)",
            command_interval, inverter_interval,
        )
        # time.monotonic(): a wall-clock step back (DST, an NTP sync after boot)
        # would otherwise stall polling and alerts until the clock caught up
        last_inverter_check = last_poll = float("-inf")

        while self._running:
            now = time.monotonic()

            # Back off polling when Telegram API is unreachable
            # Normal: every command_interval. After failures: up to 60s
            poll_backoff = min(command_interval * (2 ** self._poll_failures), 60)
            if now - last_poll >= poll_backoff:
                last_poll = now
                self._run_step(self.poll_commands)

            if now - last_inverter_check >= inverter_interval:
                last_inverter_check = now
                self._run_step(self.check_inverter)

            time.sleep(1)

    def _run_step(self, step):
        """Run one loop step; an exception is logged instead of killing the bot thread."""
        try:
            step()
        except Exception:
            logger.exception("Telegram bot: %s failed", step.__name__)
        finally:
            try:
                self._save_state()
            except Exception:
                logger.exception("Telegram bot: saving state failed")

    def start(self, inverter_interval=120):
        """Start the bot in a background thread."""
        self._thread = threading.Thread(
            target=self.run, args=(inverter_interval,), daemon=True
        )
        self._thread.start()
        return self._thread

    def stop(self):
        """Stop the bot."""
        self._running = False
