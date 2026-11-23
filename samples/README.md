# samples

Real footage is intentionally NOT included in this repository (copyright + size).
Put your own media here when running the programs:

1. Filenames/paths must be **ASCII only** (cv2 cannot open non-ASCII paths)
2. Supported: `.mp4 / .avi / .mov / .mkv`; for `.m4s` fragments remux first:
   `ffmpeg -i in.m4s -c copy out.mp4`
