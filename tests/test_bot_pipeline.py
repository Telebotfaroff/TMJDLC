import asyncio

import pytest

from tmjdlc.bot.pipeline import AdjustableGate


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
