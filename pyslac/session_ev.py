import asyncio
import logging
from binascii import hexlify
from inspect import isawaitable
from os import urandom
from typing import Union

from pyslac.enums import (
    BROADCAST_ADDR,
    CM_ATTEN_CHAR,
    CM_SET_KEY,
    CM_SLAC_MATCH,
    CM_SLAC_PARM,
    CM_START_ATTEN_CHAR,
    CM_MNBC_SOUND,
    ETH_TYPE_HPAV,
    EV_PLC_MAC,
    HOMEPLUG_MMV,
    MMTYPE_CNF,
    MMTYPE_IND,
    MMTYPE_REQ,
    MMTYPE_RSP,
    SLAC_GROUPS,
    SLAC_LIMIT,
    SLAC_MSOUNDS,
    SLAC_PAUSE,
    SLAC_SETTLE_TIME,
    STATE_MATCHED,
    STATE_MATCHING,
    STATE_UNMATCHED,
    FramesSizes,
    Timers,
)
from pyslac.environment import Config
from pyslac.layer_2_headers import EthernetHeader, HomePlugHeader
from pyslac.messages import (
    AtennChar,
    AtennCharRsp,
    MatchCnf,
    MatchReq,
    MnbcSound,
    SetKeyCnf,
    SetKeyReq,
    SlacParmCnf,
    SlacParmReq,
    StartAtennChar,
)
from pyslac import __version__
from pyslac.session import SlacSession
from pyslac.sockets.async_linux_socket import (
    create_socket,
    readeth,
    sendeth,
)
from pyslac.utils import generate_nid, get_if_hwaddr

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("slac_ev_session")


class SlacEvSession(SlacSession):
    # pylint: disable=too-many-instance-attributes, too-many-arguments
    # pylint: disable=logging-fstring-interpolation, broad-except
    def __init__(self, iface: str, config: Config):
        self.iface = iface
        self.config = config
        host_mac = get_if_hwaddr(self.iface)
        logger.debug(f"EV Session created on interface {self.iface}")
        self.socket = create_socket(iface=self.iface, port=0)
        self.ev_plc_mac = EV_PLC_MAC
        SlacSession.__init__(self, state=STATE_UNMATCHED, pev_mac=host_mac)

    def reset_socket(self):
        self.socket.close()
        self.socket = create_socket(iface=self.iface, port=0)

    async def send_frame(self, frame_to_send: bytes) -> None:
        """
        Async wrapper for sendeth that checks if sendeth is an awaitable
        """
        bytes_sent = sendeth(
            s=self.socket, frame_to_send=frame_to_send, iface=self.iface
        )
        if isawaitable(bytes_sent):
            await bytes_sent

    async def rcv_frame(self, rcv_frame_size: int, timeout: Union[float, int]) -> bytes:
        """
        Helper function to reduce lines of code when calling asyncio.wait_for
        with readeth

        :param rcv_frame_size: size of the frame to be received
        :param timeout: timeout for the specific message that is being expected
        :return:
        """
        return await asyncio.wait_for(
            readeth(self.socket, self.iface, rcv_frame_size),
            timeout,
        )

    async def ev_set_key(self) -> bytes:
        """
        EV-HLE sets a fresh NMK and NID on EV-PLC using CM_SET_KEY.REQ to
        initialise or reset the PLC chip before starting the matching process.

        Table A.8 from ISO15118-3 defines all the parameters needed.
        """
        logger.info("EV CM_SET_KEY: Started...")
        nmk = urandom(16)
        nid = generate_nid(nmk)
        logger.debug("New NMK: %s", hexlify(nmk))
        logger.debug("New NID: %s", hexlify(nid))
        ethernet_header = EthernetHeader(
            dst_mac=self.ev_plc_mac, src_mac=self.pev_mac
        )
        homeplug_header = HomePlugHeader(CM_SET_KEY | MMTYPE_REQ)
        key_req_payload = SetKeyReq(nid=nid, new_key=nmk)

        frame_to_send = (
            ethernet_header.pack_big()
            + homeplug_header.pack_big()
            + key_req_payload.pack_big()
        )

        try:
            await self.send_frame(frame_to_send)
            data_rcvd = await self.rcv_frame(
                rcv_frame_size=FramesSizes.CM_SET_KEY_CNF,
                timeout=Timers.SLAC_INIT_TIMEOUT,
            )
        except asyncio.TimeoutError as e:
            raise TimeoutError("EV SetKey Timeout raised") from e
        try:
            SetKeyCnf.from_bytes(data_rcvd)
            self.nmk = nmk
            self.nid = nid
        except ValueError as e:
            logger.error(e)
            if self.nmk and self.nid:
                logger.debug(
                    "EV SetKeyReq has failed, old NMK: %s and NID: %s apply",
                    self.nmk,
                    self.nid,
                )
            else:
                raise ValueError(
                    "EV SetKeyCnf data parsing into the class failed"
                ) from e
        logger.debug("Registering NMK and NID into the EV PLC node...")
        await asyncio.sleep(SLAC_SETTLE_TIME)
        logger.info("EV CM_SET_KEY: Finished!")
        return data_rcvd

    async def ev_slac_parm(self) -> None:
        """
        EV broadcasts CM_SLAC_PARM.REQ and awaits CM_SLAC_PARM.CNF from
        the EVSE. A random run_id is generated for the session.
        """
        logger.info("EV CM_SLAC_PARM: Started...")
        self.reset_socket()
        self.run_id = urandom(8)
        logger.debug("Generated run_id: %s", hexlify(self.run_id))

        ethernet_header = EthernetHeader(
            dst_mac=BROADCAST_ADDR, src_mac=self.pev_mac
        )
        homeplug_header = HomePlugHeader(CM_SLAC_PARM | MMTYPE_REQ)
        slac_parm_req = SlacParmReq(run_id=self.run_id)

        frame_to_send = (
            ethernet_header.pack_big()
            + homeplug_header.pack_big()
            + slac_parm_req.pack_big()
        )

        await self.send_frame(frame_to_send)
        logger.debug("Sent CM_SLAC_PARM.REQ")

        while True:
            try:
                data_rcvd = await self.rcv_frame(
                    rcv_frame_size=FramesSizes.CM_SLAC_PARM_CNF,
                    timeout=Timers.SLAC_INIT_TIMEOUT,
                )
            except asyncio.TimeoutError as e:
                logger.warning("Timeout waiting for CM_SLAC_PARM.CNF: %s", e)
                raise e
            try:
                ether_frame = EthernetHeader.from_bytes(data_rcvd)
                homeplug_frame = HomePlugHeader.from_bytes(data_rcvd)
                if homeplug_frame.mm_type != CM_SLAC_PARM | MMTYPE_CNF:
                    logger.warning("Frame received is not CM_SLAC_PARM.CNF")
                    logger.debug("Continue waiting for CM_SLAC_PARM.CNF...")
                    continue
                slac_parm_cnf = SlacParmCnf.from_bytes(data_rcvd)
            except Exception as e:
                logger.exception(e, exc_info=True)
                raise e

            if slac_parm_cnf.run_id != self.run_id:
                logger.warning(
                    "CM_SLAC_PARM.CNF run_id mismatch: expected %s, got %s",
                    hexlify(self.run_id),
                    hexlify(slac_parm_cnf.run_id),
                )
                continue
            break

        # Save EVSE parameters from the CNF
        self.evse_mac = ether_frame.src_mac
        self.sounds = slac_parm_cnf.num_sounds
        self.time_out_ms = slac_parm_cnf.time_out * 100
        self.forwarding_sta = slac_parm_cnf.forwarding_sta

        self.state = STATE_MATCHING
        logger.info("EV CM_SLAC_PARM: Finished!")

    async def ev_start_atten_charac(self) -> None:
        """
        EV sends 3 consecutive CM_START_ATTEN_CHAR.IND broadcast messages
        as required by ISO15118-3.
        """
        logger.info("EV CM_START_ATTEN_CHAR: Started...")
        ethernet_header = EthernetHeader(
            dst_mac=BROADCAST_ADDR, src_mac=self.pev_mac
        )
        homeplug_header = HomePlugHeader(CM_START_ATTEN_CHAR | MMTYPE_IND)
        start_atten_char = StartAtennChar(
            num_sounds=self.sounds,
            time_out=self.time_out_ms // 100,
            forwarding_sta=self.pev_mac,
            run_id=self.run_id,
        )

        frame_to_send = (
            ethernet_header.pack_big()
            + homeplug_header.pack_big()
            + start_atten_char.pack_big()
        )

        # ISO15118-3 requires the EV to send 3 consecutive CM_START_ATTEN_CHAR.IND
        for i in range(3):
            await self.send_frame(frame_to_send)
            logger.debug("Sent CM_START_ATTEN_CHAR.IND #%d", i + 1)
            await asyncio.sleep(SLAC_PAUSE)

        logger.info("EV CM_START_ATTEN_CHAR: Finished!")

    async def ev_mnbc_sound(self) -> None:
        """
        EV sends num_sounds CM_MNBC_SOUND.IND broadcast messages with a
        countdown counter and a random value per sound.
        """
        logger.info("EV CM_MNBC_SOUND: Started...")
        ethernet_header = EthernetHeader(
            dst_mac=BROADCAST_ADDR, src_mac=self.pev_mac
        )
        homeplug_header = HomePlugHeader(CM_MNBC_SOUND | MMTYPE_IND)

        for cnt in range(self.sounds - 1, -1, -1):
            rnd = int.from_bytes(urandom(16), "big")
            mnbc_sound = MnbcSound(
                cnt=cnt,
                run_id=self.run_id,
                rnd=rnd,
            )
            frame_to_send = (
                ethernet_header.pack_big()
                + homeplug_header.pack_big()
                + mnbc_sound.pack_big()
            )
            await self.send_frame(frame_to_send)
            logger.debug("Sent CM_MNBC_SOUND.IND, cnt=%d", cnt)
            await asyncio.sleep(SLAC_PAUSE)

        logger.info("EV CM_MNBC_SOUND: Finished!")

    async def ev_atten_char(self) -> None:
        """
        After sending all sounds, EV waits for CM_ATTEN_CHAR.IND from the
        EVSE (timer TT_EV_atten_results = max 1200 ms), validates it, and
        sends CM_ATTEN_CHAR.RSP back.

        The average attenuation is checked against SLAC_LIMIT to confirm a
        match candidate.
        """
        logger.info("EV CM_ATTEN_CHAR: Started...")
        while True:
            try:
                data_rcvd = await self.rcv_frame(
                    rcv_frame_size=FramesSizes.CM_ATTEN_CHAR_IND,
                    timeout=Timers.SLAC_ATTEN_RESULTS_TIMEOUT,
                )
                logger.debug("Payload Received: \n %s", hexlify(data_rcvd))
                ether_frame = EthernetHeader.from_bytes(data_rcvd)
                homeplug_frame = HomePlugHeader.from_bytes(data_rcvd)
                if homeplug_frame.mm_type != CM_ATTEN_CHAR | MMTYPE_IND:
                    logger.warning("Frame received is not CM_ATTEN_CHAR.IND")
                    logger.debug("Continue waiting for CM_ATTEN_CHAR.IND...")
                    continue
                atten_char = AtennChar.from_bytes(data_rcvd)
            except asyncio.TimeoutError as e:
                raise TimeoutError("EV CM_ATTEN_CHAR timeout") from e
            except Exception as e:
                logger.exception(e, exc_info=True)
                raise e

            if (
                ether_frame.ether_type != ETH_TYPE_HPAV
                or homeplug_frame.mmv != HOMEPLUG_MMV
                or atten_char.run_id != self.run_id
                or atten_char.application_type != self.application_type
                or atten_char.security_type != self.security_type
            ):
                logger.warning(
                    "CM_ATTEN_CHAR.IND validation failed, ignoring frame"
                )
                continue
            break

        # Learn the EVSE MAC from the Ethernet source address
        self.evse_mac = ether_frame.src_mac

        # Compute average attenuation across all groups
        avg_atten = 0
        if atten_char.num_groups > 0:
            avg_atten = sum(atten_char.aag) // atten_char.num_groups

        logger.debug(
            "Average attenuation: %d (limit: %d)", avg_atten, self.slac_threshold
        )
        if avg_atten > self.slac_threshold:
            raise ValueError(
                f"Average attenuation {avg_atten} exceeds threshold "
                f"{self.slac_threshold}: not a match candidate"
            )

        # Send CM_ATTEN_CHAR.RSP back to the EVSE
        ether_header = EthernetHeader(dst_mac=self.evse_mac, src_mac=self.pev_mac)
        homeplug_header = HomePlugHeader(CM_ATTEN_CHAR | MMTYPE_RSP)
        atten_char_rsp = AtennCharRsp(
            source_address=self.pev_mac,
            run_id=self.run_id,
            source_id=0x00,
            resp_id=0x00,
            result=0x00,
        )

        frame_to_send = (
            ether_header.pack_big()
            + homeplug_header.pack_big()
            + atten_char_rsp.pack_big()
        )

        await self.send_frame(frame_to_send)
        logger.debug("Sent CM_ATTEN_CHAR.RSP")
        logger.info("EV CM_ATTEN_CHAR: Finished!")

    async def ev_slac_match(self) -> None:
        """
        EV sends CM_SLAC_MATCH.REQ to the EVSE (unicast) and awaits
        CM_SLAC_MATCH.CNF. The NID and NMK from the CNF are used to join
        the EVSE's logical network.
        """
        logger.info("EV CM_SLAC_MATCH: Started...")
        ether_header = EthernetHeader(dst_mac=self.evse_mac, src_mac=self.pev_mac)
        homeplug_header = HomePlugHeader(CM_SLAC_MATCH | MMTYPE_REQ)
        match_req = MatchReq(
            pev_mac=self.pev_mac,
            evse_mac=self.evse_mac,
            run_id=self.run_id,
        )

        frame_to_send = (
            ether_header.pack_big()
            + homeplug_header.pack_big()
            + match_req.pack_big()
        )

        await self.send_frame(frame_to_send)
        logger.debug("Sent CM_SLAC_MATCH.REQ")

        while True:
            try:
                data_rcvd = await self.rcv_frame(
                    rcv_frame_size=FramesSizes.CM_SLAC_MATCH_CNF,
                    timeout=Timers.SLAC_MATCH_TIMEOUT,
                )
                logger.debug("Payload Received: \n %s", hexlify(data_rcvd))
                homeplug_frame = HomePlugHeader.from_bytes(data_rcvd)
                if homeplug_frame.mm_type != CM_SLAC_MATCH | MMTYPE_CNF:
                    logger.warning("Frame received is not CM_SLAC_MATCH.CNF")
                    logger.debug("Continue waiting for CM_SLAC_MATCH.CNF...")
                    continue
                match_cnf = MatchCnf.from_bytes(data_rcvd)
            except Exception as e:
                logger.exception(e, exc_info=True)
                raise ValueError("EV SLAC Match Failed") from e

            if match_cnf.run_id != self.run_id:
                logger.warning(
                    "CM_SLAC_MATCH.CNF run_id mismatch: expected %s, got %s",
                    hexlify(self.run_id),
                    hexlify(match_cnf.run_id),
                )
                raise ValueError("EV SLAC Match CNF run_id mismatch")
            break

        # Store NMK and NID from the EVSE's logical network
        self.nmk = match_cnf.nmk
        self.nid = match_cnf.nid
        logger.debug("Received NMK: %s", hexlify(self.nmk))
        logger.debug("Received NID: %s", hexlify(self.nid))
        self.state = STATE_MATCHED
        logger.info("EV CM_SLAC_MATCH: Finished!")

    async def ev_join_network(self) -> bytes:
        """
        After receiving CM_SLAC_MATCH.CNF, configure the EV's PLC chip with
        the NMK and NID received from the EVSE so the EV joins the EVSE's
        logical network.
        """
        logger.info("EV Join Network: Started...")
        ethernet_header = EthernetHeader(
            dst_mac=self.ev_plc_mac, src_mac=self.pev_mac
        )
        homeplug_header = HomePlugHeader(CM_SET_KEY | MMTYPE_REQ)
        key_req_payload = SetKeyReq(nid=self.nid, new_key=self.nmk)

        frame_to_send = (
            ethernet_header.pack_big()
            + homeplug_header.pack_big()
            + key_req_payload.pack_big()
        )

        try:
            await self.send_frame(frame_to_send)
            data_rcvd = await self.rcv_frame(
                rcv_frame_size=FramesSizes.CM_SET_KEY_CNF,
                timeout=Timers.SLAC_INIT_TIMEOUT,
            )
        except asyncio.TimeoutError as e:
            raise TimeoutError("EV Join Network SetKey Timeout raised") from e
        try:
            SetKeyCnf.from_bytes(data_rcvd)
        except ValueError as e:
            logger.error("EV Join Network SetKeyCnf parsing failed: %s", e)
            raise ValueError(
                "EV Join Network: SetKeyCnf data parsing failed"
            ) from e

        logger.debug("EV PLC chip configured with EVSE network credentials.")
        await asyncio.sleep(SLAC_SETTLE_TIME)
        logger.info("EV Join Network: Finished!")
        return data_rcvd

    async def matching_routine(self) -> None:
        """
        Orchestrates the full EV-side SLAC matching sequence:
        ev_slac_parm -> ev_start_atten_charac -> ev_mnbc_sound ->
        ev_atten_char -> ev_slac_match -> ev_join_network
        """
        logger.info("EV Matching Routine: Started...")
        await self.ev_slac_parm()
        await self.ev_start_atten_charac()
        await self.ev_mnbc_sound()
        await self.ev_atten_char()
        await self.ev_slac_match()
        await self.ev_join_network()
        logger.info("EV Matching Routine: Finished! State: MATCHED")


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
