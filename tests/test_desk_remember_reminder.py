"""Keep me signed in on this device, Telegram linking, and the morning Kite login reminder."""

import copy
import sys
from datetime import datetime

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import tradingagents.dataflows.config as config_module
from desk import market
from desk.auth import (
    REMEMBER_COOKIE,
    ROTATE_GRACE,
    Auth,
    RememberedDevices,
    device_name,
    hash_password,
)
from desk.kite import KiteError
from desk.kitereminder import KiteReminder, parse_nse_holidays
from desk.notify import Notifier, TelegramLink
from desk.server import create_app
from desk.settings import Settings

pytestmark = pytest.mark.unit

FAKE = {"command": sys.executable, "args": ["-m", "desk.dev.fake_kite_mcp"]}
PASSWORD = "correct horse battery"
CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
CHROME_NEXT = CHROME.replace("131.0.0.0", "132.0.6834.83")
PHONE = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
IST = market.IST


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "desk"))
    monkeypatch.delenv("DESK_PUBLIC_URL", raising=False)
    saved = copy.deepcopy(config_module._config)
    config_module.set_config({"default_exchange": "NSE"})
    market.set_holidays([])
    yield
    config_module._config = saved
    market.set_holidays([])


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# -- remembered devices --------------------------------------------------------------

def test_a_remembered_device_rotates_its_secret_and_an_old_copy_signs_it_out(tmp_path):
    clock, events = Clock(), []
    store = RememberedDevices(tmp_path / "r.json", "hash-1", wall=clock, on_event=lambda t, b: events.append(t))
    first = store.add(CHROME, "1.2.3.4")
    device_id = first.split(".")[0]
    used = store.use(first, CHROME, "1.2.3.4")
    assert used and used[0] == device_id and used[1] != first           # a new secret every use
    second = used[1]
    clock.t += 10
    assert store.use(first, CHROME, "1.2.3.4") == (device_id, "")         # a request already in flight
    assert store.use(second, CHROME_NEXT, "1.2.3.4")                      # a browser update is the same browser
    clock.t += ROTATE_GRACE + 1
    assert store.use(first, CHROME, "6.6.6.6") is None                    # an old copy: stolen
    assert store.count() == 0 and events == ["Remembered device signed out"]
    assert first.split(".")[1] not in (tmp_path / "r.json").read_text()   # only a hash is stored


def test_a_remembered_device_works_only_in_its_own_browser_and_for_30_days(tmp_path):
    clock = Clock()
    store = RememberedDevices(tmp_path / "r.json", "hash-1", wall=clock)
    cookie = store.add(CHROME, "1.2.3.4")
    assert store.use(cookie, PHONE, "1.2.3.4") is None and store.count() == 0
    cookie = store.add(CHROME, "1.2.3.4")
    clock.t += 30 * 86400 + 1
    assert store.use(cookie, CHROME, "1.2.3.4") is None and store.count() == 0
    assert store.use("garbage", CHROME, "x") is None and store.use(None, CHROME, "x") is None


def test_changing_the_desk_password_forgets_every_device(tmp_path):
    store = RememberedDevices(tmp_path / "r.json", "hash-1")
    cookie = store.add(CHROME, "1.2.3.4")
    assert RememberedDevices(tmp_path / "r.json", "hash-1").use(cookie, CHROME, "1.2.3.4")
    assert RememberedDevices(tmp_path / "r.json", "hash-2").count() == 0


def test_device_names_are_plain():
    assert device_name(CHROME) == "Chrome on Windows" and device_name(PHONE) == "Chrome on Android"
    assert device_name("") == "Browser on an unknown system"


@pytest.fixture
def hosted(tmp_path):
    from fastapi.testclient import TestClient

    settings = Settings(tmp_path / "settings.json")
    settings.update({"mcp": {"mcpServers": {"kite": FAKE}}})
    app = create_app(settings=settings, fetch=lambda s, interval="1d": None,
                     auth=Auth("admin", hash_password(PASSWORD)), allowed_hosts=["desk.example.com"])
    with TestClient(app, base_url="https://desk.example.com", follow_redirects=False,
                    headers={"User-Agent": CHROME}) as client:
        yield client
    app.state.kite.disconnect()


def token_of(client) -> dict:
    page = client.get("/")
    assert page.status_code == 200, page.headers.get("location")
    return {"X-Desk-Token": page.text.split('name="desk-token" content="')[1].split('"')[0]}


def test_keep_me_signed_in_survives_the_session_and_can_be_removed(hosted):
    plain = hosted.post("/api/login", json={"username": "admin", "password": PASSWORD})
    assert REMEMBER_COOKIE not in plain.headers.get("set-cookie", "")       # opt-in only
    hosted.cookies.clear()
    response = hosted.post("/api/login", json={"username": "admin", "password": PASSWORD, "remember": True})
    device = [c for c in response.headers.get_list("set-cookie") if c.startswith(REMEMBER_COOKIE)][0].lower()
    assert "httponly" in device and "secure" in device and "samesite=lax" in device and "max-age=2592000" in device

    hosted.cookies.delete("desk_session")                                  # the 12-hour session ended
    assert hosted.get("/api/state").status_code == 401                     # an API call never uses the device cookie
    headers = token_of(hosted)                                             # a page load does, and rotates it
    assert hosted.get("/api/state", headers=headers).status_code == 200
    listed = hosted.get("/api/devices", headers=headers).json()
    assert listed["available"] and len(listed["devices"]) == 1
    row = listed["devices"][0]
    assert row["this_device"] and row["name"] == "Chrome on Windows"

    removed = hosted.delete(f"/api/devices/{row['id']}", headers=headers)
    assert removed.json()["devices"] == [] and REMEMBER_COOKIE in removed.headers["set-cookie"]
    hosted.cookies.clear()
    assert hosted.get("/").headers["location"] == "/login"


def test_signing_out_forgets_this_device(hosted):
    hosted.post("/api/login", json={"username": "admin", "password": PASSWORD, "remember": True})
    headers = token_of(hosted)
    assert hosted.post("/api/logout", headers=headers).status_code == 200
    assert hosted.app.state.remembered.count() == 0
    assert hosted.get("/").status_code == 303


def test_a_stolen_device_cookie_in_another_browser_does_not_sign_in(hosted):
    hosted.post("/api/login", json={"username": "admin", "password": PASSWORD, "remember": True})
    stolen = hosted.cookies.get(REMEMBER_COOKIE)
    hosted.cookies.clear()
    hosted.cookies.set(REMEMBER_COOKIE, stolen, domain="desk.example.com")
    page = hosted.get("/", headers={"User-Agent": PHONE})
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert hosted.app.state.remembered.count() == 0


def test_the_reminder_link_signs_in_then_goes_to_kite(hosted):
    assert hosted.get("/broker/login").headers["location"] == "/login?next=/broker/login"
    hosted.post("/api/login", json={"username": "admin", "password": PASSWORD})
    assert hosted.get("/login?next=/broker/login").headers["location"] == "/broker/login"
    assert hosted.get("/login?next=https://evil.example").headers["location"] == "/"
    page = hosted.get("/broker/login")                       # MCP Kite, not connected: says what to press
    assert page.status_code == 400 and "Connect on the Trade tab" in page.text


def test_the_reminder_link_opens_zerodha_with_direct_kite_connect(hosted):
    hosted.app.state.settings.update({"kite_connect": {"api_key": "abcd1234", "api_secret": "s3cret99"}})
    hosted.post("/api/login", json={"username": "admin", "password": PASSWORD})
    page = hosted.get("/broker/login")
    assert page.status_code == 303 and page.headers["location"].startswith("https://kite.zerodha.com/connect/login?")
    assert "abcd1234" in page.headers["location"]


# -- Telegram linking ------------------------------------------------------------------

def test_linking_telegram_finds_the_chat_from_the_start_message():
    sent = {}

    def call(token, method, params):
        if method == "getMe":
            return {"username": "my_desk_bot"}
        sent["offset"] = params.get("offset")
        code = sent["code"]
        return [{"update_id": 7, "message": {"text": "hello", "chat": {"id": 1}}},
                {"update_id": 8, "message": {"text": f"/start {code}", "chat": {"id": 987654321, "first_name": "Ravi"}}}]

    link = TelegramLink(call=call)
    with pytest.raises(ValueError):
        link.begin("")
    started = link.begin("tok")
    assert started["url"] == f"https://t.me/my_desk_bot?start={started['code']}"
    sent["code"] = "something-else"
    assert link.check("tok") == {"linked": False}
    sent["code"] = started["code"]
    assert link.check("tok") == {"linked": True, "chat_id": "987654321", "name": "Ravi"}
    assert sent["offset"] == 9
    with pytest.raises(ValueError):
        link.check("tok")                                     # used up


# -- the morning reminder -----------------------------------------------------------------

class FakeDirect:
    def __init__(self):
        self.signed_in = False


class FakeKite:
    is_direct, connected = True, True

    def __init__(self):
        self.direct = FakeDirect()


class FakeMcpKite:
    is_direct = False

    def __init__(self, connected=True, error=None):
        self.connected, self.error = connected, error

    def profile(self):
        if self.error:
            raise self.error
        return {"user_id": "AB1234"}


def reminder_setup(tmp_path, kite=None, nse=None, chat="123456789"):
    settings = Settings(tmp_path / "settings.json")
    settings.update({"api_keys": {"TELEGRAM_BOT_TOKEN": "123:abc"}, "telegram_chat_id": chat})
    messages = []
    moment = {"now": datetime(2026, 11, 25, 8, 29, tzinfo=IST)}           # a Wednesday
    notifier = Notifier(settings, path=tmp_path / "alerts.jsonl", send=lambda t, c, text: messages.append((c, text)),
                        now=lambda: moment["now"])
    reminder = KiteReminder(settings, kite or FakeKite(), notifier, nse=nse, now=lambda: moment["now"],
                            path=tmp_path / "rem.json", link=lambda: "https://desk.example.com/broker/login",
                            background=False)
    return settings, reminder, messages, moment


def test_the_reminder_goes_once_at_8_30_with_a_login_link(tmp_path):
    settings, reminder, messages, moment = reminder_setup(tmp_path)
    assert settings.get("kite_reminder") and settings.get("alerts_enabled") is False   # on by default; alerts off
    reminder.tick()
    assert messages == []
    moment["now"] = moment["now"].replace(minute=30)
    reminder.tick()
    reminder.tick()
    assert len(messages) == 1
    chat, text = messages[0]
    assert chat == "123456789" and "Log in to Kite" in text and "https://desk.example.com/broker/login" in text
    assert reminder.status()["last_outcome"] == "sent"
    # A restart the same morning does not send it again.
    _, again, more, moment2 = reminder_setup(tmp_path)
    moment2["now"] = moment["now"]
    again.tick()
    assert more == []


def test_no_reminder_when_already_logged_in_on_weekends_or_holidays(tmp_path):
    kite = FakeKite()
    kite.direct.signed_in = True
    _, reminder, messages, moment = reminder_setup(tmp_path, kite=kite)
    moment["now"] = moment["now"].replace(minute=31)
    reminder.tick()
    assert messages == [] and reminder.status()["last_outcome"] == "already logged in to Kite"

    (tmp_path / "w").mkdir()
    _, reminder, messages, moment = reminder_setup(tmp_path / "w")
    moment["now"] = datetime(2026, 11, 28, 8, 35, tzinfo=IST)            # Saturday
    reminder.tick()
    assert messages == [] and reminder.status()["last_outcome"] == "not a trading day"

    (tmp_path / "h").mkdir()
    settings, reminder, messages, moment = reminder_setup(tmp_path / "h")
    settings.update({"holidays": ["2026-11-25"]})
    moment["now"] = moment["now"].replace(minute=40)
    reminder.tick()
    assert messages == [] and reminder.status()["last_outcome"] == "not a trading day"


def test_nse_holidays_count_even_when_settings_miss_them(tmp_path):
    class Nse:
        def json(self, path, params):
            assert path == "holiday-master" and params == {"type": "trading"}
            return {"CM": [{"tradingDate": "25-Nov-2026", "description": "Guru Nanak Jayanti"},
                           {"tradingDate": "25-Dec-2026"}], "FO": []}

    _, reminder, messages, moment = reminder_setup(tmp_path, nse=Nse())
    moment["now"] = moment["now"].replace(minute=30)
    reminder.tick()
    assert messages == [] and reminder.status()["nse_holidays"] == 2
    assert parse_nse_holidays({"CM": [{"tradingDate": "bad"}]}) == [] and parse_nse_holidays(None) == []


def test_nse_being_unreachable_falls_back_to_the_settings_list(tmp_path):
    class Down:
        def json(self, path, params):
            raise RuntimeError("403 from NSE")

    _, reminder, messages, moment = reminder_setup(tmp_path, nse=Down())
    moment["now"] = moment["now"].replace(minute=30)
    reminder.tick()
    assert len(messages) == 1 and "403" in reminder.status()["nse_error"]


def test_the_reminder_with_a_kite_mcp_server(tmp_path):
    _, reminder, messages, moment = reminder_setup(tmp_path, kite=FakeMcpKite(error=KiteError("down")))
    moment["now"] = moment["now"].replace(minute=30)
    reminder.tick()
    assert messages == [] and reminder.status()["last_day"] is None      # Kite could not say: ask again
    reminder.kite = FakeMcpKite(error=KiteError("token expired", needs_login=True))
    reminder.tick()
    assert len(messages) == 1


def test_no_chat_linked_or_switched_off_or_desk_started_late(tmp_path):
    _, reminder, messages, moment = reminder_setup(tmp_path, chat="")
    moment["now"] = moment["now"].replace(minute=30)
    reminder.tick()
    assert messages == [] and reminder.status()["last_outcome"] == "no Telegram chat linked"

    (tmp_path / "off").mkdir()
    settings, reminder, messages, moment = reminder_setup(tmp_path / "off")
    settings.update({"kite_reminder": False})
    moment["now"] = moment["now"].replace(minute=30)
    reminder.tick()
    assert messages == []

    (tmp_path / "late").mkdir()
    _, reminder, messages, moment = reminder_setup(tmp_path / "late")
    moment["now"] = moment["now"].replace(hour=10)
    reminder.tick()
    assert messages == [] and reminder.status()["last_outcome"].startswith("missed")


def test_the_reminder_time_is_validated(tmp_path):
    settings = Settings(tmp_path / "settings.json")
    settings.update({"kite_reminder_time": "07:45"})
    for bad in ("05:30", "16:00", "8:30", "noon"):
        with pytest.raises(ValueError):
            settings.update({"kite_reminder_time": bad})


def test_the_reminder_api(hosted):
    hosted.post("/api/login", json={"username": "admin", "password": PASSWORD})
    headers = token_of(hosted)
    status = hosted.get("/api/kite/reminder", headers=headers).json()
    assert status["enabled"] and status["time"] == "08:30" and not status["telegram"] and status["link"]
    assert hosted.app.state.reminder.link_base() == "https://desk.example.com"


def test_no_kite_reminder_when_the_desk_uses_another_broker(tmp_path):
    settings, reminder, messages, moment = reminder_setup(tmp_path)
    real = settings.all
    settings.all = lambda: {**real(), "broker": "upstox"}
    moment["now"] = moment["now"].replace(minute=30)
    reminder.tick()
    assert messages == [] and reminder.status()["last_outcome"] == "the desk uses another broker"
