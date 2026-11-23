#!/usr/bin/env python3
"""severino-panel — o que o Fire TV precisa saber do servidor de mídia, numa API só.

As chaves (Jellyfin, Radarr, Sonarr, Seerr, Bazarr) ficam AQUI, no Pi; a TV nunca as vê.
Somente stdlib. Escuta em 0.0.0.0:PORT.

Leitura:  GET /health, /overview, /requests, /library, /subtitles, /events
Escrita (exige X-Panel-Token): POST /request, /delete, /subtitles/search, /hook/radarr
"""

import json
import os
import re
import shutil
import subprocess
import threading
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


def jellyfin(path, method="GET", **q):
    query = urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})
    return api(JELLYFIN, path + ("?" + query if query else ""), CONF_VALUES.get("JELLYFIN_KEY", ""),
               header="X-Emby-Token", method=method)


def radarr(path, method="GET", **q):
    query = urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})
    return api(RADARR, path + ("?" + query if query else ""), CONF_VALUES.get("RADARR_KEY", ""), method=method)


def sonarr(path, method="GET", **q):
    query = urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})
    return api(SONARR, path + ("?" + query if query else ""), CONF_VALUES.get("SONARR_KEY", ""), method=method)


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
            "path": m.get("path"), "folder": m.get("folderName"),
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


AI_STATE = os.path.join(os.environ.get("STATE_DIR", "/var/lib/severino-panel"), "ai_subs.json")


def _ai_load():
    try:
        return json.load(open(AI_STATE))
    except Exception:
        return {}


def _ai_mark(path, cues, source):
    d = _ai_load()
    d[path] = {"at": int(time.time()), "cues": cues, "source": source}
    os.makedirs(os.path.dirname(AI_STATE), exist_ok=True)
    with open(AI_STATE, "w") as f:
        json.dump(d, f)


LANG2 = {"eng": "EN", "por": "PT", "spa": "ES", "fre": "FR", "ger": "DE", "ita": "IT",
         "jpn": "JA", "kor": "KO", "chi": "ZH", "rus": "RU", "hin": "HI", "ara": "AR",
         "dut": "NL", "pol": "PL", "tur": "TR", "swe": "SV", "dan": "DA", "nor": "NO",
         "fin": "FI", "cze": "CS", "hun": "HU", "gre": "EL", "rum": "RO", "ukr": "UK"}


def _subs_for(m):
    """Faixas de legenda que o arquivo tem: embutidas (texto/imagem) + externas."""
    try:
        video = _movie_file(m)
    except Exception:
        return None
    subs = []
    try:
        for s in _probe(video):
            codec = (s.get("codec_name") or "").lower()
            tags = s.get("tags") or {}
            code = (tags.get("language") or "").lower()
            title = (tags.get("title") or "").lower()
            if codec in TEXT_CODECS:
                label = LANG2.get(code, code.upper() or "??")
                if "sdh" in title or "cc" in title:
                    label += "·SDH"
                subs.append(label)
            elif "pgs" in codec or "vobsub" in codec or "dvd" in codec:
                subs.append("IMAGEM")
    except Exception:
        pass
    stem = os.path.splitext(os.path.basename(video))[0]
    folder = os.path.dirname(video)
    try:
        for f in sorted(os.listdir(folder)):
            low = f.lower()
            if low.startswith(stem.lower() + ".") and low.endswith((".srt", ".ass", ".vtt")):
                code = f[len(stem) + 1:].rsplit(".", 1)[0]
                label = code.upper().replace("_", "-")
                subs.append(label)
    except OSError:
        pass
    ai_file = os.path.join(folder, stem + ".IA.pt-BR.srt")
    ai = _ai_load().get(video) or ({"at": int(os.path.getmtime(ai_file))} if os.path.exists(ai_file) else None)
    seen, out = set(), []
    for x in subs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    has_pt = any(s.startswith("PT") for s in out)
    return {"file": video, "subs": out, "ai": bool(ai), "has_pt": has_pt,
            "ai_at": (ai or {}).get("at")}


def subtitles_catalog():
    def build():
        items = []
        for m in movies():
            if not m.get("has_file"):
                continue
            info = _subs_for(m)
            if info is None:
                continue
            items.append({"id": m["id"], "title": m["title"], "year": m["year"],
                          "size_gb": m["size_gb"], "subs": info["subs"], "ai": info["ai"],
                          "has_pt": info["has_pt"], "ai_at": info["ai_at"]})
        items.sort(key=lambda x: (x["has_pt"], -x["size_gb"]))
        return {"items": items}
    return cached("subtitles_catalog", 60, build)


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


def sync_all():
    """Depois de mexer na biblioteca: Seerr revarre Radarr/Sonarr e o Jellyfin relê as pastas."""
    out = {}
    for job in ("radarr-scan", "sonarr-scan"):
        try:
            seerr("/api/v1/settings/jobs/%s/run" % job, method="POST")
            out[job] = "ok"
        except Exception as e:
            out[job] = str(e)
    try:
        jellyfin("/Library/Refresh", method="POST")
        out["jellyfin"] = "ok"
    except Exception as e:
        out["jellyfin"] = str(e)
    for k in ("library", "requests", "overview", "subtitles_catalog"):
        _CACHE.pop(k, None)
    return out


def delete(kind, item_id, files=True):
    """Exclui no Radarr/Sonarr e CONFIRMA: nunca responde sucesso sem checar."""
    base = radarr if kind == "movie" else sonarr
    path = "/api/v3/movie/%s" % item_id if kind == "movie" else "/api/v3/series/%s" % item_id
    base(path + "?deleteFiles=%s&addImportExclusion=false" % ("true" if files else "false"), method="DELETE")
    still = base(path.replace("/%s" % item_id, ""))  # lista tudo
    ids = [x.get("id") for x in (still or [])]
    if item_id in ids:
        raise RuntimeError("o Radarr aceitou mas o item continua na biblioteca")
    _CACHE.pop("library", None)
    _CACHE.pop("overview", None)
    return {"deleted": True, "sync": sync_all()}


def subtitle_search(kind, item_id):
    code = bazarr_form("/api/movies/subtitles", {"radarrid": int(item_id), "language": SUBTITLE_LANG,
                                                 "forced": "False", "hi": "False"})
    return {"status": code}


def add_event(kind, title):
    EVENTS.append({"at": int(time.time()), "kind": kind, "title": title})


# ======================================================================================
# Legendas com IA: extrai a legenda do idioma original do próprio arquivo, valida o
# sincronismo contra o áudio, traduz com o DeepSeek e grava "<video>.pt-BR.srt".
# Sem OCR e sem ASR: se não houver legenda de TEXTO no idioma original, o job falha
# com uma mensagem clara.
# ======================================================================================

FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
FFPROBE = "/usr/lib/jellyfin-ffmpeg/ffprobe"
TEXT_CODECS = ("subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text")
LANG3 = {"en": "eng", "pt": "por", "es": "spa", "fr": "fre", "de": "ger", "it": "ita",
         "ja": "jpn", "ko": "kor", "zh": "chi", "ru": "rus", "hi": "hin", "ar": "ara"}
JOBS = {}
_JOBS_LOCK = threading.Lock()
_JOB_SEQ = [0]


HOST_MEDIA, CONT_MEDIA = "/DATA/Media", "/Media"


def _cont(path):
    """O container do Jellyfin vê /DATA/Media como /Media."""
    return CONT_MEDIA + path[len(HOST_MEDIA):] if path.startswith(HOST_MEDIA) else path


def _docker(args, timeout=600):
    """Roda ffmpeg/ffprobe dentro do container do Jellyfin (já tem os binários)."""
    r = subprocess.run(["docker", "exec", "jellyfin"] + args, capture_output=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def _probe(path):
    code, out, err = _docker([FFPROBE, "-v", "error", "-select_streams", "s", "-show_entries",
                              "stream=index,codec_name:stream_tags=language,title", "-of", "json", _cont(path)], 120)
    if code != 0:
        raise RuntimeError("ffprobe falhou: " + err.decode("utf-8", "replace")[:200])
    return json.loads(out or b"{}").get("streams", [])


def _movie_file(movie):
    """Arquivo de vídeo do filme. O Radarr responde com caminhos do container (/data/...)."""
    base = movie.get("path") or movie.get("folder") or ""
    if base.startswith("/data/"):
        base = "/DATA/" + base[len("/data/"):]     # caminho do container -> caminho do host
    if not base or not os.path.isdir(base):
        raise RuntimeError("pasta do filme não encontrada: %r" % base)
    for root, _d, files in os.walk(base):
        for f in files:
            if f.lower().endswith((".mkv", ".mp4", ".avi", ".m4v")):
                return os.path.join(root, f)
    raise RuntimeError("arquivo de vídeo não encontrado em " + base)


def _pick_source(video, orig2):
    """A melhor legenda de TEXTO no idioma original: embutida (sem SDH) > SDH > externa."""
    orig3 = LANG3.get((orig2 or "en").lower(), (orig2 or "en").lower())
    streams = _probe(video)
    text = [s for s in streams if (s.get("codec_name") or "").lower() in TEXT_CODECS]
    same = [s for s in text if ((s.get("tags") or {}).get("language") or "").lower() in (orig3, orig2)]
    plain = [s for s in same if "sdh" not in (((s.get("tags") or {}).get("title") or "").lower())]
    for group in (plain, same, text):
        if group:
            s = group[0]
            return {"kind": "embedded", "index": s["index"], "codec": s.get("codec_name"),
                    "lang": (s.get("tags") or {}).get("language") or "?"}
    # externa: "<video>.<lang>.srt"
    stem = os.path.splitext(video)[0]
    folder = os.path.dirname(video)
    for code in (orig2, orig3, "en"):
        for f in sorted(os.listdir(folder)):
            low = f.lower()
            if low.endswith((".srt", ".ass", ".vtt")) and (("." + code + ".") in low or low.endswith("." + code + ".srt")):
                return {"kind": "external", "path": os.path.join(folder, f), "lang": code}
    return None


def _extract(source, video, out):
    if source["kind"] == "embedded":
        code, data, err = _docker([FFMPEG, "-v", "error", "-i", _cont(video), "-map", "0:%d" % source["index"],
                                   "-f", "srt", "-"], 600)
        if code != 0 or not data.strip():
            raise RuntimeError("extração falhou: " + err.decode("utf-8", "replace")[:200])
        open(out, "wb").write(data)
    else:
        import shutil
        shutil.copyfile(source["path"], out)


def _parse_srt(path):
    import re
    raw = open(path, encoding="utf-8-sig", errors="replace").read().replace("\r\n", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = [l for l in block.split("\n")]
        times = next((l for l in lines if "-->" in l), None)
        if not times:
            continue
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", times)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
        idx = times.find("-->")
        body = "\n".join(lines[lines.index(times) + 1:]).strip()
        cues.append({"start": start, "end": end, "text": body})
    return cues


def _ts(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def _wrap(text, width=42):
    """Quebra em no máximo 2 linhas de 42 caracteres, em limite de palavra."""
    t = " ".join(text.split())
    if len(t) <= width:
        return t
    if len(t) <= 2 * width:
        mid = len(t) // 2
        left = t.rfind(" ", 0, mid + 1)
        right = t.find(" ", mid)
        cut = left if left > 0 and (right < 0 or mid - left <= right - mid) else right
        if cut > 0:
            return t[:cut].strip() + "\n" + t[cut:].strip()
    return t


def _write_srt(cues, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, c in enumerate(cues, 1):
            f.write("%d\n%s --> %s\n%s\n\n" % (i, _ts(c["start"]), _ts(c["end"]), _wrap(c["text"]).strip()))


def _audio_energy(video, start, dur, step=0.1):
    import array
    code, raw, _err = _docker([FFMPEG, "-v", "error", "-ss", "%.2f" % start, "-t", "%.2f" % dur,
                               "-i", _cont(video), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], 300)
    if code != 0 or not raw:
        return []
    a = array.array("h")
    usable = len(raw) // 2 * 2
    a.frombytes(raw[:usable])
    n = int(8000 * step)
    out = []
    for i in range(0, len(a) - n, n):
        chunk = a[i:i + n]
        out.append(sum(abs(x) for x in chunk) / float(n))
    return out


# 23,976 (filme) x 25 (PAL): a legenda de uma versão "escapa" da outra ~4,27% por hora.
# Só 1,0: a correlação por energia não é confiável em busca larga (ver README); o drift
# precisaria de fingerprint de áudio (chromaprint) ou timestamps de ASR.
SCALES = (1.0,)


def _activity(cues, scale, start, count, step=0.1):
    """Para cada 100 ms da janela: 1 se há fala da legenda (já escalada), senão 0."""
    out = [0.0] * count
    for c in cues:
        a, b = c["start"] * scale, c["end"] * scale
        i0 = int((a - start) / step)
        i1 = int((b - start) / step)
        for i in range(max(0, i0), min(count, i1 + 1)):
            out[i] = 1.0
    return out


def _window_shift(video, cues, start, win, scale=1.0, max_shift=8.0, step=0.1):
    """Melhor desvio (s) desta janela, com a legenda já escalada."""
    energy = _audio_energy(video, start, win)
    if not energy:
        return None
    n = len(energy)
    norm_e = sum(e * e for e in energy) or 1.0
    act = _activity(cues, scale, start, n)
    act_total = sum(act) or 1.0          # constante: deslocar não cria nem apaga fala
    pad = int(max_shift / 0.1)
    ext = [0.0] * pad + act + [0.0] * pad
    step_n = int(step / 0.1) or 1
    best = (-1.0, 0)
    for shift in range(-pad, pad + 1, step_n):
        dot = 0.0
        active = 0
        for i in range(n):
            j = i - shift + pad
            if 0 <= j < len(ext) and ext[j]:
                dot += energy[i]
                active += 1
        if dot <= 0 or active < act_total * 0.5:   # pouca sobreposição = artefato de borda
            continue
        score = dot / ((norm_e * act_total) ** 0.5)
        if score > best[0]:
            best = (score, shift)
    return best[1] * 0.1, best[0]


def _robust(shifts, tol=0.6):
    """Mediana e inliers: uma cena com música derruba só ela, não o resultado."""
    if not shifts:
        return None
    vals = sorted(s for _t, s in shifts)
    med = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
    inl = [(t, s) for t, s in shifts if abs(s - med) <= tol]
    spread = (max(s for _t, s in inl) - min(s for _t, s in inl)) if inl else 99.0
    return med, inl, spread


def _estimate_sync(video, cues, duration):
    """Desvio e deriva (fps) entre legenda e áudio, com mediana + inliers por escala.

    A escala só ganha de 1,0 se tiver mais janelas concordando — é a trava para não
    mexer numa legenda que já está certa.
    """
    if not cues or not duration:
        return 1.0, 0.0, 0.0, 0
    windows = 1 if duration < 900 else int(os.environ.get("AI_WINDOWS", "5"))
    win = float(os.environ.get("AI_WINDOW_S", "120"))
    results = {}
    for scale in SCALES:
        pts = []
        for k in range(windows):
            start = max(0.0, duration * (k + 0.5) / windows - win / 2.0)
            got = _window_shift(video, cues, start, win, scale=scale)
            if got:
                pts.append((start + win / 2.0, got[0]))
        r = _robust(pts)
        if r:
            results[scale] = r
    if not results:
        return 1.0, 0.0, 0.0, 0
    one = results.get(1.0)
    best_scale = max(results, key=lambda sc: (len(results[sc][1]), -results[sc][2]))
    med, inl, spread = results[best_scale]
    if best_scale != 1.0:
        # escala só entra se tiver pelo menos uma janela a mais que 1,0 e dispersão baixa
        if not one or len(inl) < len(one[1]) + 1 or spread > 0.5:
            best_scale = 1.0
            med, inl, spread = one if one else (0.0, [], 99.0)
    if best_scale == 1.0 and abs(med) < 1.0:
        med = 0.0                       # ruído: não mexe
    conf = round(len(inl) / float(windows), 2)
    return round(best_scale, 4), round(med, 2), conf, len(inl)


def _shift(cues, delta):
    for c in cues:
        c["start"] = max(0.0, c["start"] + delta)
        c["end"] = max(0.0, c["end"] + delta)
    return cues


def _deepseek(messages, max_tokens=8000):
    key = CONF_VALUES.get("DEEPSEEK_KEY", "")
    if not key:
        raise RuntimeError("DEEPSEEK_KEY ausente no keys.env")
    body = json.dumps({"model": "deepseek-chat", "messages": messages, "temperature": 0.2,
                       "max_tokens": max_tokens}).encode()
    req = urllib.request.Request("https://api.deepseek.com/chat/completions", data=body, method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=240) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"]


def _context(tmdb_id, media_type="movie"):
    try:
        d = seerr("/api/v1/%s/%s" % ("tv" if media_type == "tv" else "movie", tmdb_id)) or {}
        return {"title": d.get("title") or d.get("name"), "year": (d.get("releaseDate") or d.get("firstAirDate") or "")[:4],
                "original_title": d.get("originalTitle") or d.get("originalName"),
                "original_language": d.get("originalLanguage"),
                "overview": (d.get("overview") or "")[:900],
                "genres": [g.get("name") for g in (d.get("genres") or [])][:6]}
    except Exception:
        return {"title": "", "year": "", "original_language": "en", "overview": "", "genres": []}


SYSTEM = (
    "Você traduz legendas de filmes para português do Brasil.\n"
    "Regras obrigatórias:\n"
    "- Traduza apenas o texto; nunca invente, resuma ou comente.\n"
    "- Mantenha as tags <i> e </i> exatamente onde estão.\n"
    "- Uma tradução por item: não junte nem divida falas.\n"
    "- Português brasileiro natural e curto, com no máximo 42 caracteres por linha e no máximo 2 linhas, "
    "usando \\n entre elas.\n"
    "- Nomes próprios, marcas e títulos: mantenha (ou a forma consagrada no Brasil).\n"
    "- Diálogo incompreensível: traduza literalmente, sem inventar.\n"
    "Responda SOMENTE com JSON válido: {\"items\":[{\"i\":<id>,\"t\":\"<texto>\"}]}"
)


def _translate(cues, ctx, progress):
    out = []
    batch = 80
    for start in range(0, len(cues), batch):
        part = cues[start:start + batch]
        payload = {"i": i, "t": c["text"]} if False else [{"i": i, "t": c["text"]} for i, c in enumerate(part)]
        user = ("CONTEÚDO: %s (%s), título original \"%s\", idioma original %s. Gêneros: %s.\n"
                "SINOPSE: %s\n\nTraduza as %d falas abaixo para pt-BR e devolva o JSON com os mesmos ids.\n%s"
                % (ctx.get("title"), ctx.get("year"), ctx.get("original_title"), ctx.get("original_language"),
                   ", ".join(ctx.get("genres") or []), ctx.get("overview"), len(part),
                   json.dumps(payload, ensure_ascii=False)))
        got = {}
        for attempt in range(2):
            try:
                txt = _deepseek([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
                m = re.search(r"\{.*\}", txt, re.S)
                data = json.loads(m.group(0) if m else txt)
                for it in data.get("items", []):
                    if isinstance(it.get("i"), int) and isinstance(it.get("t"), str):
                        got[it["i"]] = it["t"]
                if len(got) >= len(part) * 0.9:
                    break
            except Exception:
                if attempt == 1:
                    raise
        for i, c in enumerate(part):
            t = got.get(i)
            out.append({"start": c["start"], "end": c["end"], "text": (t if t else c["text"])})
        progress(min(95, 10 + int(85 * (start + len(part)) / max(1, len(cues)))))
    return out


def _job_set(job, **kw):
    with _JOBS_LOCK:
        JOBS.setdefault(job, {}).update(kw)


def _job_run(job, movie_id):
    try:
        _job_set(job, state="running", pct=2, step="lendo o filme")
        m = next((x for x in movies() if x["id"] == movie_id), None)
        if not m:
            raise RuntimeError("filme %s não está no Radarr" % movie_id)
        video = _movie_file(m)
        ctx = _context(m.get("tmdb"))
        orig = (ctx.get("original_language") or "en")[:2]
        _job_set(job, pct=8, step="escolhendo a legenda fonte (idioma original: %s)" % orig)
        src = _pick_source(video, orig)
        if not src:
            raise RuntimeError("sem legenda de TEXTO no idioma original (%s). OCR/ASR desativados." % orig)
        tmp = "/tmp/subai-%s.srt" % job
        _extract(src, video, tmp)
        cues = _parse_srt(tmp)
        if not cues:
            raise RuntimeError("legenda fonte vazia")
        _job_set(job, pct=20, step="validando sincronismo contra o áudio",
                 source=("%s %s" % (src["kind"], src.get("lang") or "")))
        dur = 0.0
        try:
            dur = float(m.get("movieFile", {}).get("mediaInfo", {}).get("runTime") or 0)
        except Exception:
            dur = 0.0
        if not dur:
            try:
                d = seerr("/api/v1/movie/%s" % m.get("tmdb")) or {}
                dur = float(d.get("runtime") or 0) * 60
            except Exception:
                dur = 0.0
        scale, offset, conf, used = _estimate_sync(video, cues, dur)
        if scale != 1.0:
            for c in cues:
                c["start"] = max(0.0, c["start"] * scale + offset)
                c["end"] = max(0.0, c["end"] * scale + offset)
        elif abs(offset) >= 0.5:
            cues = _shift(cues, offset)
        _job_set(job, pct=30, step="traduzindo %d falas" % len(cues),
                 offset_s=offset, scale=scale, windows=used, offset_conf=conf)
        out = _translate(cues, ctx, lambda p: _job_set(job, pct=p))
        stem = os.path.splitext(video)[0]
        # Jellyfin lê "<titulo>.<idioma>.srt": assim a faixa aparece como "IA - Portuguese"
        dst = stem + ".IA.pt-BR.srt"
        _job_set(job, pct=97, step="gravando " + os.path.basename(dst))
        _write_srt(out, dst)
        try:
            _ai_mark(video, len(out), src["kind"])
        except Exception:
            pass
        _CACHE.pop("subtitles_catalog", None)
        try:
            jellyfin("/Library/Refresh", method="POST")
        except Exception:
            pass
        _job_set(job, state="done", pct=100, step="pronto", out=dst,
                 cues=len(out), source_kind=src["kind"])
    except Exception as e:
        _job_set(job, state="error", pct=100, step="falhou", error=str(e))


def start_ai_job(movie_id):
    with _JOBS_LOCK:
        _JOB_SEQ[0] += 1
        job = "j%d" % _JOB_SEQ[0]
        JOBS[job] = {"state": "queued", "pct": 0, "step": "na fila", "at": int(time.time())}
    threading.Thread(target=_job_run, args=(job, movie_id), daemon=True).start()
    return job


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
                return self._send(200, subtitles_catalog())
            if path == "/recycle":
                return self._send(200, recycle_info())
            if path.startswith("/jobs/"):
                j = JOBS.get(path.split("/")[-1])
                return self._send(200 if j else 404, j or {"error": "job"})
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
            if path == "/sync":
                return self._send(200, sync_all())
            if path == "/delete":
                return self._send(200, delete(b.get("kind", "movie"), b["id"], b.get("files", True)))
            if path == "/subtitles/ai":
                return self._send(200, {"job": start_ai_job(int(b["id"]))})
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
