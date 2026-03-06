from pyslac.utils import is_distro_linux

if not is_distro_linux():
    raise EnvironmentError("Non-Linux systems are not supported")

import asyncio
import logging

from pyslac.environment import Config
from pyslac.session_ev import SlacEvSession

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__file__)


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

    try:
        logger.info("Running EV SLAC matching routine...")
        await ev_session.matching_routine()
        logger.info("EV SLAC matching completed successfully: MATCHED")
    except (TimeoutError, ValueError) as e:
        logger.error(f"EV SLAC matching failed: {e}")


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
