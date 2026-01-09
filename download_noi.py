#!/usr/bin/env python3
"""
Utah OGM NOI Document Downloader

Downloads the first NOI document from ogm.utah.gov/minerals-files/

Usage:
    python download_noi.py           # Run with visible browser
    python download_noi.py --headless  # Run headless
"""

import asyncio
import argparse
import re
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

OUTPUT_DIR = Path(__file__).parent / "output"


async def download_first_noi(headless: bool = False):
    """Download the first NOI document from the minerals-files page."""

    async with async_playwright() as p:
        # Launch browser
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()

        # Track PDF URLs from network requests
        pdf_urls = []

        def capture_request(request):
            url = request.url
            if ".pdf" in url.lower() or "s3.amazonaws" in url.lower():
                pdf_urls.append(url)

        page.on("request", capture_request)

        try:
            print("[1/6] Navigating to minerals-files page...")
            await page.goto("https://ogm.utah.gov/minerals-files/", wait_until="networkidle")

            print("[2/6] Waiting for iframe to load...")

            # Wait for iframe and get the actual Frame object (not FrameLocator)
            await page.wait_for_selector("iframe", timeout=30000)
            await page.wait_for_timeout(5000)  # Give time for iframe content to load

            # Get all frames
            frames = page.frames
            print(f"       Found {len(frames)} frames")

            # Find the Salesforce frame
            sf_frame = None
            for i, frame in enumerate(frames):
                frame_url = frame.url
                print(f"       Frame {i}: {frame_url[:100] if frame_url else 'no url'}")
                if "site.com" in frame_url.lower() or "salesforce" in frame_url.lower():
                    sf_frame = frame
                    print(f"       Using frame {i}")
                    break

            if sf_frame is None and len(frames) > 1:
                sf_frame = frames[1]
                print("       Using frame 1 as fallback")

            if sf_frame is None:
                raise Exception("Could not find Salesforce iframe")

            print("[3/6] Clicking NOIs tab...")

            # Wait for the NOIs tab to be visible and click it
            try:
                nois_tab = sf_frame.get_by_text("NOIs", exact=True)
                await nois_tab.wait_for(state="visible", timeout=60000)
                await nois_tab.click()
                print("       Clicked NOIs tab")
            except Exception as e:
                print(f"       NOIs tab click via locator failed: {e}")
                # Fallback: click via JavaScript
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
                print("       Clicked NOIs tab via JavaScript")

            # Wait for table to update
            await page.wait_for_timeout(5000)

            print("[4/6] Getting first document info...")

            # Get the frame's HTML content
            content = await sf_frame.content()

            # Also get visible text
            body_text = await sf_frame.evaluate("() => document.body.innerText")

            # Extract permit ID (pattern: M0510008 or S0250015)
            permit_match = re.search(r'[MS]\d{7}', body_text)
            permit_id = permit_match.group(0) if permit_match else "unknown"

            # Extract date (pattern: 2025-12-22)
            date_match = re.search(r'\d{4}-\d{2}-\d{2}', body_text)
            doc_date = date_match.group(0) if date_match else "unknown"

            print(f"       Permit: {permit_id}")
            print(f"       Date: {doc_date}")

            # Save debug info
            debug_dir = OUTPUT_DIR / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / "body_text.txt").write_text(body_text)
            (debug_dir / "content.html").write_text(content)
            await page.screenshot(path=str(debug_dir / "page.png"))

            print("[5/6] Clicking VIEW button...")

            # Strategy 1: Try to find and click VIEW button using Playwright locators
            clicked = False

            try:
                # Look for the eye icon button in the table
                view_button = sf_frame.locator('lightning-button-icon[icon-name*="preview"], lightning-button-icon[icon-name*="view"], button[title*="View"], [data-key="preview"]').first
                await view_button.wait_for(state="visible", timeout=10000)

                # Set up listener for new page before clicking
                async with context.expect_page(timeout=30000) as new_page_info:
                    await view_button.click()
                    clicked = True
                    print("       Clicked VIEW button via locator")

                new_page = await new_page_info.value
                await new_page.wait_for_load_state("networkidle")
                pdf_url = new_page.url
                print(f"       New tab URL: {pdf_url[:80]}...")

            except Exception as e:
                print(f"       Locator approach failed: {e}")

            # Strategy 2: Try JavaScript to find and click
            if not clicked:
                try:
                    result = await sf_frame.evaluate("""
                        (() => {
                            // Find VIEW buttons by looking for eye icons
                            function findViewButton(root, depth = 0) {
                                if (depth > 10) return null;

                                // Look for lightning-button-icon elements
                                const buttons = root.querySelectorAll('lightning-button-icon, button, [role="button"]');
                                for (const btn of buttons) {
                                    const iconName = btn.getAttribute('icon-name') || '';
                                    const title = btn.getAttribute('title') || '';
                                    const ariaLabel = btn.getAttribute('aria-label') || '';

                                    if (iconName.includes('preview') || iconName.includes('view') ||
                                        title.toLowerCase().includes('view') ||
                                        ariaLabel.toLowerCase().includes('view')) {
                                        return btn;
                                    }
                                }

                                // Check shadow roots
                                const elements = root.querySelectorAll('*');
                                for (const el of elements) {
                                    if (el.shadowRoot) {
                                        const found = findViewButton(el.shadowRoot, depth + 1);
                                        if (found) return found;
                                    }
                                }

                                return null;
                            }

                            const btn = findViewButton(document);
                            if (btn) {
                                // Try clicking through shadow root if present
                                if (btn.shadowRoot) {
                                    const innerBtn = btn.shadowRoot.querySelector('button');
                                    if (innerBtn) {
                                        innerBtn.click();
                                        return { clicked: true, method: 'shadow' };
                                    }
                                }
                                btn.click();
                                return { clicked: true, method: 'direct' };
                            }
                            return { clicked: false };
                        })()
                    """)

                    if result and result.get('clicked'):
                        clicked = True
                        print(f"       Clicked VIEW button via JavaScript ({result.get('method')})")
                        await page.wait_for_timeout(5000)
                except Exception as e:
                    print(f"       JavaScript click failed: {e}")

            # Check for new tab or captured URLs
            pdf_url = None
            all_pages = context.pages

            if len(all_pages) > 1:
                pdf_page = all_pages[-1]
                pdf_url = pdf_page.url
                print(f"       New tab opened: {pdf_url[:80]}...")
            elif pdf_urls:
                pdf_url = pdf_urls[-1]
                print(f"       Captured URL from network: {pdf_url[:80]}...")

            # Strategy 3: Look for direct links in the page
            if not pdf_url:
                print("       Looking for PDF links in page...")
                links = await sf_frame.evaluate("""
                    (() => {
                        const links = [];
                        document.querySelectorAll('a[href]').forEach(a => {
                            const href = a.href || '';
                            if (href.includes('.pdf') || href.includes('s3.amazonaws') || href.includes('sfc/')) {
                                links.push(href);
                            }
                        });
                        return links;
                    })()
                """)

                if links:
                    pdf_url = links[0]
                    print(f"       Found link: {pdf_url[:80]}...")

            if not pdf_url:
                await page.screenshot(path=str(debug_dir / "error_screenshot.png"))
                raise Exception("Could not find PDF URL. Check debug screenshots.")

            print("[6/6] Downloading PDF...")

            # Create output directory
            permit_dir = OUTPUT_DIR / permit_id
            permit_dir.mkdir(parents=True, exist_ok=True)

            # Download the PDF
            filename = f"{permit_id}_{doc_date}.pdf"
            output_path = permit_dir / filename

            # Use Playwright's request context to download
            response = await page.request.get(pdf_url)
            content_bytes = await response.body()

            # Verify it's a PDF
            if content_bytes[:4] == b'%PDF':
                output_path.write_bytes(content_bytes)
                file_size = output_path.stat().st_size
                print(f"       Saved: {output_path} ({file_size:,} bytes)")
            else:
                # Save anyway but warn
                output_path.write_bytes(content_bytes)
                file_size = output_path.stat().st_size
                print(f"       Warning: Content may not be PDF (first 4 bytes: {content_bytes[:4]})")
                print(f"       Saved: {output_path} ({file_size:,} bytes)")

            return {
                "permit_id": permit_id,
                "doc_date": doc_date,
                "pdf_url": pdf_url,
                "output_path": str(output_path),
                "file_size": file_size
            }

        finally:
            await browser.close()


async def main():
    parser = argparse.ArgumentParser(description="Download first NOI from Utah OGM")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    args = parser.parse_args()

    print("=" * 50)
    print("Utah OGM NOI Document Downloader")
    print("=" * 50)
    print(f"Mode: {'Headless' if args.headless else 'Visible browser'}")
    print()

    try:
        result = await download_first_noi(headless=args.headless)
        print()
        print("=" * 50)
        print("SUCCESS")
        print("=" * 50)
        print(f"Permit ID: {result['permit_id']}")
        print(f"Doc Date:  {result['doc_date']}")
        print(f"File Size: {result['file_size']:,} bytes")
        print(f"Output:    {result['output_path']}")
    except Exception as e:
        print()
        print("=" * 50)
        print("ERROR")
        print("=" * 50)
        print(f"{e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
