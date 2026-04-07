#!/usr/bin/env python3

import os
import sys
import eyed3
from mutagen.id3 import ID3, ID3NoHeaderError

eyed3.log.setLevel("ERROR")


def pick(label, existing):
    if existing:
        print(f"\n{label}:")
        for i, v in enumerate(existing):
            print(f"  {i+1}.  {v}")
        print(f"  {len(existing)+1}.  enter your own")
        raw = input("> ").strip()
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(existing):
                return existing[n - 1]
    return input(f"{label}: ").strip()


path = (sys.argv[1] if len(sys.argv) > 1 else input("directory: ").strip()).rstrip("/")

if not os.path.isdir(path):
    print(f"not a directory: {path}")
    sys.exit(1)

files = [f for f in os.listdir(path) if f.endswith(".mp3")]
if not files:
    print("no mp3s found")
    sys.exit(1)

artists, albums = set(), set()
loaded = []
for f in sorted(files):
    fp = os.path.join(path, f)
    try:
        tags = ID3(fp)
        artist = str(tags["TPE1"]) if "TPE1" in tags else None
        album  = str(tags["TALB"]) if "TALB" in tags else None
        if artist:
            artists.add(artist)
        if album:
            albums.add(album)
    except (ID3NoHeaderError, Exception):
        pass
    loaded.append((f, eyed3.load(fp)))

artist = pick("artist name", sorted(artists))
album  = pick("album title", sorted(albums))

if not artist or not album:
    print("need both artist and album")
    sys.exit(1)

print()
for f, af in loaded:
    if not af:
        print(f"  skip  {f}  (couldn't load)")
        continue
    if not af.tag:
        af.initTag(version=eyed3.id3.ID3_V2_3)
    af.tag.artist = artist
    af.tag.album = album
    af.tag.album_artist = artist
    af.tag.save(version=eyed3.id3.ID3_V2_3)
    print(f"  {f}")

print(f"\ndone.  {len(loaded)} file(s) updated.")

