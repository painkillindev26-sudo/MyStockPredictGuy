# Product Agent

A Flask web app that searches for digital product ideas, scores them, queues the top five, and builds them as single-file HTML apps.

## Deploy on Render

1. Push this repository to GitHub.
2. In Render, choose **New > Blueprint** and connect this repository. Render will read `render.yaml` and create the web service.
3. The Blueprint uses a paid Starter instance because Render persistent disks are not available on free web services. The 1 GB disk is mounted at `/var/data` so saved ideas and generated products survive redeploys.
4. In the new service's **Environment** settings, set `ANTHROPIC_API_KEY` to a valid Anthropic API key. Do not commit the key to the repository.
5. Deploy, then open the service URL. Use the three buttons in order: find ideas, score and queue, then build products.

The `static/index.html` page is the Product Agent interface served by Flask. The separate root `index.html` is the Ad Creative page and is not active in this app; it expects an `/api/generate` endpoint that this backend does not provide.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your_key  # Windows PowerShell: $env:ANTHROPIC_API_KEY="your_key"
python app.py
```

Open <http://localhost:5000>.

## Notes

- The first request after an idle period may take longer while the service starts.
- Generated files are downloadable `.html` apps that open in a browser.
- Never commit API keys; configure them as Render environment variables.
