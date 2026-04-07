import io
import json
import os
import shutil
import subprocess
import urllib.parse
from urllib import request as rq

import eyed3
from PIL import Image
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from yt_dlp import YoutubeDL
from dotenv import load_dotenv

load_dotenv()

download_base_path = "/Users/ozzy/Music/Music/Media.localized/Music"
LINKS_FILE = os.path.join(os.path.dirname(__file__), "links.txt")

sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=os.environ.get("CLIENT_ID"),
    client_secret=os.environ.get("CLIENT_SECRET"),
))


def normalize(s):
    return s.translate(str.maketrans('\\/:*?"<>|', "__       "))


def make_dir(rel_path):
    full = os.path.join(download_base_path, rel_path)
    os.makedirs(full, exist_ok=True)
    return full


def ydl_opts(path):
    return {
        "format": "bestaudio/best",
        "outtmpl": f"{path}/%(id)s.%(ext)s",
        "ignoreerrors": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }],
    }


def save_cover(art_url, path):
    dest = os.path.join(path, "cover.png")
    if os.path.exists(dest) or not art_url:
        return
    try:
        data = rq.urlopen(art_url).read()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.save(dest, "PNG", compress_level=0)
    except Exception as e:
        print(f"  cover art failed: {e}")


def trim_silence(file_path, trim_start=True, trim_end=True):
    if not trim_start and not trim_end:
        return
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return
    filters = []
    if trim_start:
        filters.append("silenceremove=start_periods=1:start_duration=0.05:start_threshold=-91dB")
    if trim_end:
        filters += ["areverse", "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-91dB", "areverse"]
    tmp = file_path + ".tmp.mp3"
    try:
        subprocess.run(
            [ffmpeg, "-i", file_path, "-af", ",".join(filters),
             "-map_metadata", "0", "-id3v2_version", "3",
             "-c:a", "libmp3lame", "-b:a", "320k", tmp, "-y"],
            check=True, capture_output=True
        )
        os.replace(tmp, file_path)
    except Exception as e:
        print(f"  couldn't trim {os.path.basename(file_path)}: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)


def tag_file(track_id, track, path):
    mp3_path = f"{path}/{track_id}.mp3"
    audiofile = eyed3.load(mp3_path)
    if not audiofile:
        print(f"  couldn't tag {track_id}.mp3")
        return
    audiofile.initTag(version=eyed3.id3.ID3_V2_3)
    audiofile.tag.title = track["track_name"]
    audiofile.tag.artist = track["artist_name"]
    audiofile.tag.album = track["album_name"]
    audiofile.tag.album_artist = track["artist_name"]
    audiofile.tag.track_num = (track["track_number"], track["total_tracks"])
    try:
        year = int(str(track["album_date"])[:4])
        audiofile.tag.recording_date = eyed3.core.Date(year)
    except (ValueError, TypeError):
        pass
    try:
        raw = rq.urlopen(track["album_art"]).read()
        buf = io.BytesIO()
        Image.open(io.BytesIO(raw)).convert("RGB").save(buf, "PNG", compress_level=0)
        audiofile.tag.images.set(eyed3.id3.frames.ImageFrame.FRONT_COVER, buf.getvalue(), "image/png")
    except Exception as e:
        print(f"  album art embed failed: {e}")
    audiofile.tag.save(version=eyed3.id3.ID3_V2_3)
    os.rename(mp3_path, f"{path}/{track['file_name']}.mp3")


def find_youtube_url(track, skip_url=None):
    query = f"{track['artist_name']} {track['album_name']} {track['track_name']}"
    skip_id = skip_url.split("v=")[-1] if skip_url and "v=" in skip_url else None
    with YoutubeDL({"quiet": True, "skip_download": True, "extract_flat": True}) as ydl:
        result = ydl.extract_info(f"ytsearch5:{query}", download=False)
        if not result or not result.get("entries"):
            return None
        entries = [e for e in result["entries"] if e and e.get("id") != skip_id]
        if not entries:
            return None
        for entry in entries:
            uploader = entry.get("uploader") or entry.get("channel") or ""
            if "- Topic" in uploader:
                return f"https://www.youtube.com/watch?v={entry['id']}"
        return f"https://www.youtube.com/watch?v={entries[0]['id']}"


def find_archive_url(track):
    query = f"{track['artist_name']} {track['track_name']}"
    search = "https://archive.org/advancedsearch.php?" + urllib.parse.urlencode([
        ("q", f"{query} AND mediatype:audio"),
        ("fl[]", "identifier"),
        ("output", "json"),
        ("rows", "3"),
    ])
    try:
        with rq.urlopen(search, timeout=8) as resp:
            items = json.loads(resp.read()).get("response", {}).get("docs", [])
        name_lower = track["track_name"].lower()
        spotify_secs = track.get("duration_ms", 0) / 1000
        for item in items:
            identifier = item.get("identifier", "")
            with rq.urlopen(f"https://archive.org/metadata/{identifier}/files", timeout=8) as resp:
                files = json.loads(resp.read()).get("result", [])
            for f in files:
                fname = f.get("name", "")
                if name_lower in fname.lower() and fname.lower().endswith((".mp3", ".flac", ".ogg")):
                    length = float(f.get("length") or 0)
                    if spotify_secs and length and abs(length - spotify_secs) > 5:
                        continue
                    return f"https://archive.org/download/{identifier}/{fname}"
    except Exception:
        pass
    return None


def find_url(track, skip_url=None):
    if not skip_url:
        url = find_archive_url(track)
        if url:
            print("  (archive.org)", end=" ", flush=True)
            return url
    return find_youtube_url(track, skip_url=skip_url)


def download_tracks(tracks, path, label, results):
    existing = os.listdir(path)
    total = len(tracks)
    pending = [(i, t) for i, t in enumerate(tracks) if f"{t['file_name']}.mp3" not in existing]
    print(f"\n{label}  ({len(pending)} to download)")
    with YoutubeDL(ydl_opts(path)) as ydl:
        for list_idx, track in pending:
            print(f"  {track['track_name']}", end="", flush=True)
            url = find_url(track)
            if not url:
                print(f"\n  skip  {track['track_name']}  (no result)")
                results.append({"track": track["track_name"], "album": track["album_name"], "status": "missing", "diff": None, "_track": track, "_path": path, "_url": None, "_index": list_idx, "_total": total})
                continue
            info = ydl.extract_info(url, download=False)
            if not info:
                print(f"\n  skip  {track['track_name']}  (couldn't extract)")
                results.append({"track": track["track_name"], "album": track["album_name"], "status": "missing", "diff": None, "_track": track, "_path": path, "_url": url, "_index": list_idx, "_total": total})
                continue
            ydl.download([url])
            tag_file(info["id"], track, path)
            print("  ✓")
            final_path = os.path.join(path, f"{track['file_name']}.mp3")
            af = eyed3.load(final_path)
            actual_duration = af.info.time_secs if af and af.info else (info.get("duration") or 0)
            spotify_duration = track.get("duration_ms", 0) / 1000
            diff = abs(actual_duration - spotify_duration)
            results.append({"track": track["track_name"], "album": track["album_name"], "status": "ok", "diff": diff, "_track": track, "_path": path, "_url": url, "_index": list_idx, "_total": total})


def get_album(uri):
    album = sp.album(uri)
    artist_name = normalize(album["artists"][0]["name"])
    album_name = normalize(album["name"])
    album_date = album["release_date"]
    album_art = album["images"][0]["url"]
    total_tracks = album["tracks"]["total"]

    tracks = []
    items = album["tracks"]["items"]
    offset = 0
    while items:
        for item in items:
            tn = normalize(item["name"])
            an = normalize(item["artists"][0]["name"])
            tracks.append({
                "track_name": tn, "artist_name": an, "album_name": album_name,
                "album_date": album_date, "album_art": album_art,
                "track_number": item["track_number"], "total_tracks": total_tracks,
                "duration_ms": item.get("duration_ms", 0),
                "file_name": tn,
            })
        offset += len(items)
        items = sp.album_tracks(uri, offset=offset)["items"]

    path = make_dir(os.path.join(artist_name, album_name))
    save_cover(album_art, path)
    results = []
    download_tracks(tracks, path, album_name, results)
    return results


def get_playlist(uri):
    fields = "items.track.track_number,items.track.name,items.track.artists.name,items.track.album.name,items.track.album.release_date,total,items.track.album.images"
    pl_name = sp.playlist(uri)["name"]

    tracks = []
    offset = 0
    items = sp.playlist_items(uri, offset=offset, fields=fields, additional_types=["track"])["items"]
    while items:
        for item in items:
            if not item["track"]:
                continue
            tn = normalize(item["track"]["name"])
            an = normalize(item["track"]["artists"][0]["name"])
            tracks.append({
                "track_name": tn, "artist_name": an,
                "album_name": normalize(item["track"]["album"]["name"]),
                "album_date": item["track"]["album"]["release_date"],
                "album_art": item["track"]["album"]["images"][0]["url"],
                "track_number": item["track"]["track_number"],
                "duration_ms": item["track"].get("duration_ms", 0),
                "total_tracks": None, "file_name": tn,
            })
        offset += len(items)
        items = sp.playlist_items(uri, offset=offset, fields=fields, additional_types=["track"])["items"]

    for t in tracks:
        t["total_tracks"] = len(tracks)

    path = make_dir(os.path.join("Playlists", normalize(pl_name)))
    save_cover(tracks[0]["album_art"] if tracks else None, path)
    results = []
    download_tracks(tracks, path, pl_name, results)
    return results


def print_summary(all_results):
    print("")
    for r in all_results:
        label = f"{r['track']}  ({r['album']})"
        if r["status"] == "missing":
            print(f"  \033[31m✗  {label}\033[0m")
        elif r["diff"] is None or r["diff"] <= 1:
            print(f"  \033[32m✓  {label}\033[0m")
        elif r["diff"] <= 2:
            print(f"  \033[33m?  {label}  {r['diff']:.1f}s off\033[0m")
        else:
            print(f"  \033[31m✗  {label}  {r['diff']:.1f}s off\033[0m")
    missing = [r for r in all_results if r["status"] == "missing"]
    warned  = [r for r in all_results if r["diff"] is not None and 1 < r["diff"] <= 2]
    bad     = [r for r in all_results if r["diff"] is not None and r["diff"] > 2]
    ok = len(all_results) - len(missing) - len(warned) - len(bad)
    print(f"\n  {ok}/{len(all_results)} ok", end="")
    if warned:
        print(f"  \033[33m{len(warned)} warning\033[0m", end="")
    if missing or bad:
        print(f"  \033[31m{len(missing) + len(bad)} bad\033[0m", end="")
    print("\n")


def redo_tracks(candidates):
    print("tracks that need attention:")
    for i, r in enumerate(candidates):
        diff_str = f"{r['diff']:.1f}s off" if r["diff"] is not None else "missing"
        print(f"  {i+1}.  {r['track']}  ({diff_str})")
    choice = input("\nredo which? (e.g. 1 3, or 'all', or enter to skip): ").strip()
    if not choice:
        return []
    selected = candidates if choice.lower() == "all" else [
        candidates[int(n) - 1] for n in choice.split() if n.isdigit() and 1 <= int(n) <= len(candidates)
    ]
    redo_results = []
    for r in selected:
        track = r["_track"]
        path = r["_path"]
        existing_file = os.path.join(path, f"{track['file_name']}.mp3")
        if os.path.exists(existing_file):
            os.remove(existing_file)
        url = find_url(track, skip_url=r.get("_url"))
        if not url:
            print(f"  still no result for {track['track_name']}")
            redo_results.append({**r, "status": "missing", "diff": None})
            continue
        with YoutubeDL(ydl_opts(path)) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                redo_results.append({**r, "status": "missing", "diff": None})
                continue
            ydl.download([url])
            tag_file(info["id"], track, path)
            final_path = os.path.join(path, f"{track['file_name']}.mp3")
            af = eyed3.load(final_path)
            actual_duration = af.info.time_secs if af and af.info else (info.get("duration") or 0)
            diff = abs(actual_duration - track.get("duration_ms", 0) / 1000)
            redo_results.append({**r, "status": "ok", "diff": diff, "_url": url})
    return redo_results


def trim_bad_tracks(all_results):
    bad = [r for r in all_results if r["status"] == "ok" and r["diff"] is not None and r["diff"] > 2]
    if not bad:
        return False
    print(f"trimming silence from {len(bad)} track(s)...")
    for r in bad:
        track = r["_track"]
        path = r["_path"]
        list_idx = r.get("_index", 1)
        total = r.get("_total", 2)
        file_path = os.path.join(path, f"{track['file_name']}.mp3")
        if not os.path.exists(file_path):
            continue
        trim_silence(file_path, trim_start=list_idx != 0, trim_end=list_idx != total - 1)
        af = eyed3.load(file_path)
        if af and af.info:
            r["diff"] = abs(af.info.time_secs - track.get("duration_ms", 0) / 1000)
    return True


def run():
    with open(LINKS_FILE) as f:
        links = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]

    if not links:
        print("nothing in links.txt")
        return

    all_results = []
    for link in links:
        if "album" in link:
            all_results.extend(get_album(link))
        elif "playlist" in link:
            all_results.extend(get_playlist(link))
        else:
            print(f"unknown link: {link}")

    print_summary(all_results)

    if trim_bad_tracks(all_results):
        print_summary(all_results)

    retry_candidates = [r for r in all_results if r["status"] == "missing" or (r["diff"] is not None and r["diff"] > 1)]
    if retry_candidates:
        redo_results = redo_tracks(retry_candidates)
        if redo_results:
            for redo in redo_results:
                for i, r in enumerate(all_results):
                    if r["track"] == redo["track"] and r["album"] == redo["album"]:
                        all_results[i] = redo
                        break
            trim_bad_tracks(all_results)
            print_summary(all_results)

    print("done.")


run()
