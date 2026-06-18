"""
Weather service for BeachManager.
Uses Open-Meteo (https://open-meteo.com) — free, no API key required.
Results are cached in-process for 30 minutes.
All wind speeds are in km/h.
"""

import os
import time
import logging
import urllib.request
import json
import concurrent.futures as _cf

logger = logging.getLogger(__name__)

# Location — configure via env vars or default to Odessa, Ukraine
LATITUDE  = float(os.getenv("BEACH_LAT", "46.4825"))
LONGITUDE = float(os.getenv("BEACH_LON", "30.7233"))
BEACH_NAME = os.getenv("BEACH_NAME", "Одесса, Украина")

# Alert thresholds (km/h)
WIND_YELLOW_KMH  = float(os.getenv("WIND_YELLOW_KMH", "20"))
WIND_RED_KMH     = float(os.getenv("WIND_RED_KMH",    "35"))
GUST_YELLOW_KMH  = float(os.getenv("GUST_YELLOW_KMH", "30"))
GUST_RED_KMH     = float(os.getenv("GUST_RED_KMH",    "45"))
RAIN_WARN_PCT    = float(os.getenv("RAIN_WARN_PCT",   "50"))

CACHE_TTL = 30 * 60  # 30 minutes

_cache: dict = {"data": None, "ts": 0.0}

# WMO weather code → readable label
WMO_LABELS = {
    0:  "Ясно",
    1:  "Преимущественно ясно",
    2:  "Переменная облачность",
    3:  "Пасмурно",
    45: "Туман",
    48: "Изморозь",
    51: "Лёгкая морось",
    53: "Морось",
    55: "Сильная морось",
    61: "Небольшой дождь",
    63: "Дождь",
    65: "Сильный дождь",
    71: "Небольшой снег",
    73: "Снег",
    75: "Сильный снег",
    77: "Снежная крупа",
    80: "Ливень",
    81: "Сильный ливень",
    82: "Очень сильный ливень",
    85: "Снежный ливень",
    86: "Сильный снежный ливень",
    95: "Гроза",
    96: "Гроза с градом",
    99: "Сильная гроза с градом",
}


def weather_emoji(code) -> str:
    if code is None:
        return "🌡️"
    c = int(code)
    if c == 0:
        return "☀️"
    if c <= 3:
        return "⛅"
    if c <= 48:
        return "🌫️"
    if c <= 67:
        return "🌧️"
    if c <= 77:
        return "❄️"
    if c <= 82:
        return "🌦️"
    return "⛈️"


def weather_label(code) -> str:
    if code is None:
        return "—"
    return WMO_LABELS.get(int(code), f"Код {code}")


def danger_level(wind_kmh: float | None, gusts_kmh: float | None) -> tuple[str, str, str]:
    """
    Returns (level, emoji, recommendation):
      level = "green" | "yellow" | "red"
    """
    w = wind_kmh or 0
    g = gusts_kmh or 0

    if w >= WIND_RED_KMH or g >= GUST_RED_KMH:
        return (
            "red",
            "🔴 Красный уровень опасности",
            "🚨 Усилить крепления, предупредить всю команду.",
        )
    if w >= WIND_YELLOW_KMH or g >= GUST_YELLOW_KMH:
        return (
            "yellow",
            "🟡 Жёлтый уровень опасности",
            "⚠️ Проверить крепление зонтов.",
        )
    return (
        "green",
        "🟢 Зелёный уровень",
        "",
    )


def _fetch_from_api() -> dict | None:
    params = "&".join([
        f"latitude={LATITUDE}",
        f"longitude={LONGITUDE}",
        "current=temperature_2m,wind_speed_10m,wind_gusts_10m,precipitation,weather_code",
        "daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
        "wind_speed_10m_max,wind_gusts_10m_max,weather_code",
        "wind_speed_unit=kmh",
        "timezone=Europe%2FKyiv",
        "forecast_days=3",
    ])
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            raw = json.loads(resp.read().decode())
    except Exception as exc:
        logger.warning("Weather API request failed: %s", exc)
        return None

    cur   = raw.get("current", {})
    daily = raw.get("daily", {})

    def nth(key, n, default=None):
        lst = daily.get(key, [])
        return lst[n] if len(lst) > n else default

    data = {
        "location": BEACH_NAME,
        "current": {
            "temp":        cur.get("temperature_2m"),
            "wind":        cur.get("wind_speed_10m"),
            "wind_gusts":  cur.get("wind_gusts_10m"),
            "precip":      cur.get("precipitation"),
            "code":        cur.get("weather_code"),
        },
        "today": {
            "temp_max":       nth("temperature_2m_max",            0),
            "temp_min":       nth("temperature_2m_min",            0),
            "rain_pct":       nth("precipitation_probability_max", 0),
            "wind_max":       nth("wind_speed_10m_max",            0),
            "wind_gusts_max": nth("wind_gusts_10m_max",            0),
            "code":           nth("weather_code",                  0),
        },
        "tomorrow": {
            "temp_max":       nth("temperature_2m_max",            1),
            "temp_min":       nth("temperature_2m_min",            1),
            "rain_pct":       nth("precipitation_probability_max", 1),
            "wind_max":       nth("wind_speed_10m_max",            1),
            "wind_gusts_max": nth("wind_gusts_10m_max",            1),
            "code":           nth("weather_code",                  1),
        },
        "alerts": [],
    }

    # Build alerts for today
    tod = data["today"]
    wind_now  = tod["wind_max"]   or 0
    gust_now  = tod["wind_gusts_max"] or 0
    rain_now  = tod["rain_pct"]   or 0

    level, _, _ = danger_level(wind_now, gust_now)

    if level in ("yellow", "red"):
        if wind_now >= WIND_RED_KMH or gust_now >= GUST_RED_KMH:
            data["alerts"].append({
                "type": "wind_red",
                "emoji": "🔴",
                "text": (
                    f"Сильный ветер {wind_now:.0f} км/ч, порывы {gust_now:.0f} км/ч. "
                    f"Усилить крепления и предупредить команду."
                ),
            })
        else:
            data["alerts"].append({
                "type": "wind_yellow",
                "emoji": "🟡",
                "text": (
                    f"Ветер {wind_now:.0f} км/ч, порывы {gust_now:.0f} км/ч. "
                    f"Проверить крепление зонтов."
                ),
            })

    if rain_now >= RAIN_WARN_PCT:
        data["alerts"].append({
            "type": "rain",
            "emoji": "☔",
            "text": (
                f"Вероятность дождя {rain_now:.0f}%. "
                f"Подготовьте защитные чехлы и оборудование."
            ),
        })

    logger.info(
        "Weather fetched: now=%.1f°C wind=%.1f km/h gusts=%.1f km/h | today max=%.1f°C alerts=%d",
        cur.get("temperature_2m", 0),
        cur.get("wind_speed_10m", 0),
        cur.get("wind_gusts_10m", 0),
        tod["temp_max"] or 0,
        len(data["alerts"]),
    )
    return data


def _fetch_with_hard_timeout(timeout_sec: int = 12) -> dict | None:
    """
    Run _fetch_from_api in a worker thread with a hard wall-clock timeout.
    This covers DNS hangs that `urllib.request` timeout= does NOT protect against.
    Safe to call from both sync and async contexts.
    """
    with _cf.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_fetch_from_api)
        try:
            return future.result(timeout=timeout_sec)
        except _cf.TimeoutError:
            logger.warning(
                "Weather fetch hard-timeout after %ds (DNS or network hung)", timeout_sec
            )
            return None
        except Exception as exc:
            logger.warning("Weather fetch executor error: %s", exc)
            return None


def get_weather() -> dict | None:
    """Return cached weather data, refreshing if older than 30 minutes."""
    global _cache
    now = time.monotonic()
    if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    fresh = _fetch_with_hard_timeout(timeout_sec=12)
    if fresh is not None:
        _cache = {"data": fresh, "ts": now}
        return fresh

    if _cache["data"] is not None:
        logger.warning("Using stale weather cache")
        return _cache["data"]

    return None


def format_weather_short(day: dict) -> str:
    """One-line weather string for bot messages."""
    emoji    = weather_emoji(day.get("code"))
    temp_max = day.get("temp_max")
    wind_max = day.get("wind_max")
    rain_pct = day.get("rain_pct")

    temp_str = f"+{temp_max:.0f}°C" if temp_max is not None else "—"
    wind_str = f"{wind_max:.0f} км/ч" if wind_max is not None else "—"
    rain_str = f"{rain_pct:.0f}%" if rain_pct is not None else "—"

    return f"{emoji} {temp_str} | 💨 {wind_str} | 🌧 {rain_str}"


def format_weather_block(day: dict, title: str = "Погода") -> str:
    """Multi-line weather block for bot messages."""
    emoji    = weather_emoji(day.get("code"))
    label    = weather_label(day.get("code"))
    temp_max = day.get("temp_max")
    temp_min = day.get("temp_min")
    wind_max = day.get("wind_max")
    gusts    = day.get("wind_gusts_max")
    rain_pct = day.get("rain_pct")

    temp_str = (
        f"+{temp_max:.0f}°C / +{temp_min:.0f}°C"
        if temp_max is not None and temp_min is not None
        else "—"
    )
    wind_str  = f"{wind_max:.0f} км/ч" if wind_max is not None else "—"
    gusts_str = f"{gusts:.0f} км/ч"   if gusts   is not None else "—"
    rain_str  = f"{rain_pct:.0f}%"    if rain_pct is not None else "—"

    return (
        f"🌦 *{title}:*\n"
        f"   {emoji} {label}\n"
        f"   🌡 Температура: *{temp_str}*\n"
        f"   💨 Ветер: *{wind_str}* | 🌪 Порывы: *{gusts_str}*\n"
        f"   🌧 Дождь: *{rain_str}*"
    )


def format_weather_full(w: dict) -> str:
    """Full weather card for the bot /weather view."""
    cur  = w.get("current", {})
    tod  = w.get("today", {})

    cond_emoji = weather_emoji(cur.get("code"))
    cond_label = weather_label(cur.get("code"))

    temp_now   = cur.get("temp")
    wind_now   = cur.get("wind")
    gusts_now  = cur.get("wind_gusts")
    temp_max   = tod.get("temp_max")
    temp_min   = tod.get("temp_min")
    rain_pct   = tod.get("rain_pct")
    wind_max   = tod.get("wind_max")
    gusts_max  = tod.get("wind_gusts_max")

    def _t(v): return f"+{v:.0f}°C" if v is not None else "—"
    def _w(v): return f"{v:.0f} км/ч" if v is not None else "—"
    def _p(v): return f"{v:.0f}%" if v is not None else "—"

    lv, lv_label, lv_rec = danger_level(wind_max, gusts_max)

    lines = [
        f"🌦 *Погода на пляже*",
        f"📍 {w.get('location', '')}",
        "",
        f"{cond_emoji} *{cond_label}*",
        f"🌡 Сейчас: *{_t(temp_now)}*  |  макс *{_t(temp_max)}*  мин *{_t(temp_min)}*",
        f"💨 Ветер: *{_w(wind_now)}*  (макс за день *{_w(wind_max)}*)",
        f"🌪 Порывы: *{_w(gusts_now)}*  (макс *{_w(gusts_max)}*)",
        f"🌧 Вероятность дождя: *{_p(rain_pct)}*",
        "",
        lv_label,
    ]
    if lv_rec:
        lines.append(lv_rec)

    return "\n".join(lines)
