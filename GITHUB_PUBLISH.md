# Publishing Dohtar Monitor to GitHub

## 1. Create the repository on GitHub

1. Go to https://github.com/new
2. **Repository name**: `dohtar-monitor`
3. **Description**: `Self-hosted homelab monitoring for GPUs, CPU, Docker, and llama.cpp LLMs — real-time dashboard with Python agents`
4. Set visibility to **Public**
5. **Do NOT** initialize with README, .gitignore, or license (we already have them)
6. Click **Create repository**

---

## 2. Initialize and push from your local folder

Open a terminal in the `GithubDohtarMonitor/` folder and run:

```bash
git init
git add .
git commit -m "Initial release: Dohtar Monitor v2.5.2"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/dohtar-monitor.git
git push -u origin main
```

Replace `YOUR_USERNAME` with your GitHub username.

---

## 3. Add a license file

On GitHub, go to your repo → **Add file** → **Create new file** → name it `LICENSE`.
Click **Choose a license template** → select **MIT** → fill in your name → commit.

---

## 4. Recommended GitHub repo settings

In your repo **Settings**:

- **About** (gear icon on main page): add description, topics like `homelab`, `monitoring`, `llm`, `nvidia`, `docker`, `llama-cpp`, `python`, `dashboard`
- **Releases**: tag the first release as `v2.5.2`

To create a release:
```bash
git tag v2.5.2
git push origin v2.5.2
```
Then on GitHub → **Releases** → **Draft a new release** → select tag `v2.5.2`.

---

## 5. Verify nothing sensitive leaked

Before making public, do a final check:

```bash
grep -r "192\.168\.50\." . --include="*.py" --include="*.js" --include="*.json" --include="*.md"
grep -rE "(password|secret|token|api_key)\s*[:=]\s*['\"][^'\"]{4,}" . --include="*.py" --include="*.js" --include="*.json"
```

Both should return no results.
