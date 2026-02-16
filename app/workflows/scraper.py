"""Hacker News scraping workflow.

This module contains the primary workflow `ScrapeHackerNewsWorkflow`, which
orchestrates the end-to-end scraping process:

    1. Create scrape run record (database)
    2. Launch browser
    3. Navigate to Hacker News
    4. Scrape top N stories
    5. Persist stories to database
    6. Update scrape run status

The workflow is deterministic — all side effects (browser, database, logging)
are implemented as activities. The workflow only orchestrates.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional
from uuid import UUID

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.activities.browser import (
        BROWSER_RETRY_POLICY,
        BROWSER_START_TIMEOUT,
        HN_STORIES_PER_PAGE,
        NAVIGATE_TIMEOUT,
        NAVIGATE_TO_NEXT_PAGE_TIMEOUT,
        SCRAPE_TIMEOUT,
    )
    from app.domain.exceptions import ParseError
    from app.domain.models import ScrapeRun, ScrapeRunStatus, Story


# ---------------------------------------------------------------------------
# Activity execution options for database activities
# ---------------------------------------------------------------------------

DB_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
)

DB_ACTIVITY_TIMEOUT = timedelta(seconds=30)


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow.defn(name="ScrapeHackerNewsWorkflow")
class ScrapeHackerNewsWorkflow:
    """Orchestrates the Hacker News scraping process using Temporal activities.

    This workflow is deterministic and replay-safe. All side effects (browser
    automation, database writes, external I/O) are isolated in activities.

    Workflow execution flow:
        1. Create ScrapeRun record (status=PENDING)
        2. Start Playwright browser
        3. Navigate to Hacker News homepage (page 1)
        4. Scrape stories from current page; if top_n > HN_STORIES_PER_PAGE,
           navigate to page 2, 3, … and scrape each until top_n is reached
        5. Upsert stories into Postgres
        6. Update ScrapeRun (status=COMPLETED, stories_scraped=N)

    On failure:
        - Update ScrapeRun (status=FAILED, error_message=...)
        - Re-raise exception so Temporal marks workflow as failed

    Input:
        top_n: Number of top stories to scrape (1-100).

    Returns:
        The final ScrapeRun record with execution metadata.
    """

    @workflow.run
    async def run(self, top_n: int) -> ScrapeRun:
        """Execute the scraping workflow.

        Args:
            top_n: Number of top stories to scrape from HN front page.

        Returns:
            Final ScrapeRun record with status=COMPLETED or FAILED.

        Raises:
            Exception: Any unrecoverable activity failure (workflow fails).
        """
        wf_id = workflow.info().workflow_id
        logger = workflow.logger

        logger.info(f"Workflow starting: workflow_id={wf_id}, top_n={top_n}")

        # Track the scrape run ID so we can update it on success or failure.
        run_id: Optional[UUID] = None
        scrape_run: Optional[ScrapeRun] = None
        # Initialised here so the except handler can always reference it,
        # even when the exception fires before the scraping loop is reached.
        all_stories: list[Story] = []

        try:
            # ---------------------------------------------------------------
            # Step 1: Create scrape run record
            # ---------------------------------------------------------------
            logger.info(f"Creating scrape run record: workflow_id={wf_id}")

            scrape_run_data = await workflow.execute_activity_method(
                # Activity method name (stub for now)
                "create_scrape_run_activity",
                args=[wf_id],
                start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
                retry_policy=DB_RETRY_POLICY,
            )
            # Temporal serializes Pydantic models as dicts - reconstruct the object
            if isinstance(scrape_run_data, dict):
                scrape_run = ScrapeRun(**scrape_run_data)
            else:
                scrape_run = scrape_run_data
            run_id = scrape_run.id

            logger.info(
                f"Scrape run created: workflow_id={wf_id}, run_id={run_id}, "
                f"status={scrape_run.status.value}"
            )

            # ---------------------------------------------------------------
            # Step 2: Start Playwright browser
            # ---------------------------------------------------------------
            logger.info(f"Starting browser: workflow_id={wf_id}")

            await workflow.execute_activity_method(
                "start_playwright_activity",
                start_to_close_timeout=BROWSER_START_TIMEOUT,
                retry_policy=BROWSER_RETRY_POLICY,
            )

            logger.info(f"Browser started: workflow_id={wf_id}")

            # ---------------------------------------------------------------
            # Step 3: Navigate to Hacker News
            # ---------------------------------------------------------------
            logger.info(f"Navigating to Hacker News: workflow_id={wf_id}")

            await workflow.execute_activity_method(
                "navigate_to_hacker_news_activity",
                start_to_close_timeout=NAVIGATE_TIMEOUT,
                retry_policy=BROWSER_RETRY_POLICY,
            )

            logger.info(f"Navigation completed: workflow_id={wf_id}")

            # ---------------------------------------------------------------
            # Step 4: Scrape stories (with pagination when top_n > 30)
            # ---------------------------------------------------------------
            pages_needed = (top_n + HN_STORIES_PER_PAGE - 1) // HN_STORIES_PER_PAGE
            logger.info(
                f"Scraping stories: workflow_id={wf_id}, top_n={top_n}, "
                f"pages_needed={pages_needed}"
            )

            # Page 1 is already loaded by navigate_to_hacker_news_activity.
            page_1_stories: list[Story] = await workflow.execute_activity_method(
                "scrape_urls_activity",
                args=[top_n],
                start_to_close_timeout=SCRAPE_TIMEOUT,
                retry_policy=BROWSER_RETRY_POLICY,
            )
            all_stories.extend(page_1_stories)
            logger.info(
                f"Page 1 scraped: workflow_id={wf_id}, "
                f"stories_on_page={len(page_1_stories)}, "
                f"total_so_far={len(all_stories)}"
            )

            # Pages 2..N — only executed when top_n > HN_STORIES_PER_PAGE.
            for page_number in range(2, pages_needed + 1):
                if len(all_stories) >= top_n:
                    break

                logger.info(
                    f"Navigating to page {page_number}: workflow_id={wf_id}"
                )
                has_more: bool = await workflow.execute_activity_method(
                    "navigate_to_next_page_activity",
                    args=[page_number],
                    start_to_close_timeout=NAVIGATE_TO_NEXT_PAGE_TIMEOUT,
                    retry_policy=BROWSER_RETRY_POLICY,
                )
                if not has_more:
                    logger.info(
                        f"HN has no more pages, stopping pagination: "
                        f"workflow_id={wf_id}, last_page={page_number - 1}"
                    )
                    break

                page_stories: list[Story] = await workflow.execute_activity_method(
                    "scrape_urls_activity",
                    args=[top_n],
                    start_to_close_timeout=SCRAPE_TIMEOUT,
                    retry_policy=BROWSER_RETRY_POLICY,
                )

                if not page_stories:
                    logger.info(
                        f"No stories on page {page_number}, stopping pagination: "
                        f"workflow_id={wf_id}"
                    )
                    break

                all_stories.extend(page_stories)
                logger.info(
                    f"Page {page_number} scraped: workflow_id={wf_id}, "
                    f"stories_on_page={len(page_stories)}, "
                    f"total_so_far={len(all_stories)}"
                )

            # Truncate to the requested top_n — last page may return surplus.
            stories: list[Story] = all_stories[:top_n]
            stories_count = len(stories)
            logger.info(
                f"Stories scraped: workflow_id={wf_id}, stories_count={stories_count}"
            )

            # ---------------------------------------------------------------
            # Step 5: Persist stories to database
            # ---------------------------------------------------------------
            logger.info(
                f"Persisting stories: workflow_id={wf_id}, stories_count={stories_count}")

            upserted_count: int = await workflow.execute_activity_method(
                "upsert_stories_activity",
                args=[stories],
                start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
                retry_policy=DB_RETRY_POLICY,
            )

            logger.info(
                f"Stories persisted: workflow_id={wf_id}, upserted_count={upserted_count}")

            # ---------------------------------------------------------------
            # Step 6: Update scrape run status to COMPLETED
            # ---------------------------------------------------------------
            logger.info(
                f"Updating scrape run to COMPLETED: workflow_id={wf_id}")

            scrape_run_data = await workflow.execute_activity_method(
                "update_scrape_run_activity",
                args=[
                    run_id,
                    ScrapeRunStatus.COMPLETED.value,
                    upserted_count,
                    None,  # error_message
                ],
                start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
                retry_policy=DB_RETRY_POLICY,
            )
            if isinstance(scrape_run_data, dict):
                scrape_run = ScrapeRun(**scrape_run_data)
            else:
                scrape_run = scrape_run_data

            logger.info(
                f"Workflow completed successfully: workflow_id={wf_id}, run_id={run_id}, "
                f"stories_scraped={upserted_count}"
            )

            return scrape_run

        except Exception as exc:
            # Workflow failed — update scrape run status to FAILED if we
            # have a run_id (i.e., if the run record was created before failure).
            logger.error(
                f"Workflow failed: workflow_id={wf_id}, run_id={run_id if run_id else None}, "
                f"error_type={type(exc).__name__}, error={str(exc)}"
            )

            if run_id is not None:
                # Best-effort: persist any stories scraped before the failure
                # so partial results are not lost.
                salvaged_count: Optional[int] = None
                if all_stories:
                    stories_to_salvage = all_stories[:top_n]
                    logger.info(
                        f"Salvaging {len(stories_to_salvage)} stories before marking "
                        f"run as FAILED: workflow_id={wf_id}"
                    )
                    try:
                        salvaged_count = await workflow.execute_activity_method(
                            "upsert_stories_activity",
                            args=[stories_to_salvage],
                            start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
                            retry_policy=DB_RETRY_POLICY,
                        )
                        logger.info(
                            f"Salvaged {salvaged_count} stories: workflow_id={wf_id}"
                        )
                    except Exception as salvage_exc:  # noqa: BLE001
                        # Best effort — don't mask the original error.
                        logger.error(
                            f"Failed to salvage stories on workflow failure: "
                            f"workflow_id={wf_id}, error={str(salvage_exc)}"
                        )

                try:
                    scrape_run_data = await workflow.execute_activity_method(
                        "update_scrape_run_activity",
                        args=[
                            run_id,
                            ScrapeRunStatus.FAILED.value,
                            salvaged_count,  # stories_scraped (None if nothing salvaged)
                            str(exc),  # error_message
                        ],
                        start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
                        retry_policy=DB_RETRY_POLICY,
                    )
                    if isinstance(scrape_run_data, dict):
                        scrape_run = ScrapeRun(**scrape_run_data)
                    else:
                        scrape_run = scrape_run_data
                except Exception as update_exc:  # noqa: BLE001
                    # Best effort — if updating run status fails, log but
                    # don't mask the original error.
                    logger.error(
                        f"Failed to update scrape run status: workflow_id={wf_id}, "
                        f"run_id={run_id}, error={str(update_exc)}"
                    )

            # Re-raise the original exception so Temporal marks workflow as failed.
            raise
