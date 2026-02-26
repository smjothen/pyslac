import asyncio
import logging
from inspect import isawaitable
from os import urandom
from typing import Union

from pyslac.enums import (
    BROADCAST_ADDR,
    BUFF_MAX_SIZE,
    CM_ATTEN_CHAR,
    CM_MNBC_SOUND,
    CM_SLAC_MATCH,
    CM_SLAC_PARM,
    CM_START_ATTEN_CHAR,
    EVSE_PLC_MAC,
    MMTYPE_CNF,
    MMTYPE_IND,
    MMTYPE_REQ,
    MMTYPE_RSP,
    SLAC_PAUSE,
    STATE_MATCHED,
    STATE_MATCHING,
    STATE_UNMATCHED,
    FramesSizes,
    Timers,
)
from pyslac.layer_2_headers import EthernetHeader, HomePlugHeader
from pyslac.messages import (
    AtennCharRsp,
    AtennChar,
    MatchCnf,
    MatchReq,
    MnbcSound,
    SlacParmCnf,
    SlacParmReq,
    StartAtennChar,
)
from pyslac.session import SlacSession
from pyslac.sockets.async_linux_socket import (
    create_socket,
    readeth,
    sendeth,
)
from pyslac.utils import get_if_hwaddr

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("slac_ev_session")


class SlacEvSession(SlacSession):
    # pylint: disable=too-many-instance-attributes
    # pylint: disable=logging-fstring-interpolation, broad-except
    def __init__(self, iface: str):
        self.iface = iface
        host_mac = get_if_hwaddr(self.iface)
        logger.debug(f"EV Session created on interface {self.iface}")
        self.socket = create_socket(iface=self.iface, port=0)
        # The EV uses the same well-known PLC MAC constant as the EVSE when
        # addressing the local PLC chip for key-setting operations.
        self.plc_mac = EVSE_PLC_MAC
        SlacSession.__init__(self, state=STATE_UNMATCHED, pev_mac=host_mac)

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
        Helper to call asyncio.wait_for with readeth

        :param rcv_frame_size: size of the frame to be received
        :param timeout: timeout for the specific message that is being expected
        :return: received bytes
        """
        return await asyncio.wait_for(
            readeth(self.socket, self.iface, rcv_frame_size),
            timeout,
        )

    async def ev_slac_parm(self) -> None:
        """
        EV sends CM_SLAC_PARM.REQ (broadcast) with a newly generated RunID,
        then waits for CM_SLAC_PARM.CNF from the EVSE.

        On success, sets self.evse_mac and self.state = STATE_MATCHING.
        """
        logger.debug("CM_SLAC_PARM: Started...")
        self.run_id = urandom(8)

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
                    rcv_frame_size=FramesSizes.CM_SLAC_PARM_REQ,
                    timeout=Timers.SLAC_ATTEN_RESULTS_TIMEOUT,
                )
                ether_frame = EthernetHeader.from_bytes(data_rcvd)
                homeplug_frame = HomePlugHeader.from_bytes(data_rcvd)
                if homeplug_frame.mm_type != CM_SLAC_PARM | MMTYPE_CNF:
                    logger.debug(
                        "Frame received is not CM_SLAC_PARM.CNF, continuing..."
                    )
                    continue
                slac_parm_cnf = SlacParmCnf.from_bytes(data_rcvd)
            except Exception as e:
                logger.exception(e, exc_info=True)
                raise e

            if slac_parm_cnf.run_id != self.run_id:
                logger.warning(
                    "CM_SLAC_PARM.CNF run_id mismatch, ignoring..."
                )
                continue
            break

        self.evse_mac = ether_frame.src_mac
        self.num_expected_sounds = slac_parm_cnf.num_sounds
        self.state = STATE_MATCHING
        logger.debug("Received CM_SLAC_PARM.CNF")
        logger.debug("CM_SLAC_PARM: Finished!")

    async def ev_start_atten_char(self) -> None:
        """
        EV sends 3 consecutive CM_START_ATTEN_CHAR.IND frames (broadcast).
        Per ISO15118-3, exactly 3 are sent regardless of acknowledgement.
        """
        logger.debug("CM_START_ATTEN_CHAR: Started...")
        ethernet_header = EthernetHeader(
            dst_mac=BROADCAST_ADDR, src_mac=self.pev_mac
        )
        homeplug_header = HomePlugHeader(CM_START_ATTEN_CHAR | MMTYPE_IND)
        start_atten_char = StartAtennChar(
            num_sounds=self.sounds,
            time_out=self.time_out_ms,
            forwarding_sta=self.pev_mac,
            run_id=self.run_id,
        )

        frame_to_send = (
            ethernet_header.pack_big()
            + homeplug_header.pack_big()
            + start_atten_char.pack_big()
        )

        for _ in range(3):
            await self.send_frame(frame_to_send)
            await asyncio.sleep(SLAC_PAUSE)

        logger.debug("CM_START_ATTEN_CHAR: Finished!")

    async def ev_mnbc_sound(self) -> None:
        """
        EV sends self.sounds CM_MNBC_SOUND.IND frames (broadcast) with a
        brief pause between each, as required by the spec.
        """
        logger.debug("CM_MNBC_SOUND: Started...")
        ethernet_header = EthernetHeader(
            dst_mac=BROADCAST_ADDR, src_mac=self.pev_mac
        )
        homeplug_header = HomePlugHeader(CM_MNBC_SOUND | MMTYPE_IND)

        for cnt in range(self.sounds - 1, -1, -1):
            mnbc_sound = MnbcSound(cnt=cnt, run_id=self.run_id)
            frame_to_send = (
                ethernet_header.pack_big()
                + homeplug_header.pack_big()
                + mnbc_sound.pack_big()
            )
            await self.send_frame(frame_to_send)
            await asyncio.sleep(self.pause)

        logger.debug(f"CM_MNBC_SOUND: Finished! Sent {self.sounds} sounds")

    async def ev_atten_char(self) -> None:
        """
        EV waits for CM_ATTEN_CHAR.IND from EVSE, evaluates attenuation,
        and responds with CM_ATTEN_CHAR.RSP.

        Sets self.state = STATE_UNMATCHED if attenuation check fails.
        """
        logger.debug("CM_ATTEN_CHAR: Started...")
        while True:
            try:
                data_rcvd = await self.rcv_frame(
                    rcv_frame_size=BUFF_MAX_SIZE,
                    timeout=Timers.SLAC_ATTEN_RESULTS_TIMEOUT,
                )
                homeplug_frame = HomePlugHeader.from_bytes(data_rcvd)
                if homeplug_frame.mm_type != CM_ATTEN_CHAR | MMTYPE_IND:
                    logger.debug(
                        "Frame received is not CM_ATTEN_CHAR.IND, continuing..."
                    )
                    continue
                atten_char = AtennChar.from_bytes(data_rcvd)
            except Exception as e:
                logger.exception(e, exc_info=True)
                raise e

            if atten_char.run_id != self.run_id:
                logger.warning(
                    "CM_ATTEN_CHAR.IND run_id mismatch, ignoring..."
                )
                continue
            break

        # EV evaluates attenuation: result=0x00 indicates match (below threshold)
        # TODO: implement per-group threshold check against self.slac_threshold
        result = 0x00

        ether_header = EthernetHeader(
            dst_mac=self.evse_mac, src_mac=self.pev_mac
        )
        homeplug_header = HomePlugHeader(CM_ATTEN_CHAR | MMTYPE_RSP)
        atten_char_rsp = AtennCharRsp(
            source_address=self.pev_mac,
            run_id=self.run_id,
            source_id=0x00,
            resp_id=0x00,
            result=result,
        )

        frame_to_send = (
            ether_header.pack_big()
            + homeplug_header.pack_big()
            + atten_char_rsp.pack_big()
        )
        await self.send_frame(frame_to_send)
        logger.debug("Sent CM_ATTEN_CHAR.RSP")

        if result != 0x00:
            self.state = STATE_UNMATCHED
            raise ValueError(
                "Attenuation above threshold; SLAC matching failed"
            )

        logger.debug("CM_ATTEN_CHAR: Finished!")

    async def ev_slac_match(self) -> None:
        """
        EV sends CM_SLAC_MATCH.REQ (unicast to EVSE) and waits for
        CM_SLAC_MATCH.CNF. On success, stores NMK and NID and sets
        self.state = STATE_MATCHED.
        """
        logger.debug("CM_SLAC_MATCH: Started...")
        ether_header = EthernetHeader(
            dst_mac=self.evse_mac, src_mac=self.pev_mac
        )
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
                    rcv_frame_size=BUFF_MAX_SIZE,
                    timeout=Timers.SLAC_MATCH_TIMEOUT,
                )
                homeplug_frame = HomePlugHeader.from_bytes(data_rcvd)
                if homeplug_frame.mm_type != CM_SLAC_MATCH | MMTYPE_CNF:
                    logger.debug(
                        "Frame received is not CM_SLAC_MATCH.CNF, continuing..."
                    )
                    continue
                match_cnf = MatchCnf.from_bytes(data_rcvd)
            except Exception as e:
                logger.exception(e, exc_info=True)
                raise e

            if match_cnf.run_id != self.run_id:
                logger.warning(
                    "CM_SLAC_MATCH.CNF run_id mismatch, ignoring..."
                )
                continue
            break

        self.nmk = match_cnf.nmk
        self.nid = match_cnf.nid
        self.state = STATE_MATCHED
        logger.debug("Received CM_SLAC_MATCH.CNF")
        logger.debug("CM_SLAC_MATCH: Finished! EV is now MATCHED.")

    async def ev_match_routine(self) -> None:
        """
        Runs the complete EV-side SLAC matching protocol:
          1. CM_SLAC_PARM.REQ / .CNF
          2. CM_START_ATTEN_CHAR.IND  (3×)
          3. CM_MNBC_SOUND.IND        (SLAC_MSOUNDS×)
          4. CM_ATTEN_CHAR.IND / .RSP
          5. CM_SLAC_MATCH.REQ / .CNF
        """
        await self.ev_slac_parm()
        await self.ev_start_atten_char()
        await self.ev_mnbc_sound()
        await self.ev_atten_char()
        await self.ev_slac_match()
