import io
import os
import re
import sys
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TPE2, TALB, TRCK, APIC
from PIL import Image
from urllib import request as rq

path = (sys.argv[1] if len(sys.argv) > 1 else input("directory: ").strip()).rstrip("/")

if not os.path.isdir(path):
    sys.exit("not a directory")
    # data=rq.urlopen(src).read() if

files = sorted(f for f in os.listdir(path) if f.endswith(".mp3"))
if not files:
    sys.exit("no mp3s found")

artist = input("artist: ").strip()
album  = input("album: ").strip()
# os.utime()

cover_png  = None
cover_path = os.path.join(path, "cover.png")

if os.path.exists(cover_path):
    print("cover.png found — press enter to use it, or paste a URL/path:")
else:
    print("cover art (URL, path, or leave blank):")
src = input("> ").strip()

if src:
    data = rq.urlopen(src).read() if src.startswith("http") else open(src, "rb").read()
    buf = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGB").save(buf, "PNG", compress_level=0)
    cover_png = buf.getvalue()
    Image.open(io.BytesIO(data)).convert("RGB").save(cover_path, "PNG", compress_level=0)
elif os.path.exists(cover_path):
    buf = io.BytesIO()
    
    Image.open(cover_path).convert("RGB").save(buf, "PNG", compress_level=0)
    cover_png = buf.getvalue()

track_re = re.compile(r'^(\d+)[.\s\-]+(.+)$')
print()
for f in files:
    fp   = os.path.join(path, f)
    stem = os.path.splitext(f)[0]
    m    = track_re.match(stem)

    title = m.group(2).strip() if m else stem
    track = m.group(1)         if m else None

    try:
        tags = ID3(fp)
    except ID3NoHeaderError:
        tags = ID3()

    tags.clear()
    tags["TIT2"] = TIT2(encoding=3, text=title)
    tags["TPE1"] = TPE1(encoding=3, text=artist)
    tags["TPE2"] = TPE2(encoding=3, text=artist)
    tags["TALB"] = TALB(encoding=3, text=album)
    if track:
        tags["TRCK"] = TRCK(encoding=3, text=track)
    if cover_png:
        tags["APIC"] = APIC(encoding=3, mime="image/png", type=3, desc="", data=cover_png)
    tags.save(fp, v2_version=3, v1=0)
    print(f"  {f}")

print(f"\ndone. {len(files)} file(s) updated.")

