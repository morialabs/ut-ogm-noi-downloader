# Utah OGM Scraper

## Git Workflow

- **Never merge directly to main.** Always use a pull request.
- Create a feature branch for all changes
- Push the branch and create a PR via `gh pr create`
- Verify the PR is mergeable before merging
- Use `gh pr merge` to merge approved PRs

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
