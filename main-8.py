import asyncio
import logging
import os
from datetime import datetime
from collections import deque
from playwright.async_api import async_playwright
from telegram import Bot
from telegram.constants import ParseMode

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8819213316:AAG6I8UiIPPUoIvKce78qp4OcVgcqUcliB8")
CHANNEL_ID     = os.environ.get("CHANNEL_ID", "-1008963213073")  # ex: -1001234567890
SCRAPE_INTERVAL = 10          # secondes entre chaque scrape
HISTORY_SIZE    = 50          # nombre de multiplicateurs gardés en mémoire
MIN_ODDS_TARGET = 2.0         # multiplicateur cible pour les signaux
SIGNAL_COOLDOWN = 3           # envoyer un signal max toutes les X lectures

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── HISTORIQUE ───────────────────────────────────────────────────────────────
history: deque[float] = deque(maxlen=HISTORY_SIZE)
last_seen_values: set[str] = set()
signal_counter = 0


# ─── SCRAPING ─────────────────────────────────────────────────────────────────
async def scrape_multipliers(page) -> list[float]:
    """
    Scrape les multiplicateurs depuis la page Lucky Jet de 1win.
    Les sélecteurs peuvent changer — à adapter si 1win met à jour son UI.
    """
    try:
        await page.wait_for_selector(".history-item, .jet-history__item, [class*='history']", timeout=8000)

        # Essai de plusieurs sélecteurs possibles
        selectors = [
            ".history-item .multiplier",
            ".jet-history__item",
            "[class*='history'] [class*='multiplier']",
            "[class*='coefficient']",
        ]

        elements = []
        for selector in selectors:
            elements = await page.query_selector_all(selector)
            if elements:
                break

        values = []
        for el in elements[:20]:  # prendre les 20 derniers
            text = await el.inner_text()
            text = text.strip().replace("x", "").replace("×", "").replace(",", ".")
            try:
                val = float(text)
                if 1.0 <= val <= 1000.0:
                    values.append(val)
            except ValueError:
                continue

        return values

    except Exception as e:
        log.warning(f"Erreur scraping: {e}")
        return []


# ─── ANALYSE STATISTIQUE ──────────────────────────────────────────────────────
def analyze(hist: list[float]) -> dict:
    if not hist:
        return {}

    count = len(hist)
    avg = sum(hist) / count
    above_2 = sum(1 for x in hist if x >= 2.0) / count * 100
    above_15 = sum(1 for x in hist if x >= 1.5) / count * 100

    # Streak actuel (rounds consécutifs sous 2.0)
    streak_under_2 = 0
    for x in reversed(hist):
        if x < 2.0:
            streak_under_2 += 1
        else:
            break

    # Streak consécutifs au-dessus de 2.0
    streak_above_2 = 0
    for x in reversed(hist):
        if x >= 2.0:
            streak_above_2 += 1
        else:
            break

    # Dernier multiplicateur
    last = hist[-1] if hist else 0

    return {
        "count": count,
        "avg": round(avg, 2),
        "above_2_pct": round(above_2, 1),
        "above_15_pct": round(above_15, 1),
        "streak_under_2": streak_under_2,
        "streak_above_2": streak_above_2,
        "last": last,
        "min": round(min(hist), 2),
        "max": round(max(hist), 2),
    }


# ─── GÉNÉRATION DU SIGNAL ─────────────────────────────────────────────────────
def generate_signal(stats: dict) -> dict | None:
    """
    Logique de signal basée sur les statistiques historiques.
    Retourne un dict signal ou None si pas de signal.
    """
    if stats.get("count", 0) < 10:
        return None

    streak_under = stats.get("streak_under_2", 0)
    above_2_pct  = stats.get("above_2_pct", 0)
    avg          = stats.get("avg", 0)

    # Signal FORT : 4+ rounds consécutifs sous ×2 ET historique favorable
    if streak_under >= 4 and above_2_pct >= 40:
        return {
            "type": "🟢 SIGNAL FORT",
            "target": "×2.00",
            "advice": "Mise conseillée — cible ×2.0",
            "confidence": "Élevée",
            "reason": f"{streak_under} rounds consécutifs sous ×2 | {above_2_pct}% de rounds ×2+ sur l'historique"
        }

    # Signal MODÉRÉ : 3 rounds sous ×2
    elif streak_under >= 3 and above_2_pct >= 35:
        return {
            "type": "🟡 SIGNAL MODÉRÉ",
            "target": "×1.70",
            "advice": "Mise légère — cible ×1.7",
            "confidence": "Moyenne",
            "reason": f"{streak_under} rounds consécutifs sous ×2 | Moyenne historique: ×{avg}"
        }

    # Signal PRUDENCE : trop de gros multiplicateurs récents
    elif stats.get("streak_above_2", 0) >= 3:
        return {
            "type": "🔴 PRUDENCE",
            "target": "—",
            "advice": "Pause recommandée — refroidissement en cours",
            "confidence": "Faible",
            "reason": f"{stats['streak_above_2']} gros rounds consécutifs — risque de correction"
        }

    return None


# ─── MESSAGE TELEGRAM ─────────────────────────────────────────────────────────
def format_message(stats: dict, signal: dict | None, new_values: list[float]) -> str:
    now = datetime.now().strftime("%H:%M:%S")

    # Historique récent (5 derniers)
    recent = [f"×{v}" for v in list(history)[-5:]]
    recent_str = " → ".join(recent) if recent else "—"

    msg = f"""🚀 *Lucky Jet — Mise à jour* `{now}`

📊 *Statistiques ({stats.get('count', 0)} rounds)*
├ Moyenne : `×{stats.get('avg', '—')}`
├ ×2.0+ : `{stats.get('above_2_pct', '—')}%` des rounds
├ ×1.5+ : `{stats.get('above_15_pct', '—')}%` des rounds
├ Min / Max : `×{stats.get('min', '—')}` / `×{stats.get('max', '—')}`
└ Dernier : `×{stats.get('last', '—')}`

🕐 *5 derniers rounds*
{recent_str}
"""

    if signal:
        msg += f"""
━━━━━━━━━━━━━━━━━
{signal['type']}
🎯 Cible : `{signal['target']}`
💬 {signal['advice']}
📈 Confiance : {signal['confidence']}
📝 _{signal['reason']}_
━━━━━━━━━━━━━━━━━"""
    else:
        msg += "\n⏳ *Pas de signal clair — attendre*"

    msg += "\n\n⚠️ _Outil statistique uniquement — jouer responsablement_"
    return msg


# ─── BOUCLE PRINCIPALE ────────────────────────────────────────────────────────
async def main():
    global signal_counter

    bot = Bot(token=TELEGRAM_TOKEN)
    log.info("Bot démarré ✅")

    async with async_playwright() as p:
        # Lancer Chromium headless (stealth mode basique)
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
        )

        page = await context.new_page()

        log.info("Ouverture de 1win Lucky Jet...")
        try:
            await page.goto(
                "https://1win.com/fr/casino/games/lucky-jet",
                wait_until="domcontentloaded",
                timeout=30000
            )
        except Exception as e:
            log.error(f"Impossible d'ouvrir 1win: {e}")
            log.info("Passage en mode simulation pour test...")

        log.info("Scraping démarré — intervalle: 10s")

        while True:
            try:
                # 1. Scraper les multiplicateurs
                values = await scrape_multipliers(page)

                # Filtrer les nouvelles valeurs (éviter doublons)
                new_values = []
                for v in values:
                    key = f"{v}"
                    if key not in last_seen_values:
                        history.append(v)
                        new_values.append(v)

                # Mettre à jour le set des valeurs vues
                last_seen_values.clear()
                last_seen_values.update(f"{v}" for v in values)

                if new_values:
                    log.info(f"Nouveaux multiplicateurs: {new_values}")

                # 2. Analyser
                hist_list = list(history)
                stats = analyze(hist_list)

                if not stats:
                    await asyncio.sleep(SCRAPE_INTERVAL)
                    continue

                # 3. Générer signal
                signal = generate_signal(stats)

                # 4. Envoyer message (toutes les SIGNAL_COOLDOWN itérations)
                signal_counter += 1
                if signal_counter >= SIGNAL_COOLDOWN:
                    signal_counter = 0
                    msg = format_message(stats, signal, new_values)
                    await bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=msg,
                        parse_mode=ParseMode.MARKDOWN
                    )
                    log.info("Message envoyé ✅")

            except Exception as e:
                log.error(f"Erreur boucle principale: {e}")

            await asyncio.sleep(SCRAPE_INTERVAL)


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(main())
