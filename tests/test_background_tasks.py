import asyncio
import gc


def test_supervisor_retains_and_cancels_background_task():
    from utils.background_tasks import BackgroundTaskSupervisor

    async def scenario():
        supervisor = BackgroundTaskSupervisor()
        started = asyncio.Event()
        finalized = asyncio.Event()

        async def worker():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finalized.set()

        task = supervisor.create(worker(), name="retained-worker")
        await started.wait()
        del task
        gc.collect()

        before = supervisor.snapshot()
        assert before["active"] == 1
        assert before["active_names"] == ["retained-worker"]

        await supervisor.shutdown()

        assert finalized.is_set()
        assert supervisor.snapshot()["active"] == 0

    asyncio.run(scenario())


def test_supervisor_records_terminal_failure_without_traceback_leak():
    from utils.background_tasks import BackgroundTaskSupervisor

    async def scenario():
        supervisor = BackgroundTaskSupervisor()

        async def broken():
            raise RuntimeError("secret-ish failure")

        task = supervisor.create(broken(), name="broken-worker")
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

        snapshot = supervisor.snapshot()
        assert snapshot["active"] == 0
        assert snapshot["failed"] == 1
        assert snapshot["failures"] == [
            {"name": "broken-worker", "error_type": "RuntimeError"}
        ]

    asyncio.run(scenario())

def test_shutdown_drains_tasks_spawned_during_cancellation():
    from utils.background_tasks import BackgroundTaskSupervisor

    async def scenario():
        supervisor = BackgroundTaskSupervisor()
        child_finalized = asyncio.Event()

        async def child():
            try:
                await asyncio.Event().wait()
            finally:
                child_finalized.set()

        async def parent():
            try:
                await asyncio.Event().wait()
            finally:
                supervisor.create(child(), name="shutdown-child")

        supervisor.create(parent(), name="shutdown-parent")
        await asyncio.sleep(0)
        await supervisor.shutdown()
        await asyncio.sleep(0)

        assert child_finalized.is_set()
        assert supervisor.snapshot()["active"] == 0

    asyncio.run(scenario())

def test_supervisor_rejects_new_tasks_after_shutdown():
    from utils.background_tasks import BackgroundTaskSupervisor

    async def scenario():
        supervisor = BackgroundTaskSupervisor()
        await supervisor.shutdown()

        async def late_worker():
            await asyncio.Event().wait()

        task = supervisor.create(late_worker(), name="late-worker")
        await asyncio.sleep(0)

        assert task.cancelled()
        assert supervisor.snapshot()["active"] == 0

    asyncio.run(scenario())
