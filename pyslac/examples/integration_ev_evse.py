"""
Integration example: Run EV and EVSE SLAC sessions against each other.

This script is intended for real hardware or a Linux veth pair.
It requires root privileges and two network interfaces.

Usage
-----
Create a veth pair:
    $ sudo ip link add slac_ev0 type veth peer name slac_evse0
    $ sudo ip link set slac_ev0 up
    $ sudo ip link set slac_evse0 up

Run the integration:
    $ sudo python pyslac/examples/integration_ev_evse.py
    # or with custom interface names:
    $ sudo python pyslac/examples/integration_ev_evse.py slac_ev0 slac_evse0

The script runs the full SLAC matching protocol on both sides concurrently,
then prints the final state of each session.
"""
import asyncio
import logging
import sys
from os import urandom

from pyslac.environment import Config
from pyslac.session import SlacEvseSession
from pyslac.session_ev import SlacEvSession
from pyslac.enums import STATE_MATCHED
from pyslac.utils import generate_nid

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("integration_example")

TIMEOUT = 30  # seconds


async def run(iface_ev: str, iface_evse: str) -> None:
    config = Config(slac_init_timeout=20, slac_atten_results_timeout=None)

    evse_session = SlacEvseSession(
        evse_id="DE*12*122333", iface=iface_evse, config=config
    )
    ev_session = SlacEvSession(iface=iface_ev)

    # Pre-load NMK/NID on the EVSE so it can include them in CM_SLAC_MATCH.CNF
    nmk = urandom(16)
    evse_session.nmk = nmk
    evse_session.nid = generate_nid(nmk)

    logger.info(
        "EVSE interface : %s  MAC: %s",
        iface_evse,
        evse_session.evse_mac.hex(":"),
    )
    logger.info(
        "EV   interface : %s  MAC: %s",
        iface_ev,
        ev_session.pev_mac.hex(":"),
    )

    async def evse_run():
        await evse_session.evse_slac_parm()
        await evse_session.atten_charac_routine()

    logger.info("Starting SLAC matching...")
    try:
        await asyncio.wait_for(
            asyncio.gather(evse_run(), ev_session.ev_match_routine()),
            timeout=TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error("SLAC matching timed out after %s seconds", TIMEOUT)
    except Exception as exc:
        logger.exception("SLAC matching failed: %s", exc)
    finally:
        logger.info("EVSE state : %s", evse_session.state)
        logger.info("EV   state : %s", ev_session.state)
        if evse_session.state == ev_session.state == STATE_MATCHED:
            logger.info("SUCCESS: Both sides reached MATCHED state.")
        else:
            logger.error(
                "FAILURE: One or both sides did not reach MATCHED state."
            )


if __name__ == "__main__":
    _iface_ev = sys.argv[1] if len(sys.argv) > 1 else "slac_ev0"
    _iface_evse = sys.argv[2] if len(sys.argv) > 2 else "slac_evse0"
    asyncio.run(run(_iface_ev, _iface_evse))
