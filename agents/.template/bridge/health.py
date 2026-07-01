"""Minimal in-process polling watchdog.

launchd KeepAlive owns crash-restart. This watchdog only catches network blips:
it periodically probes the Telegram API and signals a polling restart when the
API has been unreachable past a threshold, without a full process restart.
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

WATCHDOG_INTERVAL = 60  # seconds between probes
NETWORK_FAILURE_THRESHOLD = 300  # consecutive down seconds before forced restart
RECOVERY_NOTIFY_THRESHOLD = 2  # min consecutive failures before a recovery is worth notifying (skip single-probe blips)


class PollingRestart(Exception):
    """Signal the run loop to restart polling after a network outage."""


class PollingConflict(Exception):
    """Signal the run loop to restart polling after a getUpdates Conflict.

    Telegram allows only one getUpdates consumer per bot token. PTB's internal
    network retry loop swallows Conflict and retries forever, leaving the bot
    alive but receiving zero updates (a zombie). We surface it as a distinct
    signal so the run loop can back off and cleanly re-initialize polling,
    without counting toward the rapid-crash SystemExit threshold.
    """


async def polling_watchdog(application, stop_event: asyncio.Event, on_recovery=None) -> None:
    """Probe get_me() periodically; raise PollingRestart on prolonged outage.

    on_recovery: optional async callable(down_seconds:int). Invoked once when the
    API becomes reachable again after an outage of at least RECOVERY_NOTIFY_THRESHOLD
    probes, so the bot can tell the user it was offline (DGN-045). Single-probe
    blips are not reported. Failures here never break the watchdog loop.
    """
    consecutive_failures = 0
    while not stop_event.is_set():
        await asyncio.sleep(WATCHDOG_INTERVAL)
        updater = application.updater if application else None
        if not application or not updater or not updater.running:
            continue
        try:
            await asyncio.wait_for(application.bot.get_me(), timeout=10)
            if consecutive_failures > 0:
                logger.info("Telegram API reachable again after %d failure(s)", consecutive_failures)
                if on_recovery is not None and consecutive_failures >= RECOVERY_NOTIFY_THRESHOLD:
                    try:
                        await on_recovery(consecutive_failures * WATCHDOG_INTERVAL)
                    except Exception as e:
                        logger.error("Outage recovery notify failed: %s", e)
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            total_down = consecutive_failures * WATCHDOG_INTERVAL
            logger.warning("Telegram API unreachable (%ds): %s", total_down, e)
            if total_down >= NETWORK_FAILURE_THRESHOLD:
                logger.warning("Network down for %ds, restarting polling", total_down)
                try:
                    await asyncio.wait_for(updater.stop(), timeout=15)
                except asyncio.TimeoutError:
                    logger.error("updater.stop() timed out, forcing process exit")
                    os._exit(1)
                raise PollingRestart()
