#!/usr/bin/env python3
"""severino-panel — o que o Fire TV precisa saber do servidor de mídia, numa API só.

As chaves (Jellyfin, Radarr, Sonarr, Seerr, Bazarr) ficam AQUI, no Pi; a TV nunca as vê.
Somente stdlib. Escuta em 0.0.0.0:PORT.

Leitura:  GET /health, /overview, /requests, /library, /subtitles, /events
Escrita (exige X-Panel-Token): POST /request, /delete, /subtitles/search, /hook/radarr
"""

import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "9124"))
INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
TOKEN = os.environ.get("PANEL_TOKEN", "")
CONF = os.environ.get("CONF", "/etc/severino-panel/keys.env")


def load_conf(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


CONF_VALUES = load_conf(CONF)
JELLYFIN = CONF_VALUES.get("JELLYFIN_URL", "http://127.0.0.1:8097")
JELLYFIN_USER = CONF_VALUES.get("JELLYFIN_USER_ID", "")
# A rotina deletewatchedarr olha o histórico DESTE usuário do Jellyfin
WATCH_USER = CONF_VALUES.get("JELLYFIN_WATCH_USER_ID", JELLYFIN_USER)
RADARR = CONF_VALUES.get("RADARR_URL", "http://127.0.0.1:7878")
SONARR = CONF_VALUES.get("SONARR_URL", "http://127.0.0.1:8989")
SEERR = CONF_VALUES.get("SEERR_URL", "http://127.0.0.1:5055")
BAZARR = CONF_VALUES.get("BAZARR_URL", "http://127.0.0.1:6767")
DISK_PATH = CONF_VALUES.get("DISK_PATH", "/DATA")
RECYCLE = CONF_VALUES.get("RECYCLE_PATH", "/DATA/AppData/arr-stack/recycle")
SUBTITLE_LANG = CONF_VALUES.get("SUBTITLE_LANG", "pb")   # Bazarr calls it "pb" (Portuguese, Brazil)
SUBTITLE_MATCH = {"pb", "pt-br", "pt", "pob"}

EVENTS = deque(maxlen=50)   # download finished, etc. — the TV polls /events?since=


def api(base, path, key, header="X-Api-Key", method="GET", body=None, timeout=8):
    url = base.rstrip("/") + path
    data = None
    headers = {header: key, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def jellyfin(path, **q):
    query = urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})
    return api(JELLYFIN, path + ("?" + query if query else ""), CONF_VALUES.get("JELLYFIN_KEY", ""),
               header="X-Emby-Token")


def radarr(path, **q):
    query = urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})
    return api(RADARR, path + ("?" + query if query else ""), CONF_VALUES.get("RADARR_KEY", ""))


def sonarr(path, **q):
    query = urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})
    return api(SONARR, path + ("?" + query if query else ""), CONF_VALUES.get("SONARR_KEY", ""))


def seerr(path, method="GET", body=None):
    return api(SEERR, path, CONF_VALUES.get("SEERR_KEY", ""), method=method, body=body)


def bazarr(path, method="GET", body=None):
    return api(BAZARR, path, CONF_VALUES.get("BAZARR_KEY", ""), header="X-API-KEY", method=method, body=body)


def bazarr_form(path, form):
    """Bazarr's subtitle search is PATCH with form fields, not JSON."""
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(BAZARR.rstrip("/") + path, data=data, method="PATCH",
                                 headers={"X-API-KEY": CONF_VALUES.get("BAZARR_KEY", ""),
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


_CACHE = {}


def cached(key, ttl, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (time.time(), val)
    return val


def delete_settings():
    """A rotina deletewatchedarr: retenção (dias) e se está em dry-run."""
    out = {"movie_days": None, "episode_days": None, "dry_run": True, "interval": None}
    path = os.path.join(os.path.dirname(RECYCLE.rstrip("/")), "deletewatchedarr", "config.yaml")
    try:
        with open(path) as f:
            for line in f:
                k, _, v = line.partition(":")
                k, v = k.strip(), v.strip()
                try:
                    if k == "movie_retention_days":
                        out["movie_days"] = int(v)
                    elif k == "episode_retention_days":
                        out["episode_days"] = int(v)
                    elif k == "dry_run":
                        out["dry_run"] = v.lower() == "true"
                    elif k == "interval_minutes":
                        out["interval"] = int(v)
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def watched_map():
    """TMDB id -> {played, days since last play}, do Jellyfin (o que o deletewatchedarr lê)."""
    out = {}
    try:
        d = jellyfin("/Items", IncludeItemTypes="Movie", Recursive="true", Fields="ProviderIds,UserData",
                     UserId=WATCH_USER, Limit=1000)
        for it in (d or {}).get("Items", []):
            ud = it.get("UserData") or {}
            tmdb = (it.get("ProviderIds") or {}).get("Tmdb")
            if not tmdb:
                continue
            last = ud.get("LastPlayedDate")
            days = None
            if last:
                try:
                    t = time.mktime(time.strptime(last[:19], "%Y-%m-%dT%H:%M:%S"))
                    days = int((time.time() - t) // 86400)
                except Exception:
                    days = None
            out[str(tmdb)] = {"played": bool(ud.get("Played")), "days": days}
    except Exception:
        pass
    return out


def gb(n):
    return round((n or 0) / 1e9, 1)


def disk():
    st = os.statvfs(DISK_PATH)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    return {"total_gb": gb(total), "used_gb": gb(used), "free_gb": gb(free),
            "pct": round(100.0 * used / total, 1) if total else 0}


def movies(only_files=False):
    out = []
    for m in radarr("/api/v3/movie") or []:
        if only_files and not m.get("hasFile"):
            continue
        out.append({
            "kind": "movie", "id": m.get("id"), "title": m.get("title"), "year": m.get("year"),
            "size_gb": gb(m.get("sizeOnDisk")), "tmdb": m.get("tmdbId"),
            "monitored": m.get("monitored"), "has_file": m.get("hasFile"),
        })
    return out


def series():
    out = []
    for s in sonarr("/api/v3/series") or []:
        st = s.get("statistics") or {}
        out.append({"kind": "series", "id": s.get("id"), "title": s.get("title"), "year": s.get("year"),
                    "size_gb": gb(st.get("sizeOnDisk")), "tmdb": s.get("tmdbId"),
                    "episodes": st.get("episodeFileCount")})
    return out


def queue(kind):
    base = radarr if kind == "movie" else sonarr
    try:
        d = base("/api/v3/queue?pageSize=50&includeUnknownMovieItems=true&includeUnknownSeriesItems=true")
    except Exception:
        return []
    items = []
    for r in (d or {}).get("records", []):
        media = r.get("movie") or r.get("series") or {}
        items.append({
            "kind": kind, "title": r.get("title") or media.get("title"),
            "tmdb": media.get("tmdbId"), "radarr_id": r.get("movieId"), "sonarr_id": r.get("seriesId"),
            "state": r.get("trackedDownloadState") or r.get("status"),
            "status": r.get("status"), "pct": round(100 - 100.0 * (r.get("sizeleft") or 0) / (r.get("size") or 1), 1),
            "sizeleft_gb": gb(r.get("sizeleft")), "timeleft": r.get("timeleft"), "error": r.get("errorMessage"),
        })
    return items


_TITLES = {}


def seerr_title(tmdb, media_type):
    """Seerr's request payload carries only the TMDB id: ask the title once, then cache it."""
    if not tmdb:
        return "?"
    key = (media_type, tmdb)
    if key in _TITLES:
        return _TITLES[key]
    try:
        d = seerr(f"/api/v1/{'tv' if media_type == 'tv' else 'movie'}/{tmdb}")
        name = (d or {}).get("title") or (d or {}).get("name") or f"#{tmdb}"
    except Exception:
        name = f"#{tmdb}"
    _TITLES[key] = name
    return name


SEERR_STATUS = {1: "pending", 2: "approved", 3: "declined"}
SEERR_MEDIA = {1: "unknown", 2: "pending", 3: "processing", 4: "partial", 5: "available"}
RADARR_Q = {"downloading": "baixando", "importPending": "importando", "imported": "importado",
            "importBlocked": "import bloqueado", "failedPending": "falhou", "failed": "falhou"}


def requests():
    return cached("requests", 2, _requests)


def _requests():
    try:
        d = seerr("/api/v1/request?take=40&sort=added")
    except Exception as e:
        return {"error": str(e), "items": []}
    # A fila do Radarr/Sonarr traz o id local (movieId/seriesId), não o TMDB: monta os dois sentidos.
    local, has_file = {}, {}
    try:
        for m in movies():
            if m.get("tmdb"):
                local[("movie", m["tmdb"])] = m["id"]
                has_file[("movie", m["tmdb"])] = bool(m.get("has_file"))
        for s in series():
            if s.get("tmdb"):
                local[("series", s["tmdb"])] = s["id"]
    except Exception:
        pass
    by_id = {}
    for q in queue("movie") + queue("series"):
        if q["kind"] == "movie" and q.get("radarr_id") is not None:
            by_id[("movie", q["radarr_id"])] = q
        elif q["kind"] == "series" and q.get("sonarr_id") is not None:
            by_id[("series", q["sonarr_id"])] = q
    out = []
    include_declined = False   # pedidos recusados são histórico: fora da lista da TV
    for r in (d or {}).get("results", []):
        if r.get("status") == 3 and not include_declined:
            continue
        media = r.get("media") or {}
        tmdb = media.get("tmdbId")
        mt = (media.get("mediaType") or "movie")
        title = media.get("title") or media.get("name") or media.get("originalTitle") or seerr_title(tmdb, mt)
        state, pct, eta, size, err = None, None, None, None, None
        key = (mt, tmdb)
        q = by_id.get((mt, local.get(key)))
        if q:
            state = RADARR_Q.get(q["state"], q["state"])
            pct, eta, size, err = q["pct"], q["timeleft"], q["sizeleft_gb"], q["error"]
        elif has_file.get(key) or media.get("status") == 5:
            state = "disponível"
        elif r.get("status") == 1:
            state = "aguardando aprovação"
        elif r.get("status") == 3:
            state = "recusado"
        elif key not in local:
            continue   # pedido sem mídia no Radarr: já foi apagado, não polui a lista
        else:
            state = "monitorado, sem release"
        out.append({"id": r.get("id"), "tmdb": tmdb, "type": mt, "title": title,
                    "request": SEERR_STATUS.get(r.get("status")), "media": SEERR_MEDIA.get(media.get("status")),
                    "state": state, "pct": pct, "eta": eta, "sizeleft_gb": size, "error": err,
                    "at": r.get("createdAt")})
    return {"items": out}


def du(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def recycle_info():
    return {"gb": gb(du(RECYCLE)) if os.path.isdir(RECYCLE) else 0.0}


def purge_recycle():
    freed = 0
    if not os.path.isdir(RECYCLE):
        return {"freed_gb": 0.0}
    for kind in ("radarr", "sonarr"):
        d = os.path.join(RECYCLE, kind)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                if os.path.islink(p) or os.path.isfile(p):
                    freed += os.path.getsize(p)
                    os.remove(p)
                elif os.path.isdir(p):
                    freed += du(p)
                    shutil.rmtree(p)
            except OSError:
                pass
    return {"freed_gb": gb(freed)}


def overview():
    return cached("overview", 2, _overview)


def _overview():
    d = disk()
    lib = [m for m in movies(only_files=True)]
    biggest = sorted(lib, key=lambda m: m["size_gb"] or 0, reverse=True)[:8]
    total_lib = sum(m["size_gb"] or 0 for m in biggest)
    alerts = []
    if d["pct"] >= 90:
        alerts.append({"level": "red", "text": f"Disco em {d['pct']}% — {d['free_gb']} GB livres"})
    elif d["pct"] >= 80:
        alerts.append({"level": "amber", "text": f"Disco em {d['pct']}% — {d['free_gb']} GB livres"})
    # how many of the biggest to delete to get back under 75%
    if d["pct"] >= 80:
        freed, need = 0.0, d["used_gb"] - 0.75 * d["total_gb"]
        for m in biggest:
            if freed >= need:
                break
            freed += m["size_gb"] or 0
        if freed > 0:
            alerts.append({"level": "info",
                           "text": f"os {biggest[0]['title']} e mais alguns liberam {round(freed,1)} GB"})
    recent, resume = [], []
    try:
        for it in (jellyfin(f"/Users/{JELLYFIN_USER}/Items/Latest", Limit=10) or []):
            recent.append({"title": it.get("Name"), "id": it.get("Id"), "type": it.get("Type"),
                           "year": it.get("ProductionYear"),
                           "image": f"{JELLYFIN}/Items/{it.get('Id')}/Images/Primary?maxHeight=300"})
    except Exception:
        pass
    try:
        for it in (jellyfin(f"/Users/{JELLYFIN_USER}/Items/Resume", Limit=10,
                            Fields="PrimaryImageAspectRatio") or {}).get("Items", []):
            pos = (it.get("UserData") or {}).get("PlaybackPositionTicks") or 0
            dur = it.get("RunTimeTicks") or 1
            resume.append({"title": it.get("Name"), "id": it.get("Id"), "type": it.get("Type"),
                           "pct": round(100.0 * pos / dur, 1),
                           "image": f"{JELLYFIN}/Items/{it.get('Id')}/Images/Primary?maxHeight=300"})
    except Exception:
        pass
    ds = delete_settings()
    if ds.get("dry_run") and ds.get("movie_days") is not None:
        alerts.append({"level": "info",
                       "text": f"exclusão automática em DRY-RUN (não apaga) · retenção {ds['movie_days']}d"})
    rec = recycle_info()
    if rec["gb"] >= 1:
        alerts.append({"level": "info", "text": f"lixeira: {rec['gb']} GB — dá para esvaziar"})
    return {"disk": d, "alerts": alerts, "biggest": biggest, "library_gb": round(total_lib, 1),
            "recent": recent, "resume": resume, "recycle": rec,
            "requests": requests().get("items", [])}


def library():
    return cached("library", 10, _library)


def _library():
    ds = delete_settings()
    wm = cached("watched", 60, watched_map)
    items = [m for m in movies() if m.get("has_file")] + series()
    for it in items:
        if it.get("kind") != "movie":
            continue
        w = wm.get(str(it.get("tmdb"))) or {}
        it["watched"] = w.get("played", False)
        it["watched_days"] = w.get("days")
        if it["watched"] and ds["movie_days"] is not None and w.get("days") is not None:
            it["delete_in_days"] = max(0, ds["movie_days"] - w["days"])
    items.sort(key=lambda x: (not x.get("watched", False), -(x.get("size_gb") or 0)))
    return {"items": items, "total_gb": round(sum(i.get("size_gb") or 0 for i in items), 1),
            "retention": ds}


def subtitles():
    out = []
    try:
        d = bazarr("/api/movies?start=0&length=-1")
        for m in (d or {}).get("data", []):
            miss = m.get("missing_subtitles") or []
            codes = [x.get("code2") if isinstance(x, dict) else x for x in miss]
            if any(str(c).lower() in SUBTITLE_MATCH for c in codes):
                out.append({"kind": "movie", "id": m.get("radarrId"), "title": m.get("title"),
                            "year": m.get("year"), "missing": codes})
    except Exception as e:
        return {"error": str(e), "items": []}
    return {"items": out}


def make_request(tmdb, media_type):
    return seerr("/api/v1/request", method="POST", body={"mediaType": media_type, "mediaId": int(tmdb)})


def delete(kind, item_id, files):
    if kind == "movie":
        radarr(f"/api/v3/movie/{item_id}?deleteFiles={'true' if files else 'false'}&addImportExclusion=false",
               method="DELETE")
    else:
        sonarr(f"/api/v3/series/{item_id}?deleteFiles={'true' if files else 'false'}&addImportExclusion=false",
               method="DELETE")
    return {"deleted": True}


def subtitle_search(kind, item_id):
    code = bazarr_form("/api/movies/subtitles", {"radarrid": int(item_id), "language": SUBTITLE_LANG,
                                                 "forced": "False", "hi": "False"})
    return {"status": code}


def add_event(kind, title):
    EVENTS.append({"at": int(time.time()), "kind": kind, "title": title})


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _token_ok(self):
        return not TOKEN or self.headers.get("X-Panel-Token") == TOKEN

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        path, _, query = self.path.partition("?")
        try:
            if path in ("/", "/index.html"):
                try:
                    with open(INDEX, encoding="utf-8") as f:
                        return self._send_html(f.read().replace("__PANEL_TOKEN__", TOKEN))
                except FileNotFoundError:
                    return self._send(404, {"error": "index.html missing"})
            if path == "/health":
                return self._send(200, {"ok": True})
            if path == "/overview":
                return self._send(200, overview())
            if path == "/requests":
                return self._send(200, requests())
            if path == "/library":
                return self._send(200, library())
            if path == "/subtitles":
                return self._send(200, subtitles())
            if path == "/recycle":
                return self._send(200, recycle_info())
            if path == "/events":
                since = int(dict(urllib.parse.parse_qsl(query)).get("since", 0))
                return self._send(200, {"events": [e for e in EVENTS if e["at"] > since], "now": int(time.time())})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        path = self.path.partition("?")[0]
        try:
            if path in ("/hook/radarr", "/hook/sonarr"):   # webhooks, na própria máquina
                b = self._body()
                if b.get("eventType") in ("Download", "Test", "Grab"):
                    media = b.get("movie") or b.get("series") or {}
                    title = media.get("title") or b.get("movie", {}).get("folderName") \
                        or b.get("series", {}).get("title") or "novo título"
                    add_event("grab" if b.get("eventType") == "Grab" else "download", title)
                return self._send(200, {"ok": True})
            if not self._token_ok():
                return self._send(403, {"error": "token"})
            b = self._body()
            if path == "/request":
                return self._send(200, make_request(b["tmdbId"], b.get("type", "movie")) or {"ok": True})
            if path == "/delete":
                return self._send(200, delete(b.get("kind", "movie"), b["id"], b.get("files", True)))
            if path == "/subtitles/search":
                return self._send(200, {"ok": True, "result": subtitle_search(b.get("kind", "movie"), b["id"])})
            if path == "/recycle/purge":
                return self._send(200, purge_recycle())
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"severino-panel on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
