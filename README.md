# Product Agent

Finds digital product ideas on the web, scores them, queues the top 5, and builds them as single-file HTML apps.

## Put it on GitHub

```bash
cd product-agent
git init
git add .
git commit -m "Product agent"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/product-agent.git
git push -u origin main
```

Create the empty repo `product-agent` on github.com first (no README, no .gitignore).

## Deploy on Render

1. On render.com, click **New > Blueprint** and connect your GitHub repo.
2. Render reads `render.yaml` and creates the service.
3. In the service's **Environment** tab, set `ANTHROPIC_API_KEY` to your key.
4. Open the URL Render gives you and use the three buttons in order.

## Notes

- The free plan sleeps after inactivity and the first load can take about a minute.
- Saved data lives on the server's disk. On the free plan it is wiped on each redeploy. A paid plan with a persistent disk (see `render.yaml`) keeps it.
- Downloads are the finished `.html` files. Each opens in any browser.
- Never commit your API key. It belongs only in Render's environment settings.
