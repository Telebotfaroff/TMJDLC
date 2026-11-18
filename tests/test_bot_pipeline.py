import asyncio

import pytest

from tmjdlc.bot.pipeline import AdjustableGate, _card


def test_adjustable_gate_limits_parallel_work():
    async def scenario():
        gate = AdjustableGate(1)
        await gate.acquire()
        waiting = asyncio.create_task(gate.acquire())
        await asyncio.sleep(0)
        assert not waiting.done()
        await gate.set_limit(2)
        await asyncio.wait_for(waiting, timeout=1)
        assert gate.active == 2
        await gate.release()
        await gate.release()

    asyncio.run(scenario())


def test_adjustable_gate_rejects_invalid_limits():
    with pytest.raises(ValueError):
        AdjustableGate(0)
    gate = AdjustableGate(2)
    with pytest.raises(ValueError):
        asyncio.run(gate.set_limit(7))


def test_progress_card_shows_percentage_size_speed_and_eta():
    card = _card("ABP-123", "DOWNLOAD", 5 * 1024 * 1024,
                 10 * 1024 * 1024, 1024 * 1024, 5)
    assert "50.0%" in card
    assert "5.00 MiB / 10.00 MiB" in card
    assert "1.00 MiB/s" in card
    assert "5s" in card
    assert "█████░░░░░" in card


def test_progress_card_escapes_dynamic_html():
    card = _card("<ABP&123>", "UPLOAD", 0, None, None, None,
                 "<bad> & unsafe")
    assert "&lt;ABP&amp;123&gt;" in card
    assert "&lt;bad&gt; &amp; unsafe" in card
    assert "<bad>" not in card
