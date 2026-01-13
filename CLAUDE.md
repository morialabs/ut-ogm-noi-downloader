# Utah OGM Scraper

## Virtual Environment

This project uses a Python virtual environment. Always activate it before running scripts:

```bash
source venv/bin/activate
```

### Setup (first time)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Scripts

### Download Scripts
- `download_coal.py` - Download coal permit documents
- `download_noi.py` - Download NOI (Notice of Intent) documents

### Upload Scripts
- `upload_to_qdrant.py` - Upload PDFs to Qdrant vector database
- `verify_qdrant.py` - Verify Qdrant collection status

## Environment Variables

Create a `.env` file with:
```
QDRANT_URL=https://your-cluster.qdrant.io
QDRANT_API_KEY=your_qdrant_api_key
OPENAI_API_KEY=your_openai_api_key
```
