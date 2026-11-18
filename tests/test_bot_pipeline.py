import asyncio

import pytest

from tmjdlc.bot.pipeline import AdjustableGate, BatchProgress, _card


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



def test_batch_progress_renders_all_video_states_in_one_card():
    tracker = BatchProgress(None, 123, ["ABP-123", "XYZ-456", "DEF-789", "GHI-012"])
    tracker.items["ABP-123"].update(
        status="DOWNLOADING", phase="DOWNLOAD", done=65, total=100,
        speed=10, eta=4)
    tracker.items["XYZ-456"].update(
        status="UPLOADING", phase="UPLOAD", done=32, total=100,
        speed=8, eta=9)
    tracker.items["DEF-789"]["status"] = "WAITING_UPLOAD"
    card = tracker.render()
    assert "DOWNLOAD BATCH · 4 VIDEOS" in card
    assert "ABP-123" in card and "65%" in card
    assert "XYZ-456" in card and "32%" in card
    assert "DEF-789" in card and "Waiting for upload" in card
    assert "GHI-012" in card and "Waiting" in card
    assert "Active: 2" in card


def test_batch_progress_compacts_large_batches():
    tracker = BatchProgress(None, 123, [f"CODE-{i}" for i in range(45)])
    card = tracker.render()
    assert "CODE-0" in card
    assert "CODE-29" in card
    assert "CODE-30" not in card
    assert "… and 15 more videos" in card



def test_adjustable_gate_supports_four_upload_workers():
    gate = AdjustableGate(1, maximum=4)
    async def scenario():
        await gate.set_limit(4)
        for _ in range(4):
            await gate.acquire()
        assert gate.active == 4
        await gate.release()
        await gate.release()
        await gate.release()
        await gate.release()
    asyncio.run(scenario())


def test_upload_gate_rejects_more_than_four_workers():
    with pytest.raises(ValueError):
        AdjustableGate(5, maximum=4)
