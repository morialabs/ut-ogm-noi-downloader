#!/usr/bin/env python3
"""
Utah OGM Minerals Permit Files Downloader (Wrapper)

This is a backward-compatible wrapper for the unified ogm_downloader.py script.
All functionality has been merged into ogm_downloader.py.

Usage:
    python download_minerals.py                      # Download all (visible browser)
    python download_minerals.py --headless           # Run headless
    python download_minerals.py --dry-run            # List without downloading
    python download_minerals.py --single-permit M0070001  # Process only one permit
    python download_minerals.py --start-page 5       # Start from page 5
    python download_minerals.py --min-year 2015      # Custom year filter
    python download_minerals.py --workers 4          # Run 4 parallel browser instances
"""

import sys
import asyncio
import argparse

from ogm_downloader import (
    MINERALS_CONFIG,
    DEFAULT_MIN_YEAR,
    download_all_files,
    download_queue_parallel,
)


async def main():
    parser = argparse.ArgumentParser(description="Download minerals permit files from Utah OGM")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument("--dry-run", action="store_true", help="List documents without downloading")
    parser.add_argument("--start-page", type=int, default=1, help="Start from specific page (default: 1)")
    parser.add_argument("--end-page", type=int, default=None, help="Stop at specific page (default: all)")
    parser.add_argument("--min-year", type=int, default=DEFAULT_MIN_YEAR,
                        help=f"Minimum document year (default: {DEFAULT_MIN_YEAR})")
    parser.add_argument("--single-permit", type=str, default=None,
                        help="Process only this permit ID (for testing)")
    parser.add_argument("--workers", type=int, default=1,
                        help="Number of parallel browser instances (default: 1)")
    args = parser.parse_args()

    config = MINERALS_CONFIG

    print("=" * 60)
    print("Utah OGM Minerals Permit Files Downloader")
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
            if args.start_page != 1 or args.end_page is not None:
                print("Note: --start-page and --end-page are ignored in parallel mode")
                print()
            stats = await download_queue_parallel(
                config=config,
                num_workers=args.workers,
                headless=args.headless,
                dry_run=args.dry_run,
                min_year=args.min_year,
                single_permit=args.single_permit
            )
        else:
            stats = await download_all_files(
                config=config,
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
