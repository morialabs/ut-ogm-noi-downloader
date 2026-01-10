#!/usr/bin/env python3
"""
Utah OGM NOI Document Downloader

Downloads NOI documents from ogm.utah.gov/minerals-files/
Iterates through all pages, downloading documents from 2020 onwards.

Usage:
    python download_noi.py                    # Download all (visible browser)
    python download_noi.py --headless         # Run headless
    python download_noi.py --dry-run          # List without downloading
    python download_noi.py --start-page 5     # Start from page 5
    python download_noi.py --min-date 2010-01-01  # Custom date filter
"""

import asyncio
import argparse
import re
from datetime import date
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# Configuration
OUTPUT_DIR = Path(__file__).parent / "output"
PORTAL_URL = "https://ogm.utah.gov/minerals-files/"
MIN_DATE = date(2020, 1, 1)
RATE_LIMIT_DELAY = 2.0  # seconds between downloads
MAX_RETRIES = 3


def get_existing_documents() -> set:
    """Scan output directory and return set of (permit_id, date) tuples."""
    existing = set()

    if not OUTPUT_DIR.exists():
        return existing

    for permit_dir in OUTPUT_DIR.iterdir():
        if permit_dir.is_dir() and permit_dir.name != 'debug':
            for pdf_file in permit_dir.glob('*.pdf'):
                # Parse filename: M0010058_2007-01-11.pdf
                match = re.match(r'([MS]\d{7})_(\d{4}-\d{2}-\d{2})\.pdf', pdf_file.name)
                if match:
                    existing.add((match.group(1), match.group(2)))

    return existing


def should_download(record: dict, existing: set, min_date: date) -> tuple[bool, str]:
    """Determine if a document should be downloaded. Returns (should_download, reason)."""
    permit_id = record.get('permit_id', '')
    doc_date_str = record.get('doc_date', '')

    # Validate permit ID
    if not permit_id or not re.match(r'[MS]\d{7}', permit_id):
        return False, f"Invalid permit ID: {permit_id}"

    # Parse and validate date
    try:
        doc_date = date.fromisoformat(doc_date_str)
    except (ValueError, TypeError):
        return False, f"Invalid date format: {doc_date_str}"

    # Check date threshold
    if doc_date < min_date:
        return False, f"Date {doc_date} is before {min_date}"

    # Check if file already exists
    if (permit_id, doc_date_str) in existing:
        return False, "Already downloaded"

    return True, "OK"


async def get_current_page_info(sf_frame) -> dict:
    """Extract current page number and total pages from pagination text."""
    body_text = await sf_frame.evaluate("() => document.body.innerText")

    # Look for text like "Showing 1 of 17"
    match = re.search(r'Showing\s+(\d+)\s+of\s+(\d+)', body_text)
    if match:
        return {
            'current': int(match.group(1)),
            'total': int(match.group(2))
        }
    return {'current': 1, 'total': 1}


async def get_all_rows_on_page(sf_frame) -> list[dict]:
    """Extract all document records from current page using JavaScript."""
    rows = await sf_frame.evaluate("""
        (() => {
            const records = [];

            // Get visible text and parse it
            const bodyText = document.body.innerText;
            const lines = bodyText.split('\\n').map(l => l.trim()).filter(l => l);

            // Find permit IDs (M0010058 or S0010018 format)
            const permitPattern = /^[MS]\\d{7}$/;
            const datePattern = /^\\d{4}-\\d{2}-\\d{2}$/;

            let currentRecord = null;
            let rowIndex = 0;

            for (let i = 0; i < lines.length; i++) {
                const line = lines[i];

                // Start of a new record
                if (permitPattern.test(line)) {
                    if (currentRecord && currentRecord.permit_id) {
                        records.push(currentRecord);
                    }

                    currentRecord = {
                        row_index: rowIndex++,
                        permit_id: line,
                        site: '',
                        operator: '',
                        doc_date: '',
                        description: ''
                    };

                    // Next lines should be site, operator, date, description
                    if (i + 1 < lines.length && !permitPattern.test(lines[i + 1])) {
                        currentRecord.site = lines[i + 1];
                    }
                    if (i + 2 < lines.length && !permitPattern.test(lines[i + 2])) {
                        currentRecord.operator = lines[i + 2];
                    }
                    if (i + 3 < lines.length && datePattern.test(lines[i + 3])) {
                        currentRecord.doc_date = lines[i + 3];
                    }
                    if (i + 4 < lines.length && !permitPattern.test(lines[i + 4]) && !datePattern.test(lines[i + 4])) {
                        currentRecord.description = lines[i + 4];
                    }
                }
            }

            // Don't forget the last record
            if (currentRecord && currentRecord.permit_id) {
                records.push(currentRecord);
            }

            return records;
        })()
    """)
    return rows


async def click_view_button_for_row(sf_frame, context, row_index: int) -> Optional[str]:
    """Click the VIEW button for a specific row and return the PDF URL.

    The VIEW button is hidden until the row is hovered (Salesforce hint-parent pattern).
    We need to hover over the row first to reveal the button.
    """

    # Strategy 1: Hover over row to reveal button, then click
    try:
        row_selector = f'tr[data-row-key-value="row-{row_index}"]'
        row = sf_frame.locator(row_selector)

        # Hover over the row to reveal the VIEW button
        await row.hover()
        await sf_frame.page.wait_for_timeout(500)  # Small delay for button to appear

        # Find the lightning-button-icon in this row (VIEW button)
        view_button = row.locator('lightning-button-icon').first

        # Set up listener for new page before clicking
        async with context.expect_page(timeout=30000) as new_page_info:
            await view_button.click(force=True)  # force click in case of overlay

        new_page = await new_page_info.value
        await new_page.wait_for_load_state("networkidle", timeout=30000)
        pdf_url = new_page.url
        return pdf_url

    except Exception as e:
        print(f"       Strategy 1 (hover+click) failed: {e}")

    # Strategy 2: JavaScript to hover and click
    try:
        result = await sf_frame.evaluate(f"""
            (() => {{
                const row = document.querySelector('tr[data-row-key-value="row-{row_index}"]');
                if (row) {{
                    // Trigger hover effect
                    row.dispatchEvent(new MouseEvent('mouseenter', {{ bubbles: true }}));
                    row.dispatchEvent(new MouseEvent('mouseover', {{ bubbles: true }}));

                    // Find and click the button
                    const btn = row.querySelector('lightning-button-icon');
                    if (btn) {{
                        btn.click();
                        return {{ clicked: true, method: 'js-hover-click' }};
                    }}
                }}
                return {{ clicked: false }};
            }})()
        """)

        if result and result.get('clicked'):
            await sf_frame.page.wait_for_timeout(3000)
            all_pages = context.pages
            if len(all_pages) > 1:
                return all_pages[-1].url

    except Exception as e:
        print(f"       Strategy 2 (JS hover+click) failed: {e}")

    # Strategy 3: Direct force click on the hidden button
    try:
        row_selector = f'tr[data-row-key-value="row-{row_index}"]'
        row = sf_frame.locator(row_selector)
        view_button = row.locator('lightning-button-icon').first

        async with context.expect_page(timeout=30000) as new_page_info:
            await view_button.dispatch_event('click')

        new_page = await new_page_info.value
        await new_page.wait_for_load_state("networkidle", timeout=30000)
        return new_page.url

    except Exception as e:
        print(f"       Strategy 3 (dispatch click) failed: {e}")

    # Check for new tab anyway
    await sf_frame.page.wait_for_timeout(3000)
    all_pages = context.pages
    if len(all_pages) > 1:
        pdf_page = all_pages[-1]
        return pdf_page.url

    return None


async def download_pdf(page, pdf_url: str, record: dict) -> Optional[Path]:
    """Download PDF from URL and save to output directory."""
    permit_id = record['permit_id']
    doc_date = record['doc_date']

    # Create output directory
    permit_dir = OUTPUT_DIR / permit_id
    permit_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{permit_id}_{doc_date}.pdf"
    output_path = permit_dir / filename

    # Download the PDF
    response = await page.request.get(pdf_url)
    content_bytes = await response.body()

    # Verify it's a PDF
    if content_bytes[:4] == b'%PDF':
        output_path.write_bytes(content_bytes)
        return output_path
    else:
        # Save anyway but warn
        output_path.write_bytes(content_bytes)
        print(f"       Warning: Content may not be PDF (first bytes: {content_bytes[:10]})")
        return output_path


async def cleanup_extra_tabs(context):
    """Close any tabs beyond the main page."""
    pages = context.pages
    if len(pages) > 1:
        for page in pages[1:]:
            await page.close()


async def navigate_to_next_page(sf_frame) -> bool:
    """Click Next button and wait for table to update. Returns False if on last page."""
    page_info = await get_current_page_info(sf_frame)

    if page_info['current'] >= page_info['total']:
        return False

    # First, close any open detail panels by pressing Escape
    try:
        await sf_frame.page.keyboard.press('Escape')
        await sf_frame.page.wait_for_timeout(500)
    except Exception:
        pass

    # Click the NOIs tab to ensure it's focused
    try:
        nois_tab = sf_frame.get_by_text("NOIs", exact=True)
        await nois_tab.click()
        await sf_frame.page.wait_for_timeout(2000)
    except Exception:
        pass

    # Re-check page info after closing panels
    page_info = await get_current_page_info(sf_frame)

    # Strategy 1: Try JavaScript click with Shadow DOM traversal
    try:
        result = await sf_frame.evaluate("""
            (() => {
                // Traverse Shadow DOMs to find elements
                function findElementsDeep(selector, root = document, results = [], depth = 0) {
                    if (depth > 20) return results;

                    // Check current root
                    const elements = root.querySelectorAll(selector);
                    results.push(...elements);

                    // Check all shadow roots
                    const allElements = root.querySelectorAll('*');
                    for (const el of allElements) {
                        if (el.shadowRoot) {
                            findElementsDeep(selector, el.shadowRoot, results, depth + 1);
                        }
                    }

                    return results;
                }

                // Find all Next buttons in shadow DOMs
                const buttons = findElementsDeep('button');
                const nextButtons = buttons.filter(btn => btn.textContent.trim() === 'Next');

                console.log('Found ' + nextButtons.length + ' Next buttons in shadow DOM');

                // Click the first visible Next button
                for (const btn of nextButtons) {
                    // Check if button is in the NOIs tab area (not the detail panel)
                    const isVisible = btn.offsetParent !== null;
                    if (isVisible) {
                        const clickEvent = new MouseEvent('click', {
                            bubbles: true,
                            cancelable: true,
                            view: window
                        });
                        btn.dispatchEvent(clickEvent);
                        return { clicked: true, method: 'shadow-dom-dispatch', total: nextButtons.length };
                    }
                }

                // If no visible, try clicking any Next button
                if (nextButtons.length > 0) {
                    nextButtons[0].click();
                    return { clicked: true, method: 'shadow-dom-direct', total: nextButtons.length };
                }

                return { clicked: false, total: nextButtons.length };
            })()
        """)

        if result and result.get('clicked'):
            await sf_frame.page.wait_for_timeout(4000)
            new_page_info = await get_current_page_info(sf_frame)
            if new_page_info['current'] > page_info['current']:
                return True

    except Exception as e:
        print(f"       JS navigation error: {e}")

    # Strategy 2: Try Playwright dispatch_event (doesn't require visibility)
    try:
        next_buttons = sf_frame.get_by_role("button", name="Next")
        count = await next_buttons.count()

        if count > 0:
            await next_buttons.first.dispatch_event('click')
            await sf_frame.page.wait_for_timeout(4000)
            new_page_info = await get_current_page_info(sf_frame)
            if new_page_info['current'] > page_info['current']:
                return True

    except Exception as e:
        pass  # Fall through to next strategy

    # Strategy 3: Use evaluate_handle to click via element handle
    try:
        next_button = sf_frame.locator('button:has-text("Next")').first
        await next_button.evaluate("el => el.click()")
        await sf_frame.page.wait_for_timeout(4000)
        new_page_info = await get_current_page_info(sf_frame)
        return new_page_info['current'] > page_info['current']

    except Exception as e:
        print(f"       Navigation error: {e}")
        return False


async def download_all_nois(
    headless: bool = False,
    dry_run: bool = False,
    start_page: int = 1,
    end_page: Optional[int] = None,
    min_date: date = MIN_DATE
):
    """Main function to download all NOIs from specified date onwards."""

    # Load existing documents to skip
    existing = get_existing_documents()
    print(f"Found {len(existing)} existing documents to skip")

    # Statistics tracking
    stats = {
        'total_processed': 0,
        'downloaded': 0,
        'skipped_exists': 0,
        'skipped_date': 0,
        'failed': 0
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()

        # Track PDF URLs from network requests (fallback)
        pdf_urls = []

        def capture_request(request):
            url = request.url
            if ".pdf" in url.lower() or "s3.amazonaws" in url.lower():
                pdf_urls.append(url)

        page.on("request", capture_request)

        try:
            print("[1/4] Navigating to minerals-files page...")
            await page.goto(PORTAL_URL, wait_until="networkidle", timeout=60000)

            print("[2/4] Waiting for iframe to load...")
            await page.wait_for_selector("iframe", timeout=30000)
            await page.wait_for_timeout(5000)

            # Find the Salesforce frame
            frames = page.frames
            sf_frame = None
            for frame in frames:
                frame_url = frame.url
                if "site.com" in frame_url.lower() or "salesforce" in frame_url.lower():
                    sf_frame = frame
                    break

            if sf_frame is None and len(frames) > 1:
                sf_frame = frames[1]

            if sf_frame is None:
                raise Exception("Could not find Salesforce iframe")

            print("[3/4] Clicking NOIs tab...")
            try:
                nois_tab = sf_frame.get_by_text("NOIs", exact=True)
                await nois_tab.wait_for(state="visible", timeout=60000)
                await nois_tab.click()
            except Exception as e:
                print(f"       NOIs tab click via locator failed: {e}")
                await sf_frame.evaluate("""
                    (() => {
                        const tabs = document.querySelectorAll('*');
                        for (const tab of tabs) {
                            if (tab.textContent.trim() === 'NOIs' && tab.offsetParent !== null) {
                                tab.click();
                                return true;
                            }
                        }
                        return false;
                    })()
                """)

            await page.wait_for_timeout(5000)

            # Save debug info after clicking NOIs tab
            debug_dir = OUTPUT_DIR / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            body_text = await sf_frame.evaluate("() => document.body.innerText")
            content = await sf_frame.content()
            (debug_dir / "nois_body_text.txt").write_text(body_text)
            (debug_dir / "nois_content.html").write_text(content)
            await page.screenshot(path=str(debug_dir / "nois_page.png"))
            print("       Debug files saved to output/debug/")

            print("[4/4] Processing documents...")

            # Get total page count
            page_info = await get_current_page_info(sf_frame)
            total_pages = page_info['total']

            if end_page is None:
                end_page = total_pages

            print(f"       Total pages: {total_pages}")
            print(f"       Processing pages {start_page} to {end_page}")
            print(f"       Min date filter: {min_date}")
            print()

            # Navigate to start page if needed
            if start_page > 1:
                print(f"       Navigating to page {start_page}...")
                for _ in range(start_page - 1):
                    await navigate_to_next_page(sf_frame)

            # Process each page
            for page_num in range(start_page, end_page + 1):
                print(f"\n{'='*50}")
                print(f"PAGE {page_num}/{total_pages}")
                print('='*50)

                # Get all rows on current page
                rows = await get_all_rows_on_page(sf_frame)
                print(f"Found {len(rows)} documents on this page")

                # Process each row
                for record in rows:
                    stats['total_processed'] += 1
                    permit_id = record.get('permit_id', 'unknown')
                    doc_date = record.get('doc_date', 'unknown')

                    should_dl, reason = should_download(record, existing, min_date)

                    if not should_dl:
                        if 'already' in reason.lower():
                            stats['skipped_exists'] += 1
                        elif 'date' in reason.lower() or 'before' in reason.lower():
                            stats['skipped_date'] += 1
                        print(f"  SKIP: {permit_id} ({doc_date}) - {reason}")
                        continue

                    print(f"  {'[DRY RUN] ' if dry_run else ''}Downloading: {permit_id} ({doc_date})")

                    if dry_run:
                        stats['downloaded'] += 1
                        continue

                    # Attempt download with retries
                    success = False
                    for attempt in range(MAX_RETRIES):
                        try:
                            pdf_url = await click_view_button_for_row(sf_frame, context, record['row_index'])

                            if not pdf_url and pdf_urls:
                                pdf_url = pdf_urls[-1]
                                pdf_urls.clear()

                            if pdf_url:
                                output_path = await download_pdf(page, pdf_url, record)
                                if output_path:
                                    file_size = output_path.stat().st_size
                                    print(f"       SUCCESS: {output_path.name} ({file_size:,} bytes)")
                                    stats['downloaded'] += 1
                                    existing.add((permit_id, doc_date))
                                    success = True
                                    break
                            else:
                                print(f"       Attempt {attempt + 1}: No PDF URL found")

                        except Exception as e:
                            print(f"       Attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")

                        # Cleanup and retry
                        await cleanup_extra_tabs(context)
                        await asyncio.sleep(RATE_LIMIT_DELAY)

                    if not success:
                        stats['failed'] += 1
                        print(f"       FAILED: {permit_id} after {MAX_RETRIES} attempts")

                    # Rate limit between downloads
                    await asyncio.sleep(RATE_LIMIT_DELAY)

                # Navigate to next page if not on last
                if page_num < end_page:
                    print(f"\nNavigating to page {page_num + 1}...")
                    has_next = await navigate_to_next_page(sf_frame)
                    if not has_next:
                        print("Warning: Could not navigate to next page")
                        break

            return stats

        finally:
            await browser.close()


async def main():
    parser = argparse.ArgumentParser(description="Download NOI documents from Utah OGM")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument("--dry-run", action="store_true", help="List documents without downloading")
    parser.add_argument("--start-page", type=int, default=1, help="Start from specific page (default: 1)")
    parser.add_argument("--end-page", type=int, default=None, help="Stop at specific page (default: all)")
    parser.add_argument("--min-date", type=str, default="2020-01-01", help="Minimum date YYYY-MM-DD (default: 2020-01-01)")
    args = parser.parse_args()

    # Parse min date
    try:
        min_date = date.fromisoformat(args.min_date)
    except ValueError:
        print(f"Error: Invalid date format '{args.min_date}'. Use YYYY-MM-DD")
        return

    print("=" * 60)
    print("Utah OGM NOI Document Downloader")
    print("=" * 60)
    print(f"Mode:       {'Headless' if args.headless else 'Visible browser'}")
    print(f"Dry run:    {args.dry_run}")
    print(f"Pages:      {args.start_page} to {args.end_page or 'end'}")
    print(f"Min date:   {min_date}")
    print()

    try:
        stats = await download_all_nois(
            headless=args.headless,
            dry_run=args.dry_run,
            start_page=args.start_page,
            end_page=args.end_page,
            min_date=min_date
        )

        print()
        print("=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"Total processed:  {stats['total_processed']}")
        print(f"Downloaded:       {stats['downloaded']}")
        print(f"Skipped (exists): {stats['skipped_exists']}")
        print(f"Skipped (date):   {stats['skipped_date']}")
        print(f"Failed:           {stats['failed']}")

    except Exception as e:
        print()
        print("=" * 60)
        print("ERROR")
        print("=" * 60)
        print(f"{e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
