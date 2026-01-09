# Utah OGM NOI Document Downloader

Downloads NOI (Notice of Intention) documents from Utah's Division of Oil, Gas and Mining website.

## Setup

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
```

## Usage

```bash
# Run with visible browser (recommended for first run)
python download_noi.py

# Run headless
python download_noi.py --headless
```

## Output

Downloaded documents are saved to:
```
output/{permit_id}/{permit_id}_{date}.pdf
```

Example:
```
output/M0510008/M0510008_2025-12-22.pdf
```

## How it works

1. Navigates to ogm.utah.gov/minerals-files/
2. Waits for the embedded Salesforce portal to load
3. Clicks the "NOIs" tab
4. Finds the first document in the table
5. Clicks the VIEW button to access the PDF
6. Downloads the PDF from Amazon S3
