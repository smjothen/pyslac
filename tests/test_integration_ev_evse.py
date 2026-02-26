"""
Integration test: EV and EVSE run the full SLAC protocol together.

Both sides communicate via an in-memory socket bridge (asyncio.Queue-based),
so no root privileges or real network interfaces are required.  The bridge
transparently routes frames between the two sessions and synthesises the
CM_ATTEN_PROFILE.IND messages that a real PLC chip would generate for each
CM_MNBC_SOUND.IND.

Run with:
    pytest tests/test_integration_ev_evse.py -v
"""
import asyncio
from os import urandom
from unittest.mock import Mock, patch

import pytest

from pyslac.enums import (
    BROADCAST_ADDR,
    CM_ATTEN_PROFILE,
    CM_MNBC_SOUND,
    EVSE_PLC_MAC,
    MMTYPE_IND,
    SLAC_GROUPS,
    STATE_MATCHED,
)
from pyslac.environment import Config
from pyslac.layer_2_headers import EthernetHeader, HomePlugHeader
from pyslac.messages import AttenProfile
from pyslac.session import SlacEvseSession
from pyslac.session_ev import SlacEvSession
from pyslac.utils import generate_nid

# ---------------------------------------------------------------------------
# Test fixtures / constants
# ---------------------------------------------------------------------------

EV_MAC = b"\xAA\xBB\xCC\xDD\xEE\xFF"
EVSE_MAC = b"\x11\x22\x33\x44\x55\x66"
EVSE_ID = "DE*12*122333"
IFACE_EV = "slac_ev"
IFACE_EVSE = "slac_evse"

# Deterministic NMK/NID for the EVSE (set before matching begins)
_EVSE_NMK = urandom(16)
_EVSE_NID = generate_nid(_EVSE_NMK)


# ---------------------------------------------------------------------------
# In-memory socket bridge
# ---------------------------------------------------------------------------


class SocketBridge:
    """
    Virtual Ethernet link between an EV session and an EVSE session.

    Two asyncio.Queue instances carry frames in each direction:
      * ev_to_evse – frames sent by EV, read by EVSE
      * evse_to_ev – frames sent by EVSE, read by EV

    When the EV sends a CM_MNBC_SOUND.IND frame the bridge also synthesises
    a matching CM_ATTEN_PROFILE.IND (as a real PLC chip would) and enqueues
    it so the EVSE's cm_sounds_loop receives both frame types in the expected
    alternating order.
    """

    def __init__(self, ev_mac: bytes, evse_mac: bytes) -> None:
        self._ev_to_evse: asyncio.Queue = asyncio.Queue()
        self._evse_to_ev: asyncio.Queue = asyncio.Queue()
        self._ev_mac = ev_mac
        self._evse_mac = evse_mac

    # ---- send helpers (replace session.send_frame) -------------------------

    async def ev_send(self, frame: bytes) -> None:
        """Route a frame from the EV side to the EVSE side."""
        await self._ev_to_evse.put(frame)
        # If the EV just sent a CM_MNBC_SOUND.IND, synthesise the
        # CM_ATTEN_PROFILE.IND that the EVSE's PLC chip would generate.
        try:
            homeplug_frame = HomePlugHeader.from_bytes(frame)
            if homeplug_frame.mm_type == CM_MNBC_SOUND | MMTYPE_IND:
                await self._ev_to_evse.put(self._make_atten_profile())
        except Exception:  # pragma: no cover
            pass

    async def evse_send(self, frame: bytes) -> None:
        """Route a frame from the EVSE side to the EV side."""
        await self._evse_to_ev.put(frame)

    # ---- receive helpers (replace session.rcv_frame) -----------------------

    async def ev_recv(self, rcv_frame_size: int, timeout: float) -> bytes:
        """Return the next frame destined for the EV side."""
        return await asyncio.wait_for(self._evse_to_ev.get(), timeout=timeout)

    async def evse_recv(self, rcv_frame_size: int, timeout: float) -> bytes:
        """Return the next frame destined for the EVSE side."""
        return await asyncio.wait_for(self._ev_to_evse.get(), timeout=timeout)

    # ---- private helpers ---------------------------------------------------

    def _make_atten_profile(self) -> bytes:
        """
        Build a synthetic CM_ATTEN_PROFILE.IND frame.

        In a real deployment this message is sent by the EVSE's PLC chip
        (EVSE_PLC_MAC → evse_mac) after each received M-Sound.  All
        attenuation groups are set to 0 so the EV's threshold check passes.
        """
        ether_header = EthernetHeader(
            dst_mac=self._evse_mac, src_mac=EVSE_PLC_MAC
        )
        homeplug_header = HomePlugHeader(CM_ATTEN_PROFILE | MMTYPE_IND)
        atten_profile = AttenProfile(
            pev_mac=self._ev_mac,
            num_groups=SLAC_GROUPS,
            aag=[0] * SLAC_GROUPS,
        )
        return (
            ether_header.pack_big()
            + homeplug_header.pack_big()
            + atten_profile.pack_big()
        )


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ev_session() -> SlacEvSession:
    with patch("pyslac.session_ev.get_if_hwaddr", return_value=EV_MAC):
        with patch("pyslac.session_ev.create_socket", return_value=Mock()):
            session = SlacEvSession(iface=IFACE_EV)
    return session


@pytest.fixture
def evse_session() -> SlacEvseSession:
    config = Config(slac_init_timeout=5, slac_atten_results_timeout=None)
    with patch("pyslac.session.get_if_hwaddr", return_value=EVSE_MAC):
        with patch("pyslac.session.create_socket", return_value=Mock()):
            session = SlacEvseSession(EVSE_ID, IFACE_EVSE, config)
            session.reset_socket = Mock()
    # Pre-load NMK/NID so the EVSE can include them in CM_SLAC_MATCH.CNF
    session.nmk = _EVSE_NMK
    session.nid = _EVSE_NID
    return session


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_slac_matching(ev_session, evse_session):
    """
    Run both EV and EVSE through the complete SLAC protocol using an
    in-memory bridge.  Asserts that both sessions reach STATE_MATCHED with
    consistent run_id, NMK, and NID.
    """
    bridge = SocketBridge(ev_mac=EV_MAC, evse_mac=EVSE_MAC)

    # Wire the bridge into both sessions
    ev_session.send_frame = bridge.ev_send
    ev_session.rcv_frame = bridge.ev_recv

    evse_session.send_frame = bridge.evse_send
    evse_session.rcv_frame = bridge.evse_recv

    async def evse_run():
        await evse_session.evse_slac_parm()
        await evse_session.atten_charac_routine()

    # Run both sides concurrently with a generous overall timeout
    await asyncio.wait_for(
        asyncio.gather(evse_run(), ev_session.ev_match_routine()),
        timeout=30,
    )

    # Both sessions must have reached the MATCHED state
    assert ev_session.state == STATE_MATCHED
    assert evse_session.state == STATE_MATCHED

    # The run_id must be the same on both sides (generated by EV)
    assert ev_session.run_id == evse_session.run_id
    assert len(ev_session.run_id) == 8

    # EV must have received the EVSE's NMK and NID
    assert ev_session.nmk == _EVSE_NMK
    assert ev_session.nid == _EVSE_NID
