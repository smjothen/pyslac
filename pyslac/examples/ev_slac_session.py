from pyslac.utils import is_distro_linux

if not is_distro_linux():
    raise EnvironmentError("Non-Linux systems are not supported")

import asyncio
import logging

from pyslac.environment import Config
from pyslac.session_ev import SlacEvSession, SlacEvSessionController

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__file__)


class EvSlacHandler(SlacEvSessionController):
    async def notify_matching_ongoing(self):
        """Overrides the notify_matching_ongoing method defined in
        SlacEvSessionController."""
        logger.info("EV SLAC matching is ongoing")

    async def notify_matching_failed(self):
        """Overrides the notify_matching_failed method defined in
        SlacEvSessionController."""
        logger.error("EV SLAC matching has failed after all retries")

    async def notify_matching_succeeded(self):
        """Overrides the notify_matching_succeeded method defined in
        SlacEvSessionController."""
        logger.info("EV SLAC matching succeeded and logical network is joined")


async def main(iface: str = "eth0"):
    slac_config = Config()
    slac_config.load_envs()

    ev_session = SlacEvSession(iface=iface, config=slac_config)

    try:
        logger.info("Initialising EV PLC chip...")
        await ev_session.ev_set_key()
    except (OSError, TimeoutError, ValueError) as e:
        logger.error(
            f"EV PLC chip initialisation failed on interface {iface}: {e}. "
            f"Please check your settings."
        )
        return

    ev_slac_handler = EvSlacHandler()
    await ev_slac_handler.start_matching(ev_session)


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
