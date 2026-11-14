# Sources that block the bot (Cloudflare)

Some sites (nhentai.net, comicsvalley.com, multporn.net ...) put a Cloudflare challenge in front of their pages.
The bot handles this in layers, cheapest first - you only need the later ones if the earlier ones fail:

1. **Browser fingerprints** - automatic. Needs `curl_cffi` (in requirements.txt; `pip install -U curl_cffi`).
2. **FlareSolverr** - a real browser the bot hands blocked pages to. *This is the fix that works from a hosted server.*
   The cookies it earns are reused for ~25 min for pages, APIs **and image downloads**, so one solve covers many requests.
3. **Your own cookies** - only if the bot runs on the machine you browse from (Cloudflare ties `cf_clearance` to the IP).
4. **nhentai API key** - nhentai only: an API key from your account settings skips its captcha.

## Run FlareSolverr (docker)

    docker run -d --name flaresolverr -p 8191:8191 --restart unless-stopped ghcr.io/flaresolverr/flaresolverr:latest

Next to the bot in docker-compose, use `http://flaresolverr:8191`. Needs ~300-500 MB RAM while solving.

## Tell the bot

`/settings` -> **🛰 Source Access**

| Setting | Value |
|---|---|
| FlareSolverr URL | `http://flaresolverr:8191` (or the server's address) |
| NHentai API key | key from nhentai.net -> account -> settings |
| Site cookies / Browser User-Agent | only for option 3: `nhentai.net: cf_clearance=...` + that browser's User-Agent |

## Check a source

    python -m Webs.diagnose nhentai "your query"

Step [0] says whether the bot can reach the site and, if not, whether it is bot protection. Paste the output when
reporting a problem - it contains no secrets.
