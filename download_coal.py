#!/usr/bin/env python3
"""
Utah OGM Coal Permit Files Downloader

Downloads coal permit files from ogm.utah.gov/coal-files/
Navigates through permits and downloads documents from 2020 onwards.

Usage:
    python download_coal.py                      # Download all (visible browser)
    python download_coal.py --headless           # Run headless
    python download_coal.py --dry-run            # List without downloading
    python download_coal.py --single-permit C0070001  # Process only one permit
    python download_coal.py --start-page 5       # Start from page 5
    python download_coal.py --min-year 2015      # Custom year filter
    python download_coal.py --workers 4          # Run 4 parallel browser instances
"""

import asyncio
import argparse
import math
import re
import requests
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright


@dataclass
class PermitWorkItem:
    """Represents a single permit to be processed by a worker."""
    permit_id: str
    mine_name: str
    page_num: int
    row_index: int


@dataclass
class SharedState:
    """Thread-safe shared state for workers."""
    existing: dict  # dict[str, set] - mine_name -> set of existing filenames
    downloaded: set  # Newly downloaded file paths
    downloaded_lock: asyncio.Lock
    stats: dict
    stats_lock: asyncio.Lock

# Configuration
OUTPUT_DIR = Path(__file__).parent / "output_coal"
PORTAL_URL = "https://ogm.utah.gov/coal-files/"
DEFAULT_MIN_YEAR = 2020
RATE_LIMIT_DELAY = 2.0  # seconds between downloads
MAX_RETRIES = 3


def sanitize_filename_part(text: str, max_len: int = 0) -> str:
    """Sanitize a string for use in filenames or directory names."""
    if not text:
        return ""
    safe = re.sub(r'[<>:"/\\|?*,.]', '_', text)
    safe = re.sub(r'\s+', '_', safe)
    safe = re.sub(r'_+', '_', safe)
    safe = safe.strip('_.')
    if max_len > 0:
        safe = safe[:max_len]
    return safe


def sanitize_mine_name(name: str) -> str:
    """Convert mine name to filesystem-safe directory name."""
    return sanitize_filename_part(name) or "unknown_mine"


def get_existing_coal_documents() -> dict[str, set]:
    """Scan output directory and return dict of mine_name -> set of filenames."""
    existing = {}

    if not OUTPUT_DIR.exists():
        return existing

    for mine_dir in OUTPUT_DIR.iterdir():
        if mine_dir.is_dir() and mine_dir.name != 'debug':
            existing[mine_dir.name] = {f.name for f in mine_dir.glob('*.pdf')}

    return existing


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


async def get_all_permits_on_page(sf_frame) -> list[dict]:
    """Extract all permit records from current page.

    Returns list of dicts with:
    - row_index: int
    - permit_id: str (e.g., "C0070001")
    - mine_name: str (for output directory)
    - operator: str
    """
    rows = await sf_frame.evaluate("""
        (() => {
            const records = [];

            // Get visible text and parse it
            const bodyText = document.body.innerText;
            const lines = bodyText.split('\\n').map(l => l.trim()).filter(l => l);

            // Coal permit IDs start with C or ACT followed by digits
            const permitPattern = /^(C|ACT)\\d+$/i;

            let rowIndex = 0;

            for (let i = 0; i < lines.length; i++) {
                const line = lines[i];

                // Permit ID found - look backwards for mine name (after SELECT)
                if (permitPattern.test(line)) {
                    let mineName = '';
                    let operator = '';
                    let county = '';

                    // Mine name is typically 1-2 lines before permit ID (after SELECT)
                    for (let j = i - 1; j >= Math.max(0, i - 3); j--) {
                        const prevLine = lines[j];
                        if (prevLine === 'SELECT' || prevLine === 'VIEW') {
                            break;
                        }
                        if (!permitPattern.test(prevLine) && prevLine.length > 2) {
                            mineName = prevLine;
                            break;
                        }
                    }

                    // County and operator are after permit ID
                    if (i + 1 < lines.length && !permitPattern.test(lines[i + 1])) {
                        county = lines[i + 1];
                    }
                    if (i + 2 < lines.length && !permitPattern.test(lines[i + 2]) && lines[i + 2] !== 'SELECT') {
                        operator = lines[i + 2];
                    }

                    records.push({
                        row_index: rowIndex++,
                        permit_id: line,
                        mine_name: mineName,
                        county: county,
                        operator: operator
                    });
                }
            }

            return records;
        })()
    """)
    return rows


async def sort_by_doc_year(sf_frame) -> bool:
    """Click on 'Doc Year' column header twice to sort descending (newest first).

    Returns True if successfully clicked sort.
    """
    try:
        doc_year_header = sf_frame.get_by_text("Doc Year", exact=False).first
        # Click twice: first to sort, second to sort descending
        await doc_year_header.click()
        await sf_frame.page.wait_for_timeout(2000)
        await doc_year_header.click()
        await sf_frame.page.wait_for_timeout(2000)
        return True
    except Exception as e:
        print(f"       Sort by Doc Year failed: {e}")
        return False


async def get_files_in_permit(sf_frame) -> list[dict]:
    """Extract all files from the current permit's file list view."""
    rows = await sf_frame.evaluate("""
        (() => {
            const records = [];
            const bodyText = document.body.innerText;
            const lines = bodyText.split('\\n').map(l => l.trim()).filter(l => l);

            // Patterns
            const permitPattern = /^C\\d+$/;  // Coal permit ID
            const yearPattern = /^(19|20)\\d{2}$/;
            const datePattern = /^\\d{4}-\\d{2}-\\d{2}$/;
            const locationPattern = /^(Outgoing|Incoming|Internal)$/;

            let rowIndex = 0;

            for (let i = 0; i < lines.length; i++) {
                const line = lines[i];

                // Start of a file record - detect by permit ID
                if (permitPattern.test(line)) {
                    let docYear = 0;
                    let fileDate = '';
                    let docLocation = '';
                    let docTo = '';
                    let docFrom = '';
                    let docRegarding = '';

                    // Parse next lines for file info
                    // Expected order: year, date, location, to, from, regarding
                    let fieldIndex = 0;
                    for (let j = i + 1; j < Math.min(i + 10, lines.length); j++) {
                        const nextLine = lines[j];

                        // Stop if we hit another permit ID (next record)
                        if (permitPattern.test(nextLine)) break;
                        // Skip header/footer lines
                        if (nextLine.match(/^(First|Previous|Next|Last|Showing|Sort by|Navigation Mode|VIEW)/)) continue;
                        if (nextLine === 'Sorted: None' || nextLine === 'Sorted Descending' || nextLine === 'Sorted Ascending') continue;

                        // Parse based on position and pattern
                        if (yearPattern.test(nextLine) && !docYear) {
                            docYear = parseInt(nextLine, 10);
                        } else if (datePattern.test(nextLine) && !fileDate) {
                            fileDate = nextLine;
                        } else if (locationPattern.test(nextLine) && !docLocation) {
                            docLocation = nextLine;
                        } else if (!docTo && docLocation && !docFrom) {
                            docTo = nextLine;
                        } else if (!docFrom && docTo) {
                            docFrom = nextLine;
                        } else if (!docRegarding && docFrom) {
                            docRegarding = nextLine;
                        }
                    }

                    // Only add if we have meaningful data
                    if (docYear || fileDate) {
                        records.push({
                            row_index: rowIndex++,
                            permit_id: line,
                            description: docRegarding || 'Document',
                            doc_year: docYear,
                            file_date: fileDate,
                            doc_location: docLocation,
                            doc_to: docTo,
                            doc_from: docFrom
                        });
                    }
                }
            }

            return records;
        })()
    """)
    return rows


async def click_permit_row(sf_frame, row_index: int) -> bool:
    """Click the button on a permit row to view its files.

    Returns True if successfully opened file list view.
    """
    try:
        await sf_frame.page.keyboard.press('Escape')
        await sf_frame.page.wait_for_timeout(500)
    except Exception:
        pass

    row_selector = f'tr[data-row-key-value="row-{row_index}"]:visible'

    # Try hover + click first (most reliable for Salesforce hint-parent pattern)
    try:
        row = sf_frame.locator(row_selector).first
        await row.hover()
        await sf_frame.page.wait_for_timeout(500)
        view_button = row.locator('lightning-button-icon').first
        await view_button.click(force=True)
        await sf_frame.page.wait_for_timeout(3000)
        return True
    except Exception as e:
        print(f"       Hover+click failed: {e}")

    # Fallback: JavaScript click
    try:
        result = await sf_frame.evaluate(f"""
            (() => {{
                const row = document.querySelector('tr[data-row-key-value="row-{row_index}"]');
                if (row) {{
                    const btn = row.querySelector('lightning-button-icon');
                    if (btn) {{
                        btn.click();
                        return true;
                    }}
                }}
                return false;
            }})()
        """)
        if result:
            await sf_frame.page.wait_for_timeout(3000)
            return True
    except Exception as e:
        print(f"       JS click failed: {e}")

    return False


async def click_file_and_get_url(sf_frame, context, file_row_index: int) -> Optional[str]:
    """Click on a file row to get PDF URL.

    Returns the PDF URL if successful, None otherwise.
    """
    try:
        await sf_frame.page.keyboard.press('Escape')
        await sf_frame.page.wait_for_timeout(500)
    except Exception:
        pass

    row_selector = f'tr[data-row-key-value="row-{file_row_index}"]:visible'
    captured_pdf_url = None

    # Set up listener to capture PDF URLs from new page requests
    def create_request_handler():
        nonlocal captured_pdf_url
        def on_request(request):
            nonlocal captured_pdf_url
            url = request.url
            if '.pdf' in url.lower() or 's3.amazonaws' in url.lower():
                captured_pdf_url = url
        return on_request

    def on_new_page(new_page):
        new_page.on('request', create_request_handler())

    context.on('page', on_new_page)

    # Try hover + click to open new page
    try:
        row = sf_frame.locator(row_selector).first
        await row.hover()
        await sf_frame.page.wait_for_timeout(500)
        view_button = row.locator('lightning-button-icon').first

        async with context.expect_page(timeout=180000) as new_page_info:
            await view_button.click(force=True)

        new_page = await new_page_info.value
        await sf_frame.page.wait_for_timeout(2000)

        pdf_url = captured_pdf_url or new_page.url
        if pdf_url and pdf_url.startswith('http'):
            return pdf_url
    except Exception as e:
        print(f"       Click failed: {e}")

    # Fallback: check for any new tabs that may have opened
    await sf_frame.page.wait_for_timeout(2000)
    all_pages = context.pages
    if len(all_pages) > 1:
        url = all_pages[-1].url
        if url and url.startswith('http'):
            return url

    return None


async def download_coal_file(pdf_url: str, mine_name: str, file_info: dict) -> Optional[Path]:
    """Download PDF and save to output/{mine_name}/ directory."""
    safe_name = sanitize_mine_name(mine_name)
    mine_dir = OUTPUT_DIR / safe_name
    mine_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename from file_info
    file_date = file_info.get('file_date', 'unknown')
    description = file_info.get('description', 'Document')
    doc_from = file_info.get('doc_from', '')
    doc_to = file_info.get('doc_to', '')

    parts = [file_date]
    if description:
        parts.append(sanitize_filename_part(description, 40))
    if doc_from:
        parts.append(f"from_{sanitize_filename_part(doc_from, 20)}")
    if doc_to:
        parts.append(f"to_{sanitize_filename_part(doc_to, 20)}")

    base_filename = "_".join(parts)
    if len(base_filename) > 180:
        base_filename = base_filename[:180]

    filename = f"{base_filename}.pdf"
    output_path = mine_dir / filename

    counter = 1
    while output_path.exists():
        filename = f"{base_filename}_{counter}.pdf"
        output_path = mine_dir / filename
        counter += 1

    try:
        response = requests.get(pdf_url, timeout=120, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        })
        response.raise_for_status()
        content_bytes = response.content

        # Verify it's a PDF
        if content_bytes[:4] == b'%PDF':
            output_path.write_bytes(content_bytes)
            return output_path
        else:
            output_path.write_bytes(content_bytes)
            print(f"       Warning: Content may not be PDF (first bytes: {content_bytes[:10]})")
            return output_path
    except requests.RequestException as e:
        print(f"       Download error: {e}")
        return None


async def cleanup_extra_tabs(context):
    """Close any tabs beyond the main page."""
    pages = context.pages
    if len(pages) > 1:
        for page in pages[1:]:
            await page.close()


async def navigate_back_to_permit_list(sf_frame) -> bool:
    """Navigate back from file list to permit list by switching tabs.

    Returns True if successful.
    """
    # Close any open panels first
    try:
        await sf_frame.page.keyboard.press('Escape')
        await sf_frame.page.wait_for_timeout(500)
    except Exception:
        pass

    # Switch to General Files tab, then back to Permit Files to reset the view
    try:
        general_files_tab = sf_frame.get_by_text("General Files", exact=True)
        await general_files_tab.click()
        await sf_frame.page.wait_for_timeout(2000)

        permit_files_tab = sf_frame.get_by_text("Permit Files", exact=True)
        await permit_files_tab.click()
        await sf_frame.page.wait_for_timeout(3000)

        body_text = await sf_frame.evaluate("() => document.body.innerText")
        if ">C" not in body_text[:1000] and "MINE NAME" in body_text:
            return True
    except Exception as e:
        print(f"       Tab switch navigation failed: {e}")

    # Fallback: click Permit Files tab directly via JavaScript
    try:
        await sf_frame.evaluate("""
            (() => {
                const elements = document.querySelectorAll('*');
                for (const el of elements) {
                    if (el.textContent.trim() === 'Permit Files' && el.offsetParent !== null) {
                        el.click();
                        return true;
                    }
                }
                return false;
            })()
        """)
        await sf_frame.page.wait_for_timeout(3000)
        return True
    except Exception as e:
        print(f"       Fallback navigation failed: {e}")

    return False


async def navigate_to_next_page(sf_frame) -> bool:
    """Click Next button and wait for table to update. Returns False if on last page."""
    page_info = await get_current_page_info(sf_frame)

    if page_info['current'] >= page_info['total']:
        return False

    # Close any open detail panels
    try:
        await sf_frame.page.keyboard.press('Escape')
        await sf_frame.page.wait_for_timeout(500)
    except Exception:
        pass

    # Click Permit Files tab to ensure it's focused
    try:
        permit_files_tab = sf_frame.get_by_text("Permit Files", exact=True)
        await permit_files_tab.click()
        await sf_frame.page.wait_for_timeout(2000)
    except Exception:
        pass

    page_info = await get_current_page_info(sf_frame)

    # Strategy 1: JavaScript click with Shadow DOM traversal
    try:
        result = await sf_frame.evaluate("""
            (() => {
                function findElementsDeep(selector, root = document, results = [], depth = 0) {
                    if (depth > 20) return results;
                    const elements = root.querySelectorAll(selector);
                    results.push(...elements);
                    const allElements = root.querySelectorAll('*');
                    for (const el of allElements) {
                        if (el.shadowRoot) {
                            findElementsDeep(selector, el.shadowRoot, results, depth + 1);
                        }
                    }
                    return results;
                }

                const buttons = findElementsDeep('button');
                const nextButtons = buttons.filter(btn => btn.textContent.trim() === 'Next');

                for (const btn of nextButtons) {
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

    # Strategy 2: Playwright dispatch_event
    try:
        next_buttons = sf_frame.get_by_role("button", name="Next")
        count = await next_buttons.count()

        if count > 0:
            await next_buttons.first.dispatch_event('click')
            await sf_frame.page.wait_for_timeout(4000)
            new_page_info = await get_current_page_info(sf_frame)
            if new_page_info['current'] > page_info['current']:
                return True

    except Exception:
        pass

    # Strategy 3: Element handle evaluation
    try:
        next_button = sf_frame.locator('button:has-text("Next")').first
        await next_button.evaluate("el => el.click()")
        await sf_frame.page.wait_for_timeout(4000)
        new_page_info = await get_current_page_info(sf_frame)
        return new_page_info['current'] > page_info['current']

    except Exception as e:
        print(f"       Navigation error: {e}")
        return False


async def navigate_to_page(sf_frame, target_page: int, current_page: int) -> int:
    """Navigate from current_page to target_page.

    If target < current, must reload and navigate from page 1.
    Returns the actual page number after navigation.
    """
    if target_page == current_page:
        return current_page

    if target_page < current_page:
        # Must reload and start from page 1
        await sf_frame.page.reload(wait_until="networkidle")
        await sf_frame.page.wait_for_timeout(5000)

        # Re-click Permit Files tab
        try:
            permit_files_tab = sf_frame.get_by_text("Permit Files", exact=True)
            await permit_files_tab.wait_for(state="visible", timeout=30000)
            await permit_files_tab.click()
        except Exception:
            await sf_frame.evaluate("""
                (() => {
                    const tabs = document.querySelectorAll('*');
                    for (const tab of tabs) {
                        if (tab.textContent.trim() === 'Permit Files' && tab.offsetParent !== null) {
                            tab.click();
                            return true;
                        }
                    }
                    return false;
                })()
            """)
        await sf_frame.page.wait_for_timeout(3000)
        current_page = 1

    # Navigate forward to target
    while current_page < target_page:
        success = await navigate_to_next_page(sf_frame)
        if success:
            current_page += 1
        else:
            print(f"       Failed to navigate from page {current_page} to {target_page}")
            break

    return current_page


async def get_salesforce_frame(page):
    """Find and return the Salesforce iframe."""
    frames = page.frames
    sf_frame = None
    for frame in frames:
        frame_url = frame.url
        if "site.com" in frame_url.lower() or "salesforce" in frame_url.lower():
            sf_frame = frame
            break
    if sf_frame is None and len(frames) > 1:
        sf_frame = frames[1]
    return sf_frame


async def setup_permit_files_tab(sf_frame):
    """Click on Permit Files tab and wait for it to load."""
    try:
        permit_files_tab = sf_frame.get_by_text("Permit Files", exact=True)
        await permit_files_tab.wait_for(state="visible", timeout=60000)
        await permit_files_tab.click()
    except Exception:
        await sf_frame.evaluate("""
            (() => {
                const tabs = document.querySelectorAll('*');
                for (const tab of tabs) {
                    if (tab.textContent.trim() === 'Permit Files' && tab.offsetParent !== null) {
                        tab.click();
                        return true;
                    }
                }
                return false;
            })()
        """)
    await sf_frame.page.wait_for_timeout(5000)


async def discover_all_permits(headless: bool = True) -> list:
    """Phase 1: Scan all pages and collect permit metadata.

    Returns list of PermitWorkItem objects for all permits found.
    """
    permits = []

    async with async_playwright() as p:
        browser_args = ['--disable-blink-features=AutomationControlled']
        browser = await p.chromium.launch(
            headless=headless,
            channel="chrome",
            args=browser_args
        )
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()

        try:
            print("  Navigating to coal-files page...")
            await page.goto(PORTAL_URL, wait_until="networkidle", timeout=60000)
            await page.wait_for_selector("iframe", timeout=30000)
            await page.wait_for_timeout(5000)

            sf_frame = await get_salesforce_frame(page)
            if sf_frame is None:
                raise Exception("Could not find Salesforce iframe")

            await setup_permit_files_tab(sf_frame)

            page_info = await get_current_page_info(sf_frame)
            total_pages = page_info['total']
            print(f"  Total pages to scan: {total_pages}")

            current_page = 1
            while current_page <= total_pages:
                print(f"  Scanning page {current_page}/{total_pages}...")

                page_permits = await get_all_permits_on_page(sf_frame)

                for permit in page_permits:
                    permits.append(PermitWorkItem(
                        permit_id=permit.get('permit_id', 'unknown'),
                        mine_name=permit.get('mine_name', 'unknown'),
                        page_num=current_page,
                        row_index=permit.get('row_index', 0)
                    ))

                if current_page < total_pages:
                    success = await navigate_to_next_page(sf_frame)
                    if success:
                        current_page += 1
                    else:
                        print(f"  Warning: Could not navigate past page {current_page}")
                        break
                else:
                    break

            print(f"  Discovery complete: {len(permits)} permits found")

        finally:
            await browser.close()

    return permits


async def process_permit(
    worker_id: int,
    sf_frame,
    context,
    permit: PermitWorkItem,
    shared: SharedState,
    min_year: int,
    dry_run: bool
) -> dict:
    """Process a single permit: click into it, get files, download matching ones.

    Returns dict with counts: files_found, downloaded, skipped_exists, skipped_date, failed
    """
    prefix = f"[W{worker_id}]"
    local_stats = {
        'files_found': 0,
        'downloaded': 0,
        'skipped_exists': 0,
        'skipped_date': 0,
        'failed': 0
    }

    # Click permit row
    if not await click_permit_row(sf_frame, permit.row_index):
        print(f"{prefix}       FAILED to open permit {permit.permit_id}")
        local_stats['failed'] += 1
        return local_stats

    await sort_by_doc_year(sf_frame)

    files = await get_files_in_permit(sf_frame)
    local_stats['files_found'] = len(files)
    print(f"{prefix}       Found {len(files)} files in permit")

    safe_mine_name = sanitize_mine_name(permit.mine_name)
    existing_files = shared.existing.get(safe_mine_name, set())

    for file_info in files:
        description = file_info.get('description', 'Document')
        file_date = file_info.get('file_date', '')

        should_dl, reason = should_download_file(file_info, existing_files, min_year)

        if not should_dl:
            if 'already' in reason.lower():
                local_stats['skipped_exists'] += 1
            elif 'year' in reason.lower():
                local_stats['skipped_date'] += 1
            print(f"{prefix}       SKIP: {description[:40]} ({file_date}) - {reason}")
            continue

        print(f"{prefix}       {'[DRY RUN] ' if dry_run else ''}Downloading: {description[:40]} ({file_date})")

        if dry_run:
            local_stats['downloaded'] += 1
            continue

        success = False
        for attempt in range(MAX_RETRIES):
            try:
                pdf_url = await click_file_and_get_url(sf_frame, context, file_info['row_index'])

                if pdf_url and pdf_url.startswith('http'):
                    output_path = await download_coal_file(pdf_url, permit.mine_name, file_info)
                    if output_path:
                        file_size = output_path.stat().st_size
                        print(f"{prefix}         SUCCESS: {output_path.name} ({file_size:,} bytes)")
                        local_stats['downloaded'] += 1
                        existing_files.add(output_path.name)

                        # Mark as downloaded in shared state
                        async with shared.downloaded_lock:
                            shared.downloaded.add(f"{safe_mine_name}/{output_path.name}")

                        success = True
                        break
                else:
                    print(f"{prefix}         Attempt {attempt + 1}: No PDF URL found")

            except Exception as e:
                print(f"{prefix}         Attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")

            await cleanup_extra_tabs(context)
            await asyncio.sleep(RATE_LIMIT_DELAY)

        if not success:
            local_stats['failed'] += 1
            print(f"{prefix}         FAILED: {description[:40]} after {MAX_RETRIES} attempts")

        await asyncio.sleep(RATE_LIMIT_DELAY)

    await navigate_back_to_permit_list(sf_frame)
    await cleanup_extra_tabs(context)

    return local_stats


async def queue_download_worker(
    worker_id: int,
    permit_queue: asyncio.Queue,
    shared: SharedState,
    headless: bool,
    dry_run: bool,
    min_year: int
) -> None:
    """Queue-based download worker that pulls permits from shared queue."""
    prefix = f"[W{worker_id}]"
    current_page = 0  # Not yet on any page

    async with async_playwright() as p:
        browser_args = ['--disable-blink-features=AutomationControlled']
        if headless:
            browser_args.append('--headless=new')

        browser = await p.chromium.launch(
            headless=headless,
            channel="chrome",
            args=browser_args
        )
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()

        try:
            print(f"{prefix} Starting worker, navigating to portal...")
            await page.goto(PORTAL_URL, wait_until="networkidle", timeout=60000)
            await page.wait_for_selector("iframe", timeout=30000)
            await page.wait_for_timeout(5000)

            sf_frame = await get_salesforce_frame(page)
            if sf_frame is None:
                raise Exception(f"{prefix} Could not find Salesforce iframe")

            await setup_permit_files_tab(sf_frame)
            current_page = 1

            while True:
                # Try to get next permit from queue
                try:
                    permit = await asyncio.wait_for(
                        permit_queue.get(),
                        timeout=10.0
                    )
                except asyncio.TimeoutError:
                    if permit_queue.empty():
                        print(f"{prefix} Queue empty, worker finishing")
                        break
                    continue

                print(f"\n{prefix}   PERMIT: {permit.permit_id} - {permit.mine_name} (page {permit.page_num})")

                # Navigate to correct page if needed
                if permit.page_num != current_page:
                    print(f"{prefix}     Navigating from page {current_page} to {permit.page_num}...")
                    current_page = await navigate_to_page(sf_frame, permit.page_num, current_page)

                # Process the permit
                local_stats = await process_permit(
                    worker_id, sf_frame, context, permit,
                    shared, min_year, dry_run
                )

                # Update shared stats atomically
                async with shared.stats_lock:
                    shared.stats['permits_processed'] = shared.stats.get('permits_processed', 0) + 1
                    shared.stats['total_files_found'] = shared.stats.get('total_files_found', 0) + local_stats['files_found']
                    shared.stats['downloaded'] = shared.stats.get('downloaded', 0) + local_stats['downloaded']
                    shared.stats['skipped_exists'] = shared.stats.get('skipped_exists', 0) + local_stats['skipped_exists']
                    shared.stats['skipped_date'] = shared.stats.get('skipped_date', 0) + local_stats['skipped_date']
                    shared.stats['failed'] = shared.stats.get('failed', 0) + local_stats['failed']

                permit_queue.task_done()

        except Exception as e:
            print(f"{prefix} Worker error: {e}")
        finally:
            await browser.close()
            print(f"{prefix} Worker shut down")


async def download_queue_parallel(
    num_workers: int = 4,
    headless: bool = True,
    dry_run: bool = False,
    min_year: int = 2020,
    single_permit: Optional[str] = None
) -> dict:
    """Queue-based parallel downloading - main entry point.

    Phase 1: Discovery - collect all permits
    Phase 2: Queue-based download - workers pull from shared queue
    """
    print()
    print("=" * 60)
    print("PHASE 1: Discovery")
    print("=" * 60)

    # Discover all permits
    all_permits = await discover_all_permits(headless=headless)
    print(f"Discovered {len(all_permits)} permits across all pages")

    # Filter for single permit if specified
    if single_permit:
        all_permits = [p for p in all_permits if p.permit_id == single_permit]
        print(f"Filtered to {len(all_permits)} permits matching {single_permit}")

    if not all_permits:
        return {
            'permits_processed': 0,
            'total_files_found': 0,
            'downloaded': 0,
            'skipped_exists': 0,
            'skipped_date': 0,
            'failed': 0
        }

    # Sort by page for efficient navigation
    all_permits.sort(key=lambda p: (p.page_num, p.row_index))

    # Get existing documents
    existing = get_existing_coal_documents()
    total_existing = sum(len(files) for files in existing.values())
    print(f"Found {total_existing} existing documents across {len(existing)} mines")

    # Create shared state
    shared = SharedState(
        existing=existing,
        downloaded=set(),
        downloaded_lock=asyncio.Lock(),
        stats={
            'permits_processed': 0,
            'total_files_found': 0,
            'downloaded': 0,
            'skipped_exists': 0,
            'skipped_date': 0,
            'failed': 0
        },
        stats_lock=asyncio.Lock()
    )

    # Populate queue
    permit_queue = asyncio.Queue()
    for permit in all_permits:
        await permit_queue.put(permit)

    print()
    print("=" * 60)
    print("PHASE 2: Queue-Based Download")
    print("=" * 60)
    print(f"Starting {num_workers} workers for {len(all_permits)} permits")
    print()

    # Launch workers
    workers = [
        queue_download_worker(
            worker_id=i,
            permit_queue=permit_queue,
            shared=shared,
            headless=headless,
            dry_run=dry_run,
            min_year=min_year
        )
        for i in range(num_workers)
    ]

    # Wait for all workers to complete
    await asyncio.gather(*workers, return_exceptions=True)

    return shared.stats


def should_download_file(file_info: dict, existing_files: set, min_year: int) -> tuple[bool, str]:
    """Determine if a file should be downloaded. Returns (should_download, reason)."""
    doc_year = file_info.get('doc_year', 0)
    file_date = file_info.get('file_date', '')
    description = file_info.get('description', '')
    doc_from = file_info.get('doc_from', '')
    doc_to = file_info.get('doc_to', '')

    # Generate expected filename for deduplication check (matches download_coal_file logic)
    parts = [file_date]
    if description:
        parts.append(sanitize_filename_part(description, 40))
    if doc_from:
        parts.append(f"from_{sanitize_filename_part(doc_from, 20)}")
    if doc_to:
        parts.append(f"to_{sanitize_filename_part(doc_to, 20)}")

    base_filename = "_".join(parts)
    if len(base_filename) > 180:
        base_filename = base_filename[:180]

    expected_filename = f"{base_filename}.pdf"

    if expected_filename in existing_files:
        return False, "Already downloaded"
    for i in range(1, 10):
        if f"{base_filename}_{i}.pdf" in existing_files:
            return False, "Already downloaded"

    if doc_year and doc_year < min_year:
        return False, f"Doc year {doc_year} is before {min_year}"

    return True, "OK"


async def get_total_pages(headless: bool = True) -> int:
    """Quick probe to get total page count from the portal."""
    async with async_playwright() as p:
        browser_args = ['--disable-blink-features=AutomationControlled']
        browser = await p.chromium.launch(
            headless=headless,
            channel="chrome",
            args=browser_args
        )
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()

        try:
            await page.goto(PORTAL_URL, wait_until="networkidle", timeout=60000)
            await page.wait_for_selector("iframe", timeout=30000)
            await page.wait_for_timeout(5000)

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

            permit_files_tab = sf_frame.get_by_text("Permit Files", exact=True)
            await permit_files_tab.wait_for(state="visible", timeout=60000)
            await permit_files_tab.click()
            await page.wait_for_timeout(5000)

            page_info = await get_current_page_info(sf_frame)
            return page_info['total']

        finally:
            await browser.close()


async def download_worker(
    worker_id: int,
    start_page: int,
    end_page: int,
    headless: bool,
    dry_run: bool,
    min_year: int,
    existing: dict,
    single_permit: Optional[str] = None
) -> dict:
    """Independent worker that processes a range of pages."""
    prefix = f"[W{worker_id}]"
    stats = {
        'permits_processed': 0,
        'total_files_found': 0,
        'downloaded': 0,
        'skipped_exists': 0,
        'skipped_date': 0,
        'failed': 0
    }

    async with async_playwright() as p:
        browser_args = ['--disable-blink-features=AutomationControlled']
        if headless:
            browser_args.append('--headless=new')

        browser = await p.chromium.launch(
            headless=headless,
            channel="chrome",
            args=browser_args
        )
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()

        try:
            print(f"{prefix} Navigating to coal-files page...")
            await page.goto(PORTAL_URL, wait_until="networkidle", timeout=60000)

            await page.wait_for_selector("iframe", timeout=30000)
            await page.wait_for_timeout(5000)

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
                raise Exception(f"{prefix} Could not find Salesforce iframe")

            print(f"{prefix} Clicking Permit Files tab...")
            try:
                permit_files_tab = sf_frame.get_by_text("Permit Files", exact=True)
                await permit_files_tab.wait_for(state="visible", timeout=60000)
                await permit_files_tab.click()
            except Exception as e:
                print(f"{prefix} Tab click via locator failed: {e}")
                await sf_frame.evaluate("""
                    (() => {
                        const tabs = document.querySelectorAll('*');
                        for (const tab of tabs) {
                            if (tab.textContent.trim() === 'Permit Files' && tab.offsetParent !== null) {
                                tab.click();
                                return true;
                            }
                        }
                        return false;
                    })()
                """)

            await page.wait_for_timeout(5000)

            # Navigate to start_page if not page 1
            if start_page > 1:
                print(f"{prefix} Navigating to page {start_page}...")
                for _ in range(start_page - 1):
                    await navigate_to_next_page(sf_frame)

            print(f"{prefix} Processing pages {start_page} to {end_page}")

            for page_num in range(start_page, end_page + 1):
                print(f"\n{prefix} {'='*40}")
                print(f"{prefix} PAGE {page_num}")
                print(f"{prefix} {'='*40}")

                permits = await get_all_permits_on_page(sf_frame)
                print(f"{prefix} Found {len(permits)} permits on this page")

                for permit in permits:
                    permit_id = permit.get('permit_id', 'unknown')
                    mine_name = permit.get('mine_name', 'unknown')

                    if single_permit and permit_id != single_permit:
                        continue

                    stats['permits_processed'] += 1
                    safe_mine_name = sanitize_mine_name(mine_name)
                    existing_files = existing.get(safe_mine_name, set())

                    print(f"\n{prefix}   PERMIT: {permit_id} - {mine_name}")

                    if not await click_permit_row(sf_frame, permit['row_index']):
                        print(f"{prefix}       FAILED to open permit {permit_id}")
                        stats['failed'] += 1
                        continue

                    await sort_by_doc_year(sf_frame)

                    files = await get_files_in_permit(sf_frame)
                    stats['total_files_found'] += len(files)
                    print(f"{prefix}       Found {len(files)} files in permit")

                    for file_info in files:
                        description = file_info.get('description', 'Document')
                        doc_year = file_info.get('doc_year', 0)
                        file_date = file_info.get('file_date', '')

                        should_dl, reason = should_download_file(file_info, existing_files, min_year)

                        if not should_dl:
                            if 'already' in reason.lower():
                                stats['skipped_exists'] += 1
                            elif 'year' in reason.lower():
                                stats['skipped_date'] += 1
                            print(f"{prefix}       SKIP: {description[:40]} ({file_date}) - {reason}")
                            continue

                        print(f"{prefix}       {'[DRY RUN] ' if dry_run else ''}Downloading: {description[:40]} ({file_date})")

                        if dry_run:
                            stats['downloaded'] += 1
                            continue

                        success = False

                        for attempt in range(MAX_RETRIES):
                            try:
                                pdf_url = await click_file_and_get_url(sf_frame, context, file_info['row_index'])

                                if pdf_url and pdf_url.startswith('http'):
                                    output_path = await download_coal_file(pdf_url, mine_name, file_info)
                                    if output_path:
                                        file_size = output_path.stat().st_size
                                        print(f"{prefix}         SUCCESS: {output_path.name} ({file_size:,} bytes)")
                                        stats['downloaded'] += 1
                                        existing_files.add(output_path.name)
                                        success = True
                                        break
                                else:
                                    print(f"{prefix}         Attempt {attempt + 1}: No PDF URL found")

                            except Exception as e:
                                print(f"{prefix}         Attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")

                            await cleanup_extra_tabs(context)
                            await asyncio.sleep(RATE_LIMIT_DELAY)

                        if not success:
                            stats['failed'] += 1
                            print(f"{prefix}         FAILED: {description[:40]} after {MAX_RETRIES} attempts")

                        await asyncio.sleep(RATE_LIMIT_DELAY)

                    await navigate_back_to_permit_list(sf_frame)
                    await cleanup_extra_tabs(context)

                    if single_permit and permit_id == single_permit:
                        print(f"\n{prefix} Single permit {single_permit} processed. Stopping.")
                        return stats

                if page_num < end_page:
                    print(f"\n{prefix} Navigating to page {page_num + 1}...")
                    has_next = await navigate_to_next_page(sf_frame)
                    if not has_next:
                        print(f"{prefix} Warning: Could not navigate to next page")
                        break

            return stats

        finally:
            await browser.close()


async def download_parallel(
    num_workers: int = 4,
    headless: bool = True,
    dry_run: bool = False,
    min_year: int = 2020,
    single_permit: Optional[str] = None
) -> dict:
    """Launch multiple workers to download in parallel."""
    print(f"Probing portal for total page count...")
    total_pages = await get_total_pages(headless=headless)
    print(f"Total pages: {total_pages}")

    # Adjust workers if more than pages
    actual_workers = min(num_workers, total_pages)
    if actual_workers < num_workers:
        print(f"Reducing workers from {num_workers} to {actual_workers} (only {total_pages} pages)")

    pages_per_worker = math.ceil(total_pages / actual_workers)

    existing = get_existing_coal_documents()
    total_existing = sum(len(files) for files in existing.values())
    print(f"Found {total_existing} existing documents across {len(existing)} mines")

    print(f"\nLaunching {actual_workers} parallel workers...")
    print("=" * 60)

    tasks = []
    for i in range(actual_workers):
        start = i * pages_per_worker + 1
        end = min((i + 1) * pages_per_worker, total_pages)

        print(f"  Worker {i}: pages {start}-{end}")

        task = download_worker(
            worker_id=i,
            start_page=start,
            end_page=end,
            headless=headless,
            dry_run=dry_run,
            min_year=min_year,
            existing=existing,
            single_permit=single_permit
        )
        tasks.append(task)

    print("=" * 60)
    print()

    # Run all workers concurrently
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Aggregate stats
    total_stats = {
        'permits_processed': 0,
        'total_files_found': 0,
        'downloaded': 0,
        'skipped_exists': 0,
        'skipped_date': 0,
        'failed': 0
    }

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            print(f"Worker {i} failed with error: {result}")
            continue
        for key in total_stats:
            total_stats[key] += result.get(key, 0)

    return total_stats


async def download_all_coal_files(
    headless: bool = False,
    dry_run: bool = False,
    start_page: int = 1,
    end_page: Optional[int] = None,
    min_year: int = 2020,
    single_permit: Optional[str] = None
):
    """Main function to download all coal permit files."""

    existing = get_existing_coal_documents()
    total_existing = sum(len(files) for files in existing.values())
    print(f"Found {total_existing} existing documents across {len(existing)} mines")

    stats = {
        'permits_processed': 0,
        'total_files_found': 0,
        'downloaded': 0,
        'skipped_exists': 0,
        'skipped_date': 0,
        'failed': 0
    }

    async with async_playwright() as p:
        browser_args = [
            '--disable-blink-features=AutomationControlled',
        ]
        if headless:
            browser_args.append('--headless=new')

        browser = await p.chromium.launch(
            headless=headless,
            channel="chrome",
            args=browser_args
        )
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()

        try:
            print("[1/4] Navigating to coal-files page...")
            await page.goto(PORTAL_URL, wait_until="networkidle", timeout=60000)

            print("[2/4] Waiting for iframe to load...")
            await page.wait_for_selector("iframe", timeout=30000)
            await page.wait_for_timeout(5000)

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

            print("[3/4] Clicking Permit Files tab...")
            try:
                permit_files_tab = sf_frame.get_by_text("Permit Files", exact=True)
                await permit_files_tab.wait_for(state="visible", timeout=60000)
                await permit_files_tab.click()
            except Exception as e:
                print(f"       Permit Files tab click via locator failed: {e}")
                await sf_frame.evaluate("""
                    (() => {
                        const tabs = document.querySelectorAll('*');
                        for (const tab of tabs) {
                            if (tab.textContent.trim() === 'Permit Files' && tab.offsetParent !== null) {
                                tab.click();
                                return true;
                            }
                        }
                        return false;
                    })()
                """)

            await page.wait_for_timeout(5000)

            debug_dir = OUTPUT_DIR / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            body_text = await sf_frame.evaluate("() => document.body.innerText")
            content = await sf_frame.content()
            (debug_dir / "coal_body_text.txt").write_text(body_text)
            (debug_dir / "coal_content.html").write_text(content)
            await page.screenshot(path=str(debug_dir / "coal_page.png"))
            print("       Debug files saved to output_coal/debug/")

            print("[4/4] Processing permits...")

            page_info = await get_current_page_info(sf_frame)
            total_pages = page_info['total']

            if end_page is None:
                end_page = total_pages

            print(f"       Total pages: {total_pages}")
            print(f"       Processing pages {start_page} to {end_page}")
            print(f"       Min year filter: {min_year}")
            if single_permit:
                print(f"       Single permit mode: {single_permit}")
            print()

            if start_page > 1:
                print(f"       Navigating to page {start_page}...")
                for _ in range(start_page - 1):
                    await navigate_to_next_page(sf_frame)

            for page_num in range(start_page, end_page + 1):
                print(f"\n{'='*50}")
                print(f"PAGE {page_num}/{total_pages}")
                print('='*50)

                permits = await get_all_permits_on_page(sf_frame)
                print(f"Found {len(permits)} permits on this page")

                for permit in permits:
                    permit_id = permit.get('permit_id', 'unknown')
                    mine_name = permit.get('mine_name', 'unknown')

                    if single_permit and permit_id != single_permit:
                        continue

                    stats['permits_processed'] += 1
                    safe_mine_name = sanitize_mine_name(mine_name)
                    existing_files = existing.get(safe_mine_name, set())

                    print(f"\n  PERMIT: {permit_id} - {mine_name}")

                    if not await click_permit_row(sf_frame, permit['row_index']):
                        print(f"       FAILED to open permit {permit_id}")
                        stats['failed'] += 1
                        continue

                    await sort_by_doc_year(sf_frame)

                    file_debug_dir = OUTPUT_DIR / "debug" / f"permit_{permit_id}"
                    file_debug_dir.mkdir(parents=True, exist_ok=True)
                    file_body_text = await sf_frame.evaluate("() => document.body.innerText")
                    (file_debug_dir / "files_body_text.txt").write_text(file_body_text)

                    files = await get_files_in_permit(sf_frame)
                    stats['total_files_found'] += len(files)
                    print(f"       Found {len(files)} files in permit")

                    for file_info in files:
                        description = file_info.get('description', 'Document')
                        doc_year = file_info.get('doc_year', 0)
                        file_date = file_info.get('file_date', '')

                        should_dl, reason = should_download_file(file_info, existing_files, min_year)

                        if not should_dl:
                            if 'already' in reason.lower():
                                stats['skipped_exists'] += 1
                            elif 'year' in reason.lower():
                                stats['skipped_date'] += 1
                            print(f"       SKIP: {description[:40]} ({file_date}) - {reason}")
                            continue

                        print(f"       {'[DRY RUN] ' if dry_run else ''}Downloading: {description[:40]} ({file_date})")

                        if dry_run:
                            stats['downloaded'] += 1
                            continue

                        success = False

                        for attempt in range(MAX_RETRIES):
                            try:
                                pdf_url = await click_file_and_get_url(sf_frame, context, file_info['row_index'])

                                # Only proceed if we got a valid URL from this specific click
                                # Do NOT use fallback URLs - they may be from previous files
                                if pdf_url and pdf_url.startswith('http'):
                                    output_path = await download_coal_file(pdf_url, mine_name, file_info)
                                    if output_path:
                                        file_size = output_path.stat().st_size
                                        print(f"         SUCCESS: {output_path.name} ({file_size:,} bytes)")
                                        stats['downloaded'] += 1
                                        existing_files.add(output_path.name)
                                        success = True
                                        break
                                else:
                                    print(f"         Attempt {attempt + 1}: No PDF URL found")

                            except Exception as e:
                                print(f"         Attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")

                            await cleanup_extra_tabs(context)
                            await asyncio.sleep(RATE_LIMIT_DELAY)

                        if not success:
                            stats['failed'] += 1
                            print(f"         FAILED: {description[:40]} after {MAX_RETRIES} attempts")

                        await asyncio.sleep(RATE_LIMIT_DELAY)

                    await navigate_back_to_permit_list(sf_frame)
                    await cleanup_extra_tabs(context)

                    if single_permit and permit_id == single_permit:
                        print(f"\nSingle permit {single_permit} processed. Stopping.")
                        return stats

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
    parser = argparse.ArgumentParser(description="Download coal permit files from Utah OGM")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument("--dry-run", action="store_true", help="List documents without downloading")
    parser.add_argument("--start-page", type=int, default=1, help="Start from specific page (default: 1)")
    parser.add_argument("--end-page", type=int, default=None, help="Stop at specific page (default: all)")
    parser.add_argument("--min-year", type=int, default=DEFAULT_MIN_YEAR, help=f"Minimum document year (default: {DEFAULT_MIN_YEAR})")
    parser.add_argument("--single-permit", type=str, default=None, help="Process only this permit ID (for testing)")
    parser.add_argument("--workers", type=int, default=1, help="Number of parallel browser instances (default: 1)")
    args = parser.parse_args()

    print("=" * 60)
    print("Utah OGM Coal Permit Files Downloader")
    print("=" * 60)
    print(f"Mode:          {'Headless' if args.headless else 'Visible browser'}")
    print(f"Dry run:       {args.dry_run}")
    print(f"Workers:       {args.workers}")
    if args.workers == 1:
        print(f"Pages:         {args.start_page} to {args.end_page or 'end'}")
    print(f"Min year:      {args.min_year}")
    print(f"Single permit: {args.single_permit or 'None (all permits)'}")
    print()

    try:
        if args.workers > 1:
            # Queue-based parallel mode - ignores start_page/end_page
            if args.start_page != 1 or args.end_page is not None:
                print("Note: --start-page and --end-page are ignored in parallel mode")
                print()
            stats = await download_queue_parallel(
                num_workers=args.workers,
                headless=args.headless,
                dry_run=args.dry_run,
                min_year=args.min_year,
                single_permit=args.single_permit
            )
        else:
            # Sequential mode - original behavior
            stats = await download_all_coal_files(
                headless=args.headless,
                dry_run=args.dry_run,
                start_page=args.start_page,
                end_page=args.end_page,
                min_year=args.min_year,
                single_permit=args.single_permit
            )

        print()
        print("=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"Permits processed:  {stats['permits_processed']}")
        print(f"Total files found:  {stats['total_files_found']}")
        print(f"Downloaded:         {stats['downloaded']}")
        print(f"Skipped (exists):   {stats['skipped_exists']}")
        print(f"Skipped (date):     {stats['skipped_date']}")
        print(f"Failed:             {stats['failed']}")

    except Exception as e:
        print()
        print("=" * 60)
        print("ERROR")
        print("=" * 60)
        print(f"{e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
