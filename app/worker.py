"""Temporal worker entry point.

Invoked as:  python -m app.worker

Registers all workflow and activity implementations with the Temporal SDK
and begins polling the configured task queue. Workflows and activities are
imported here as they are implemented — see app/workflows/ and app/activities/.
"""

import asyncio


async def main() -> None:
    # TODO: Implement worker startup.
    #
    # Canonical implementation (uncomment as workflows/activities are added):
    #
    #   from temporalio.client import Client
    #   from temporalio.worker import Worker
    #   from app.config import constants
    #
    #   client = await Client.connect(
    #       constants.TEMPORAL_ADDRESS,
    #       namespace=constants.TEMPORAL_NAMESPACE,
    #   )
    #   worker = Worker(
    #       client,
    #       task_queue=constants.TEMPORAL_TASK_QUEUE,
    #       workflows=[],   # register workflow classes here
    #       activities=[],  # register activity functions here
    #   )
    #   await worker.run()
    raise NotImplementedError("Worker not yet implemented.")


if __name__ == "__main__":
    asyncio.run(main())
