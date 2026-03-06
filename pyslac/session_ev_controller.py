import asyncio
import logging

from pyslac import __version__
from pyslac.enums import STATE_MATCHED, STATE_UNMATCHED
from pyslac.session_ev import SlacEvSession

logger = logging.getLogger("slac_session")


class SlacEvSessionController:
    def __init__(self):
        logger.info(
            f"\n\n#################################################"
            f"\n ###### Starting PySlac version: {__version__} #######"
            f"\n#################################################\n"
        )

    async def notify_matching_ongoing(self):
        """
        Used to Notify an external service that an EV Matching process is ongoing
        """
        pass

    async def notify_matching_failed(self):
        """
        Used to Notify an external service that an EV Matching process has failed
        """
        pass

    async def notify_matching_succeeded(self):
        """
        Used to Notify an external service that EV SLAC matching succeeded and
        the logical network is joined
        """
        pass

    async def start_matching(
        self, slac_session: SlacEvSession, number_of_retries=3
    ) -> None:
        """
        Runs the EV SLAC matching routine with retries.
        Calls matching_routine() which internally runs:
        ev_slac_parm -> ev_start_atten_charac -> ev_mnbc_sound ->
        ev_atten_char -> ev_slac_match -> ev_join_network

        :param slac_session: Instance of SlacEvSession
        :param number_of_retries: number of trials before SLAC Matching is defined
        as a failure
        :return: None
        """
        while number_of_retries > 0:
            number_of_retries -= 1
            slac_session.state = STATE_UNMATCHED
            await self.notify_matching_ongoing()
            try:
                await slac_session.matching_routine()
            except Exception as e:
                slac_session.state = STATE_UNMATCHED
                logger.debug(
                    f"Exception occurred during EV matching routine: "
                    f"{e} \n"
                    f"Number of retries left: {number_of_retries}"
                )
                if number_of_retries > 0:
                    logger.warning("EV SLAC Matching Failed; Retrying..")
                else:
                    logger.error(
                        "EV SLAC Matching Failed: No more retries possible"
                    )
                    await self.notify_matching_failed()
                continue

            if slac_session.state == STATE_MATCHED:
                logger.info("EV-EVSE MATCHED Successfully, Logical Network Joined.")
                await self.notify_matching_succeeded()
                while True:
                    await asyncio.sleep(2.0)

        logger.debug("EV SLAC Protocol Concluded...")
