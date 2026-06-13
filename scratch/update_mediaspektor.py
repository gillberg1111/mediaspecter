#!/usr/bin/env python3
import re
import os

def main():
    mp4_path = "scratch/videos/dummy_mp4_base64.txt"
    mkv_path = "scratch/videos/dummy_mkv_base64.txt"
    avi_path = "scratch/videos/dummy_avi_base64.txt"
    
    if not (os.path.exists(mp4_path) and os.path.exists(mkv_path) and os.path.exists(avi_path)):
        print("Error: Base64 video text files not found in scratch/videos/")
        return
        
    with open(mp4_path, "r") as f:
        mp4_b64 = f.read().strip()
    with open(mkv_path, "r") as f:
        mkv_b64 = f.read().strip()
    with open(avi_path, "r") as f:
        avi_b64 = f.read().strip()

    with open("mediaspektor.py", "r") as f:
        content = f.read()

    replacement = f'''DUMMY_VIDEOS: dict[str, str] = {{
    ".mp4": "{mp4_b64}",
    ".mkv": "{mkv_b64}",
    ".avi": "{avi_b64}",
}}'''

    # Matches DUMMY_VIDEOS block down to its closing bracket
    pattern = r"DUMMY_VIDEOS:\s*dict\[str,\s*str\]\s*=\s*\{.*?\}"
    content_new, count = re.subn(pattern, replacement, content, flags=re.DOTALL)

    if count > 0:
        with open("mediaspektor.py", "w") as f:
            f.write(content_new)
        print("Successfully updated DUMMY_VIDEOS in mediaspektor.py")
    else:
        print("Error: Could not find DUMMY_VIDEOS block in mediaspektor.py")

if __name__ == "__main__":
    main()
