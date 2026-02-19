"""Hacker News scraping workflow.

This module contains the primary workflow `ScrapeHackerNewsWorkflow`, which
orchestrates the end-to-end scraping process:

    1. Create scrape run record (database)
    2. Launch browser
    3. Navigate to Hacker News
    4. Scrape top N stories
    5. Scrape top comment for each story
    6. Persist stories (with comments) to database
    7. Update scrape run status

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
        CLEANUP_TIMEOUT,
        HN_STORIES_PER_PAGE,
        NAVIGATE_TIMEOUT,
        NAVIGATE_TO_NEXT_PAGE_TIMEOUT,
        SCRAPE_COMMENT_TIMEOUT,
        SCRAPE_TIMEOUT,
    )
    from app.config import constants
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
        top_n: Number of top stories to scrape.

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
            # Wrap entire workflow in try/finally to ensure browser cleanup
            return await self._execute_scrape(
                wf_id, logger, top_n, run_id, scrape_run, all_stories
            )
        finally:
            # Always clean up browser context, even if workflow fails.
            # This prevents memory leaks from accumulating browser contexts.
            logger.info(f"Cleaning up browser context: workflow_id={wf_id}")
            try:
                await workflow.execute_activity_method(
                    "cleanup_browser_context_activity",
                    start_to_close_timeout=CLEANUP_TIMEOUT,
                    retry_policy=BROWSER_RETRY_POLICY,
                )
                logger.info(f"Browser context cleaned up: workflow_id={wf_id}")
            except Exception as cleanup_exc:  # noqa: BLE001
                # Best effort — log cleanup failure but don't mask the
                # original workflow error (if any).
                logger.error(
                    f"Failed to clean up browser context: workflow_id={wf_id}, "
                    f"error={str(cleanup_exc)}"
                )

    async def _execute_scrape(
        self,
        wf_id: str,
        logger,
        top_n: int,
        run_id: Optional[UUID],
        scrape_run: Optional[ScrapeRun],
        all_stories: list[Story],
    ) -> ScrapeRun:
        """Internal method containing the main scrape workflow logic.

        Extracted to allow try/finally cleanup in the main run method.
        """
        # Initialize here so it's available in the except block for salvaging
        stories_with_comments: list[Story] = []

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
            pages_needed = (top_n + HN_STORIES_PER_PAGE -
                            1) // HN_STORIES_PER_PAGE
            logger.info(
                f"Scraping stories: workflow_id={wf_id}, top_n={top_n}, "
                f"pages_needed={pages_needed}"
            )

            # Page 1 is already loaded by navigate_to_hacker_news_activity.
            raw_page_1_stories = await workflow.execute_activity_method(
                "scrape_urls_activity",
                args=[top_n],
                start_to_close_timeout=SCRAPE_TIMEOUT,
                retry_policy=BROWSER_RETRY_POLICY,
            )
            page_1_stories: list[Story] = [
                Story(**s) if isinstance(s, dict) else s for s in raw_page_1_stories
            ]
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

                raw_page_stories = await workflow.execute_activity_method(
                    "scrape_urls_activity",
                    args=[top_n],
                    start_to_close_timeout=SCRAPE_TIMEOUT,
                    retry_policy=BROWSER_RETRY_POLICY,
                )
                page_stories: list[Story] = [
                    Story(**s) if isinstance(s, dict) else s for s in raw_page_stories
                ]

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
            # Step 5: Scrape top comment for each story
            # ---------------------------------------------------------------
            logger.info(
                f"Scraping comments: workflow_id={wf_id}, stories_count={stories_count}"
            )

            comments_scraped = 0
            comments_failed = 0

            for idx, story in enumerate(stories, start=1):
                logger.info(
                    f"Scraping comment for story {idx}/{stories_count}: "
                    f"workflow_id={wf_id}, hn_id={story.hn_id}"
                )

                try:
                    top_comment: Optional[str] = await workflow.execute_activity_method(
                        "scrape_top_comment_activity",
                        args=[story.hn_id],
                        start_to_close_timeout=SCRAPE_COMMENT_TIMEOUT,
                        retry_policy=BROWSER_RETRY_POLICY,
                    )

                    # Enrich story with top comment
                    enriched_story = story.model_copy(
                        update={"top_comment": top_comment}
                    )
                    stories_with_comments.append(enriched_story)

                    if top_comment:
                        comments_scraped += 1
                        logger.info(
                            f"Comment scraped: workflow_id={wf_id}, hn_id={story.hn_id}, "
                            f"comment_length={len(top_comment)}"
                        )
                    else:
                        logger.info(
                            f"No comment found: workflow_id={wf_id}, hn_id={story.hn_id}"
                        )

                except Exception as exc:  # noqa: BLE001
                    # Continue on error: log failure, store story with NULL comment
                    comments_failed += 1
                    logger.error(
                        f"Comment scraping failed: workflow_id={wf_id}, "
                        f"hn_id={story.hn_id}, error_type={type(exc).__name__}, "
                        f"error={str(exc)}"
                    )
                    # Store story with NULL comment
                    enriched_story = story.model_copy(update={"top_comment": None})
                    stories_with_comments.append(enriched_story)

                # Rate limiting: add delay between comment scrapes
                # (except after the last story—no need to wait)
                if idx < stories_count:
                    delay_seconds = constants.COMMENT_SCRAPE_DELAY_MS / 1000.0
                    await workflow.sleep(delay_seconds)

            logger.info(
                f"Comments scraping completed: workflow_id={wf_id}, "
                f"total={stories_count}, scraped={comments_scraped}, "
                f"no_comments={stories_count - comments_scraped - comments_failed}, "
                f"failed={comments_failed}"
            )

            # ---------------------------------------------------------------
            # Step 6: Persist stories to database
            # ---------------------------------------------------------------
            logger.info(
                f"Persisting stories: workflow_id={wf_id}, stories_count={stories_count}")

            upserted_count: int = await workflow.execute_activity_method(
                "upsert_stories_activity",
                args=[stories_with_comments],  # Now includes top_comment
                start_to_close_timeout=DB_ACTIVITY_TIMEOUT,
                retry_policy=DB_RETRY_POLICY,
            )

            logger.info(
                f"Stories persisted: workflow_id={wf_id}, upserted_count={upserted_count}")

            # ---------------------------------------------------------------
            # Step 7: Update scrape run status to COMPLETED
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

                # Prefer stories_with_comments if populated (failure during/after
                # comment scraping), otherwise fall back to all_stories (failure
                # during story scraping, before comment scraping began).
                if stories_with_comments:
                    stories_to_salvage = stories_with_comments
                    logger.info(
                        f"Salvaging {len(stories_to_salvage)} stories with comments "
                        f"before marking run as FAILED: workflow_id={wf_id}"
                    )
                elif all_stories:
                    stories_to_salvage = all_stories[:top_n]
                    logger.info(
                        f"Salvaging {len(stories_to_salvage)} stories (no comments) "
                        f"before marking run as FAILED: workflow_id={wf_id}"
                    )
                else:
                    stories_to_salvage = []

                if stories_to_salvage:
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
                            # stories_scraped (None if nothing salvaged)
                            salvaged_count,
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
