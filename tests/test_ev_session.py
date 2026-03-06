from unittest.mock import AsyncMock, Mock, patch

import pytest

from pyslac.enums import (
    BROADCAST_ADDR,
    CM_ATTEN_CHAR,
    CM_SET_CCO_CAPAB,
    CM_SET_KEY,
    CM_SET_KEY_MY_NONCE,
    CM_SET_KEY_PID,
    CM_SET_KEY_PMN,
    CM_SET_KEY_PRN,
    CM_SET_KEY_YOUR_NONCE,
    CM_SLAC_MATCH,
    CM_SLAC_PARM,
    CM_START_ATTEN_CHAR,
    CM_MNBC_SOUND,
    EV_PLC_MAC,
    MMTYPE_CNF,
    MMTYPE_IND,
    MMTYPE_REQ,
    MMTYPE_RSP,
    QUALCOMM_NID,
    QUALCOMM_NMK,
    SLAC_APPLICATION_TYPE,
    SLAC_ATTEN_TIMEOUT,
    SLAC_MSOUNDS,
    SLAC_SECURITY_TYPE,
    STATE_MATCHED,
    STATE_MATCHING,
    STATE_UNMATCHED,
)
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

PEV_MAC = b"\xAA" * 6
EVSE_MAC = b"\xBB" * 6
RUN_ID = b"\xFA" * 8


@pytest.fixture
def dummy_config():
    from pyslac.environment import Config

    return Config(slac_init_timeout=1, slac_atten_results_timeout=None)


@pytest.fixture
def ev_mac():
    return PEV_MAC


@pytest.fixture
def ev_slac_session(dummy_config, ev_mac):
    with patch("pyslac.session_ev.get_if_hwaddr", new=Mock(return_value=ev_mac)):
        with patch("pyslac.session_ev.create_socket", new=Mock()):
            from pyslac.session_ev import SlacEvSession

            ev_session = SlacEvSession("en0", dummy_config)
            ev_session.reset_socket = Mock()

    return ev_session


@pytest.mark.asyncio
async def test_ev_set_key(ev_slac_session, ev_mac):
    """
    Tests the EV SetKey Req/Cnf sequence between the EV host and the PLC chip
    """
    ethernet_header = EthernetHeader(dst_mac=EV_PLC_MAC, src_mac=ev_mac)
    homeplug_header = HomePlugHeader(CM_SET_KEY | MMTYPE_REQ)
    key_req_payload = SetKeyReq(nid=QUALCOMM_NID, new_key=QUALCOMM_NMK)

    key_req_frame = (
        ethernet_header.pack_big()
        + homeplug_header.pack_big()
        + key_req_payload.pack_big()
    )

    homeplug_header = HomePlugHeader(CM_SET_KEY | MMTYPE_CNF)
    key_cnf_payload = SetKeyCnf(
        result=0x00,
        my_nonce=CM_SET_KEY_MY_NONCE,
        your_nonce=CM_SET_KEY_YOUR_NONCE,
        pid=CM_SET_KEY_PID,
        prn=CM_SET_KEY_PRN,
        pmn=CM_SET_KEY_PMN,
        cco_capab=CM_SET_CCO_CAPAB,
    )
    key_cnf_frame = (
        ethernet_header.pack_big()
        + homeplug_header.pack_big()
        + key_cnf_payload.pack_big()
    )

    ev_slac_session.send_frame = AsyncMock()
    with patch("pyslac.session_ev.SLAC_SETTLE_TIME", 0.01):
        with patch(
            "pyslac.session_ev.readeth", new=AsyncMock(return_value=key_cnf_frame)
        ):
            with patch(
                "pyslac.session_ev.urandom", new=Mock(return_value=QUALCOMM_NMK)
            ):
                data_rcvd = await ev_slac_session.ev_set_key()

                assert data_rcvd == key_cnf_frame
                ev_slac_session.send_frame.assert_called_with(key_req_frame)


@pytest.mark.asyncio
async def test_ev_slac_parm(ev_slac_session, ev_mac):
    """
    Tests that the EV broadcasts CM_SLAC_PARM.REQ and correctly processes
    CM_SLAC_PARM.CNF from the EVSE.
    """
    # Build the CNF that the EVSE would send back
    ether_header_cnf = EthernetHeader(dst_mac=ev_mac, src_mac=EVSE_MAC)
    homeplug_header_cnf = HomePlugHeader(CM_SLAC_PARM | MMTYPE_CNF)
    slac_parm_cnf = SlacParmCnf(forwarding_sta=ev_mac, run_id=RUN_ID)
    slac_parm_cnf_frame = (
        ether_header_cnf.pack_big()
        + homeplug_header_cnf.pack_big()
        + slac_parm_cnf.pack_big()
    )

    ev_slac_session.send_frame = AsyncMock()
    with patch(
        "pyslac.session_ev.readeth", new=AsyncMock(return_value=slac_parm_cnf_frame)
    ):
        with patch("pyslac.session_ev.urandom", new=Mock(return_value=RUN_ID)):
            await ev_slac_session.ev_slac_parm()

    assert ev_slac_session.run_id == RUN_ID
    assert ev_slac_session.evse_mac == EVSE_MAC
    assert ev_slac_session.sounds == slac_parm_cnf.num_sounds
    assert ev_slac_session.state == STATE_MATCHING


@pytest.mark.asyncio
async def test_ev_start_atten_charac(ev_slac_session, ev_mac):
    """
    Tests that the EV sends exactly 3 CM_START_ATTEN_CHAR.IND messages.
    """
    ev_slac_session.run_id = RUN_ID
    ev_slac_session.sounds = SLAC_MSOUNDS
    ev_slac_session.time_out_ms = SLAC_ATTEN_TIMEOUT * 100

    ethernet_header = EthernetHeader(dst_mac=BROADCAST_ADDR, src_mac=ev_mac)
    homeplug_header = HomePlugHeader(CM_START_ATTEN_CHAR | MMTYPE_IND)
    start_atten_char = StartAtennChar(
        num_sounds=SLAC_MSOUNDS,
        time_out=SLAC_ATTEN_TIMEOUT,
        forwarding_sta=ev_mac,
        run_id=RUN_ID,
    )
    expected_frame = (
        ethernet_header.pack_big()
        + homeplug_header.pack_big()
        + start_atten_char.pack_big()
    )

    ev_slac_session.send_frame = AsyncMock()
    with patch("pyslac.session_ev.SLAC_PAUSE", 0):
        await ev_slac_session.ev_start_atten_charac()

    assert ev_slac_session.send_frame.call_count == 3
    ev_slac_session.send_frame.assert_called_with(expected_frame)


@pytest.mark.asyncio
async def test_ev_mnbc_sound(ev_slac_session, ev_mac):
    """
    Tests that the EV sends num_sounds CM_MNBC_SOUND.IND messages with a
    correctly decrementing counter.
    """
    ev_slac_session.run_id = RUN_ID
    ev_slac_session.sounds = 3

    ev_slac_session.send_frame = AsyncMock()
    with patch("pyslac.session_ev.SLAC_PAUSE", 0):
        with patch(
            "pyslac.session_ev.urandom",
            new=Mock(return_value=b"\x00" * 16),
        ):
            await ev_slac_session.ev_mnbc_sound()

    assert ev_slac_session.send_frame.call_count == 3
    # Check that the first call has cnt=2 (sounds-1) and last call has cnt=0
    calls = ev_slac_session.send_frame.call_args_list
    # Each call encodes MnbcSound with the given cnt; verify decrement
    first_frame = calls[0][0][0]
    last_frame = calls[2][0][0]
    first_mnbc = MnbcSound.from_bytes(first_frame.ljust(71, b"\x00"))
    last_mnbc = MnbcSound.from_bytes(last_frame.ljust(71, b"\x00"))
    assert first_mnbc.cnt == 2
    assert last_mnbc.cnt == 0


@pytest.mark.asyncio
async def test_ev_atten_char(ev_slac_session, ev_mac):
    """
    Tests that the EV correctly receives CM_ATTEN_CHAR.IND, validates it,
    and sends CM_ATTEN_CHAR.RSP.
    """
    num_sounds = 5
    num_groups = 3
    aag = [10, 20, 15]

    ev_slac_session.run_id = RUN_ID
    ev_slac_session.pev_mac = ev_mac

    # Build CM_ATTEN_CHAR.IND from EVSE
    ether_header_ind = EthernetHeader(dst_mac=ev_mac, src_mac=EVSE_MAC)
    homeplug_header_ind = HomePlugHeader(CM_ATTEN_CHAR | MMTYPE_IND)
    atten_char = AtennChar(
        source_address=ev_mac,
        run_id=RUN_ID,
        num_sounds=num_sounds,
        num_groups=num_groups,
        aag=aag,
    )
    atten_char_frame = (
        ether_header_ind.pack_big()
        + homeplug_header_ind.pack_big()
        + atten_char.pack_big()
    )

    # Expected CM_ATTEN_CHAR.RSP sent by EV to EVSE
    ether_header_rsp = EthernetHeader(dst_mac=EVSE_MAC, src_mac=ev_mac)
    homeplug_header_rsp = HomePlugHeader(CM_ATTEN_CHAR | MMTYPE_RSP)
    atten_char_rsp = AtennCharRsp(
        source_address=ev_mac,
        run_id=RUN_ID,
        source_id=0x00,
        resp_id=0x00,
        result=0x00,
    )
    expected_rsp_frame = (
        ether_header_rsp.pack_big()
        + homeplug_header_rsp.pack_big()
        + atten_char_rsp.pack_big()
    )

    ev_slac_session.send_frame = AsyncMock()
    with patch(
        "pyslac.session_ev.readeth", new=AsyncMock(return_value=atten_char_frame)
    ):
        await ev_slac_session.ev_atten_char()

    assert ev_slac_session.evse_mac == EVSE_MAC
    ev_slac_session.send_frame.assert_called_with(expected_rsp_frame)


@pytest.mark.asyncio
async def test_ev_atten_char_threshold_exceeded(ev_slac_session, ev_mac):
    """
    Tests that ev_atten_char raises ValueError when average attenuation
    exceeds the threshold.
    """
    ev_slac_session.run_id = RUN_ID
    ev_slac_session.pev_mac = ev_mac
    ev_slac_session.slac_threshold = 10  # very low threshold

    num_groups = 3
    aag = [50, 60, 70]  # high attenuation values

    ether_header_ind = EthernetHeader(dst_mac=ev_mac, src_mac=EVSE_MAC)
    homeplug_header_ind = HomePlugHeader(CM_ATTEN_CHAR | MMTYPE_IND)
    atten_char = AtennChar(
        source_address=ev_mac,
        run_id=RUN_ID,
        num_sounds=5,
        num_groups=num_groups,
        aag=aag,
    )
    atten_char_frame = (
        ether_header_ind.pack_big()
        + homeplug_header_ind.pack_big()
        + atten_char.pack_big()
    )

    ev_slac_session.send_frame = AsyncMock()
    with patch(
        "pyslac.session_ev.readeth", new=AsyncMock(return_value=atten_char_frame)
    ):
        with pytest.raises(ValueError):
            await ev_slac_session.ev_atten_char()


@pytest.mark.asyncio
async def test_ev_slac_match(ev_slac_session, ev_mac):
    """
    Tests that the EV sends CM_SLAC_MATCH.REQ and correctly processes
    CM_SLAC_MATCH.CNF, extracting NMK and NID.
    """
    ev_slac_session.run_id = RUN_ID
    ev_slac_session.pev_mac = ev_mac
    ev_slac_session.evse_mac = EVSE_MAC

    # Expected CM_SLAC_MATCH.REQ
    ether_header_req = EthernetHeader(dst_mac=EVSE_MAC, src_mac=ev_mac)
    homeplug_header_req = HomePlugHeader(CM_SLAC_MATCH | MMTYPE_REQ)
    match_req = MatchReq(pev_mac=ev_mac, evse_mac=EVSE_MAC, run_id=RUN_ID)
    expected_req_frame = (
        ether_header_req.pack_big()
        + homeplug_header_req.pack_big()
        + match_req.pack_big()
    )

    # CM_SLAC_MATCH.CNF from EVSE
    ether_header_cnf = EthernetHeader(dst_mac=ev_mac, src_mac=EVSE_MAC)
    homeplug_header_cnf = HomePlugHeader(CM_SLAC_MATCH | MMTYPE_CNF)
    match_cnf = MatchCnf(
        pev_mac=ev_mac,
        evse_mac=EVSE_MAC,
        run_id=RUN_ID,
        nid=QUALCOMM_NID,
        nmk=QUALCOMM_NMK,
    )
    match_cnf_frame = (
        ether_header_cnf.pack_big()
        + homeplug_header_cnf.pack_big()
        + match_cnf.pack_big()
    )

    ev_slac_session.send_frame = AsyncMock()
    with patch(
        "pyslac.session_ev.readeth", new=AsyncMock(return_value=match_cnf_frame)
    ):
        await ev_slac_session.ev_slac_match()

    ev_slac_session.send_frame.assert_called_with(expected_req_frame)
    assert ev_slac_session.nmk == QUALCOMM_NMK
    assert ev_slac_session.nid == QUALCOMM_NID
    assert ev_slac_session.state == STATE_MATCHED


@pytest.mark.asyncio
async def test_ev_join_network(ev_slac_session, ev_mac):
    """
    Tests that ev_join_network sends CM_SET_KEY.REQ with the EVSE's NMK/NID
    to configure the EV PLC chip to join the EVSE's logical network.
    """
    ev_slac_session.pev_mac = ev_mac
    ev_slac_session.nmk = QUALCOMM_NMK
    ev_slac_session.nid = QUALCOMM_NID

    # Expected CM_SET_KEY.REQ frame to the EV PLC chip
    ethernet_header = EthernetHeader(dst_mac=EV_PLC_MAC, src_mac=ev_mac)
    homeplug_header = HomePlugHeader(CM_SET_KEY | MMTYPE_REQ)
    key_req_payload = SetKeyReq(nid=QUALCOMM_NID, new_key=QUALCOMM_NMK)
    expected_req_frame = (
        ethernet_header.pack_big()
        + homeplug_header.pack_big()
        + key_req_payload.pack_big()
    )

    # CM_SET_KEY.CNF from PLC chip
    homeplug_header_cnf = HomePlugHeader(CM_SET_KEY | MMTYPE_CNF)
    key_cnf_payload = SetKeyCnf(
        result=0x00,
        my_nonce=CM_SET_KEY_MY_NONCE,
        your_nonce=CM_SET_KEY_YOUR_NONCE,
        pid=CM_SET_KEY_PID,
        prn=CM_SET_KEY_PRN,
        pmn=CM_SET_KEY_PMN,
        cco_capab=CM_SET_CCO_CAPAB,
    )
    key_cnf_frame = (
        ethernet_header.pack_big()
        + homeplug_header_cnf.pack_big()
        + key_cnf_payload.pack_big()
    )

    ev_slac_session.send_frame = AsyncMock()
    with patch("pyslac.session_ev.SLAC_SETTLE_TIME", 0.01):
        with patch(
            "pyslac.session_ev.readeth", new=AsyncMock(return_value=key_cnf_frame)
        ):
            data_rcvd = await ev_slac_session.ev_join_network()

    ev_slac_session.send_frame.assert_called_with(expected_req_frame)
    assert data_rcvd == key_cnf_frame


@pytest.mark.asyncio
async def test_ev_session_state_transitions(ev_slac_session, ev_mac):
    """
    Tests that the EV session transitions through states correctly:
    UNMATCHED -> MATCHING (after ev_slac_parm) -> MATCHED (after ev_slac_match)
    """
    assert ev_slac_session.state == STATE_UNMATCHED

    # Simulate ev_slac_parm
    ether_header_cnf = EthernetHeader(dst_mac=ev_mac, src_mac=EVSE_MAC)
    homeplug_header_cnf = HomePlugHeader(CM_SLAC_PARM | MMTYPE_CNF)
    slac_parm_cnf = SlacParmCnf(forwarding_sta=ev_mac, run_id=RUN_ID)
    slac_parm_cnf_frame = (
        ether_header_cnf.pack_big()
        + homeplug_header_cnf.pack_big()
        + slac_parm_cnf.pack_big()
    )

    ev_slac_session.send_frame = AsyncMock()
    with patch(
        "pyslac.session_ev.readeth", new=AsyncMock(return_value=slac_parm_cnf_frame)
    ):
        with patch("pyslac.session_ev.urandom", new=Mock(return_value=RUN_ID)):
            await ev_slac_session.ev_slac_parm()

    assert ev_slac_session.state == STATE_MATCHING

    # Simulate ev_slac_match
    ev_slac_session.pev_mac = ev_mac
    ev_slac_session.evse_mac = EVSE_MAC

    ether_header_cnf = EthernetHeader(dst_mac=ev_mac, src_mac=EVSE_MAC)
    homeplug_header_cnf = HomePlugHeader(CM_SLAC_MATCH | MMTYPE_CNF)
    match_cnf = MatchCnf(
        pev_mac=ev_mac,
        evse_mac=EVSE_MAC,
        run_id=RUN_ID,
        nid=QUALCOMM_NID,
        nmk=QUALCOMM_NMK,
    )
    match_cnf_frame = (
        ether_header_cnf.pack_big()
        + homeplug_header_cnf.pack_big()
        + match_cnf.pack_big()
    )

    with patch(
        "pyslac.session_ev.readeth", new=AsyncMock(return_value=match_cnf_frame)
    ):
        await ev_slac_session.ev_slac_match()

    assert ev_slac_session.state == STATE_MATCHED


def test_slac_parm_req_construction(ev_mac):
    """
    Tests that SlacParmReq is correctly constructed for EV-side sending.
    """
    slac_parm_req = SlacParmReq(run_id=RUN_ID)
    assert slac_parm_req.run_id == RUN_ID
    assert slac_parm_req.application_type == SLAC_APPLICATION_TYPE
    assert slac_parm_req.security_type == SLAC_SECURITY_TYPE

    # Verify round-trip via from_bytes
    ethernet_header = EthernetHeader(dst_mac=BROADCAST_ADDR, src_mac=ev_mac)
    homeplug_header = HomePlugHeader(CM_SLAC_PARM | MMTYPE_REQ)
    frame = (
        ethernet_header.pack_big()
        + homeplug_header.pack_big()
        + slac_parm_req.pack_big()
    )
    parsed = SlacParmReq.from_bytes(frame)
    assert parsed.run_id == RUN_ID
    assert parsed.application_type == SLAC_APPLICATION_TYPE
    assert parsed.security_type == SLAC_SECURITY_TYPE


def test_start_atten_char_construction(ev_mac):
    """
    Tests that StartAtennChar is correctly constructed for EV-side sending.
    """
    start_atten_char = StartAtennChar(
        num_sounds=SLAC_MSOUNDS,
        time_out=SLAC_ATTEN_TIMEOUT,
        forwarding_sta=ev_mac,
        run_id=RUN_ID,
    )
    assert start_atten_char.num_sounds == SLAC_MSOUNDS
    assert start_atten_char.time_out == SLAC_ATTEN_TIMEOUT
    assert start_atten_char.forwarding_sta == ev_mac
    assert start_atten_char.run_id == RUN_ID

    # Verify round-trip via from_bytes
    ethernet_header = EthernetHeader(dst_mac=BROADCAST_ADDR, src_mac=ev_mac)
    homeplug_header = HomePlugHeader(CM_START_ATTEN_CHAR | MMTYPE_IND)
    frame = (
        ethernet_header.pack_big()
        + homeplug_header.pack_big()
        + start_atten_char.pack_big()
    )
    parsed = StartAtennChar.from_bytes(frame)
    assert parsed.num_sounds == SLAC_MSOUNDS
    assert parsed.run_id == RUN_ID


def test_mnbc_sound_construction(ev_mac):
    """
    Tests that MnbcSound is correctly constructed for EV-side sending.
    """
    rnd = int.from_bytes(b"\xAB" * 16, "big")
    mnbc_sound = MnbcSound(cnt=9, run_id=RUN_ID, rnd=rnd)
    assert mnbc_sound.cnt == 9
    assert mnbc_sound.run_id == RUN_ID
    assert mnbc_sound.rnd == rnd

    # Verify round-trip via from_bytes
    ethernet_header = EthernetHeader(dst_mac=BROADCAST_ADDR, src_mac=ev_mac)
    homeplug_header = HomePlugHeader(CM_MNBC_SOUND | MMTYPE_IND)
    frame = (
        ethernet_header.pack_big()
        + homeplug_header.pack_big()
        + mnbc_sound.pack_big()
    )
    parsed = MnbcSound.from_bytes(frame)
    assert parsed.cnt == 9
    assert parsed.run_id == RUN_ID


def test_atten_char_rsp_construction(ev_mac):
    """
    Tests that AtennCharRsp is correctly constructed for EV-side sending.
    """
    atten_char_rsp = AtennCharRsp(
        source_address=ev_mac,
        run_id=RUN_ID,
        source_id=0x00,
        resp_id=0x00,
        result=0x00,
    )
    assert atten_char_rsp.source_address == ev_mac
    assert atten_char_rsp.run_id == RUN_ID
    assert atten_char_rsp.result == 0x00

    # Verify round-trip via from_bytes
    ethernet_header = EthernetHeader(dst_mac=EVSE_MAC, src_mac=ev_mac)
    homeplug_header = HomePlugHeader(CM_ATTEN_CHAR | MMTYPE_RSP)
    frame = (
        ethernet_header.pack_big()
        + homeplug_header.pack_big()
        + atten_char_rsp.pack_big()
    )
    parsed = AtennCharRsp.from_bytes(frame)
    assert parsed.source_address == ev_mac
    assert parsed.run_id == RUN_ID
    assert parsed.result == 0x00


def test_match_req_construction(ev_mac):
    """
    Tests that MatchReq is correctly constructed for EV-side sending.
    """
    match_req = MatchReq(pev_mac=ev_mac, evse_mac=EVSE_MAC, run_id=RUN_ID)
    assert match_req.pev_mac == ev_mac
    assert match_req.evse_mac == EVSE_MAC
    assert match_req.run_id == RUN_ID

    # Verify round-trip via from_bytes
    ethernet_header = EthernetHeader(dst_mac=EVSE_MAC, src_mac=ev_mac)
    homeplug_header = HomePlugHeader(CM_SLAC_MATCH | MMTYPE_REQ)
    frame = (
        ethernet_header.pack_big()
        + homeplug_header.pack_big()
        + match_req.pack_big()
    )
    parsed = MatchReq.from_bytes(frame)
    assert parsed.pev_mac == ev_mac
    assert parsed.evse_mac == EVSE_MAC
    assert parsed.run_id == RUN_ID


def test_slac_parm_cnf_parsing(ev_mac):
    """
    Tests that SlacParmCnf is correctly parsed from EVSE response bytes.
    """
    slac_parm_cnf = SlacParmCnf(forwarding_sta=ev_mac, run_id=RUN_ID)
    ethernet_header = EthernetHeader(dst_mac=ev_mac, src_mac=EVSE_MAC)
    homeplug_header = HomePlugHeader(CM_SLAC_PARM | MMTYPE_CNF)
    frame = (
        ethernet_header.pack_big()
        + homeplug_header.pack_big()
        + slac_parm_cnf.pack_big()
    )
    parsed = SlacParmCnf.from_bytes(frame)
    assert parsed.run_id == RUN_ID
    assert parsed.forwarding_sta == ev_mac


def test_atten_char_ind_parsing(ev_mac):
    """
    Tests that AtennChar (CM_ATTEN_CHAR.IND) is correctly parsed from EVSE
    response bytes.
    """
    num_groups = 4
    aag = [10, 20, 30, 40]
    atten_char = AtennChar(
        source_address=ev_mac,
        run_id=RUN_ID,
        num_sounds=5,
        num_groups=num_groups,
        aag=aag,
    )
    ethernet_header = EthernetHeader(dst_mac=ev_mac, src_mac=EVSE_MAC)
    homeplug_header = HomePlugHeader(CM_ATTEN_CHAR | MMTYPE_IND)
    frame = (
        ethernet_header.pack_big()
        + homeplug_header.pack_big()
        + atten_char.pack_big()
    )
    parsed = AtennChar.from_bytes(frame)
    assert parsed.run_id == RUN_ID
    assert parsed.source_address == ev_mac
    assert parsed.num_sounds == 5
    assert parsed.num_groups == num_groups
    assert parsed.aag == aag


def test_match_cnf_parsing(ev_mac):
    """
    Tests that MatchCnf (CM_SLAC_MATCH.CNF) is correctly parsed from EVSE
    response bytes.
    """
    match_cnf = MatchCnf(
        pev_mac=ev_mac,
        evse_mac=EVSE_MAC,
        run_id=RUN_ID,
        nid=QUALCOMM_NID,
        nmk=QUALCOMM_NMK,
    )
    ethernet_header = EthernetHeader(dst_mac=ev_mac, src_mac=EVSE_MAC)
    homeplug_header = HomePlugHeader(CM_SLAC_MATCH | MMTYPE_CNF)
    frame = (
        ethernet_header.pack_big()
        + homeplug_header.pack_big()
        + match_cnf.pack_big()
    )
    parsed = MatchCnf.from_bytes(frame)
    assert parsed.pev_mac == ev_mac
    assert parsed.evse_mac == EVSE_MAC
    assert parsed.run_id == RUN_ID
    assert parsed.nid == QUALCOMM_NID
    assert parsed.nmk == QUALCOMM_NMK
