import os
import sys
import json
import shutil
import subprocess
import urllib.request
import urllib.parse
import asyncio
import traceback
import edge_tts

# Voix francaises (le public est africain: voix posee, claire)
VOICE = os.environ.get("TTS_VOICE", "fr-FR-RemyMultilingualNeural")
FALLBACK_VOICE = "fr-FR-HenriNeural"
RATE = "-5%"
FPS = 25
WORDS_PER_CUE = 4


def tg_send_text(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("CHAT_ID", "")
    if not token or not chat_id:
        return
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30
        )
    except Exception as e:
        print("Telegram text error:", e)


def srt_time(t100ns):
    ms = int(t100ns / 10000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


async def tts_once(text, voice, audio_path, srt_path):
    communicate = edge_tts.Communicate(text, voice, rate=RATE, boundary="WordBoundary")
    words = []
    with open(audio_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append((chunk["offset"], chunk["duration"], chunk["text"]))
    if os.path.getsize(audio_path) == 0:
        raise RuntimeError("audio vide")
    cues = []
    for i in range(0, len(words), WORDS_PER_CUE):
        grp = words[i:i + WORDS_PER_CUE]
        start = grp[0][0]
        end = grp[-1][0] + grp[-1][1]
        cues.append((start, end, " ".join(w[2] for w in grp)))
    with open(srt_path, "w", encoding="utf-8") as f:
        for n, (s, e, t) in enumerate(cues, 1):
            f.write(f"{n}\n{srt_time(s)} --> {srt_time(e)}\n{t}\n\n")


async def generate_voice_and_subs(text, audio_path, srt_path):
    last = None
    for attempt in range(3):
        voice = VOICE if attempt < 2 else FALLBACK_VOICE
        try:
            await tts_once(text, voice, audio_path, srt_path)
            return
        except Exception as e:
            last = e
            print(f"TTS tentative {attempt + 1} echouee: {e}")
            await asyncio.sleep(2)
    raise last


def get_audio_duration(audio_path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", audio_path]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def download_file(url, dest):
    last = None
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
                out.write(resp.read())
            if os.path.getsize(dest) < 1000:
                raise RuntimeError("image trop petite (lien Drive invalide ?)")
            return
        except Exception as e:
            last = e
    raise RuntimeError(f"Telechargement image impossible: {url} -> {last}")


def make_segment(idx, scene, width, height, font_size, margin_v):
    img = f"work/scene_{idx}.jpg"
    audio = f"work/scene_{idx}.mp3"
    srt = f"work/scene_{idx}.srt"
    seg = f"work/segment_{idx}.mp4"

    download_file(scene["image_url"], img)
    asyncio.run(generate_voice_and_subs(scene["narration_fr"], audio, srt))

    duration = get_audio_duration(audio) + 0.4
    frames = int(duration * FPS)

    style = (f"FontName=Liberation Sans,FontSize={font_size},Bold=1,"
             f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,"
             f"Outline=2,Shadow=1,Alignment=2,MarginV={margin_v}")
    vf = (
        f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
        f"crop={width * 2}:{height * 2},"
        f"zoompan=z='min(zoom+0.0007,1.12)':d={frames}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={FPS},"
        f"eq=contrast=1.05:saturation=1.05,"
        f"subtitles={srt}:force_style='{style}'"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", img, "-i", audio,
        "-vf", vf,
        "-af", "apad,loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
        "-t", f"{duration:.2f}", "-pix_fmt", "yuv420p", "-r", str(FPS),
        seg,
    ]
    subprocess.run(cmd, check=True)
    return seg


def publish_release(job_id, title, path):
    """Cree une release GitHub avec la video et renvoie le lien de telechargement."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo or not shutil.which("gh") or not os.environ.get("GH_TOKEN"):
        return ""
    tag = f"video-{job_id}"
    r = subprocess.run(
        ["gh", "release", "create", tag, path, "--repo", repo,
         "--title", title[:100], "--notes", f"Video generee automatiquement: {title}"],
        capture_output=True, text=True)
    print(r.stdout, r.stderr)
    if r.returncode != 0:
        return ""
    return f"https://github.com/{repo}/releases/download/{tag}/{os.path.basename(path)}"


def send_video_telegram(path, caption):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("CHAT_ID", "")
    if not token or not chat_id:
        return
    subprocess.run([
        "curl", "-s", "-X", "POST",
        f"https://api.telegram.org/bot{token}/sendVideo",
        "-F", f"chat_id={chat_id}", "-F", f"video=@{path}",
        "-F", "supports_streaming=true", "-F", f"caption={caption}",
    ])


def callback_n8n(url, data):
    if not url:
        return
    try:
        req = urllib.request.Request(
            url, data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=60)
    except Exception as e:
        print("Callback n8n erreur:", e)


def main():
    payload = json.loads(os.environ.get("STORY_PAYLOAD", "{}"))
    video_format = payload.get("format", "short")
    title = payload.get("title", "Histoire_IA")
    job_id = str(payload.get("job_id", "manual"))
    scenes = payload.get("scenes", [])
    callback_url = payload.get("callback_url", "")
    os.environ["CHAT_ID"] = str(payload.get("chat_id", ""))

    if not scenes:
        raise RuntimeError("Aucune scene recue dans le payload")

    os.makedirs("work", exist_ok=True)
    if video_format == "long":
        width, height, font_size, margin_v = 1920, 1080, 20, 60
    else:
        width, height, font_size, margin_v = 1080, 1920, 22, 300

    scenes = sorted(scenes, key=lambda s: s.get("numero", 0))
    segments = []
    for idx, scene in enumerate(scenes):
        print(f"--- Scene {idx + 1}/{len(scenes)}")
        segments.append(make_segment(idx, scene, width, height, font_size, margin_v))

    with open("work/concat_list.txt", "w", encoding="utf-8") as f:
        for seg in segments:
            f.write(f"file '{os.path.abspath(seg)}'\n")

    final = "output_story.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", "work/concat_list.txt", "-c", "copy", final], check=True)

    send_file = final
    if os.path.getsize(final) > 48 * 1024 * 1024:
        send_file = "output_telegram.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", final,
                        "-vf", "scale=-2:720", "-c:v", "libx264", "-crf", "28",
                        "-c:a", "aac", "-b:a", "128k", send_file], check=True)

    video_url = publish_release(job_id, title, final)
    caption = f"Montage termine: {title} ({video_format.upper()})"
    if video_url:
        caption += f"\nLien MP4: {video_url}"
    send_video_telegram(send_file, caption)
    if video_url and not os.path.exists(send_file):
        tg_send_text(caption)

    callback_n8n(callback_url, {
        "job_id": job_id,
        "status": "termine",
        "title": title,
        "format": video_format,
        "video_url": video_url,
        "chat_id": payload.get("chat_id", ""),
        "drive_videos_folder_id": payload.get("drive_videos_folder_id", ""),
        "scenes_count": len(scenes),
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        tg_send_text(f"Echec du montage GitHub: {e}")
        try:
            p = json.loads(os.environ.get("STORY_PAYLOAD", "{}"))
            callback_n8n(p.get("callback_url", ""), {
                "job_id": str(p.get("job_id", "manual")), "status": "erreur",
                "title": p.get("title", ""), "error": str(e)[:300],
                "chat_id": p.get("chat_id", ""),
                "drive_videos_folder_id": "", "video_url": "", "format": p.get("format", ""),
                "scenes_count": 0,
            })
        except Exception:
            pass
        sys.exit(1)
