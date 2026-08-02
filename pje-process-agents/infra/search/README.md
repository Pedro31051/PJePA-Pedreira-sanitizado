# Agent Platform Search corpus

This directory provisions a private GCS-backed Discovery Engine data store that
contains only the public primary sources declared in `corpus.json`.

```bash
gcloud services enable discoveryengine.googleapis.com --project=your-gcp-project-id
uv run --script infra/search/manage_search.py \
  --project your-gcp-project-id \
  --bucket your-gcp-bucket
```

Copy the resulting full `data_store_path` into
`PJE_OFFICIAL_SEARCH_DATA_STORE_ID`. Never upload process documents to this
bucket.
