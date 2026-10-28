import os
import json
import subprocess
import urllib.request
import asyncio
import edge_tts

VOICE = "fr-FR-RemyMultilingualNeural"
RATE = "-5%"
PITCH = "-2Hz"

async def generate_voice_and_subs(text, audio_path, srt_path):
    communicate = edge_tts.Communicate(text, VOICE, rate=RATE, pitch=PITCH)
    submaker = edge_tts.SubMaker()
    with open(audio_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                submaker.create_sub((chunk["offset"], chunk["duration"]), chunk["text"])
    with open(srt_path, "w", encoding="utf-8") as srt_file:
        srt_file.write(submaker.generate_subs())

def get_audio_duration(audio_path):
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", audio_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return float(result.stdout.strip())

def download_file(url, dest):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response, open(dest, 'wb') as out_file:
        out_file.write(response.read())

async def main():
    payload_raw = os.environ.get("STORY_PAYLOAD", "{}")
    payload = json.loads(payload_raw)
    
    video_format = payload.get("format", "short")
    title = payload.get("title", "Histoire_IA")
    chat_id = payload.get("chat_id", "")
    scenes = payload.get("scenes", [])
    
    os.makedirs("work", exist_ok=True)
    
    if video_format == "long":
        width, height = 1920, 1080
        font_size = 18
        margin_v = 45
    else:
        width, height = 1080, 1920
        font_size = 24
        margin_v = 280

    segment_files = []

    for idx, scene in enumerate(scenes):
        img_path = f"work/scene_{idx}.jpg"
        audio_path = f"work/scene_{idx}.mp3"
        srt_path = f"work/scene_{idx}.srt"
        seg_path = f"work/segment_{idx}.mp4"

        download_file(scene["image_url"], img_path)
        await generate_voice_and_subs(scene["narration_fr"], audio_path, srt_path)
        
        duration = get_audio_duration(audio_path) + 0.35
        frames = int(duration * 25)

        vf_filter = (
            f"scale={width*2}:{height*2}:force_original_aspect_ratio=increase,"
            f"crop={width*2}:{height*2},"
            f"zoompan=z='min(zoom+0.0008,1.15)':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps=25,"
            f"eq=contrast=1.06:saturation=0.95,vignette=PI/5,"
            f"subtitles={srt_path}:force_style='FontName=Liberation Sans,FontSize={font_size},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV={margin_v}'"
        )

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", img_path,
            "-i", audio_path,
            "-vf", vf_filter,
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-t", str(duration),
            "-pix_fmt", "yuv420p",
            seg_path
        ]
        subprocess.run(cmd, check=True)
        segment_files.append(seg_path)

    with open("work/concat_list.txt", "w", encoding="utf-8") as f:
        for seg in segment_files:
            f.write(f"file '{os.path.abspath(seg)}'\n")

    final_output = "output_story.mp4"
    concat_cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", "work/concat_list.txt",
        "-c", "copy", final_output
    ]
    subprocess.run(concat_cmd, check=True)

    send_file = final_output
    if os.path.getsize(final_output) > 48 * 1024 * 1024:
        compressed_output = "output_telegram.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-i", final_output,
            "-vf", "scale=-2:720", "-c:v", "libx264", "-crf", "28",
            "-c:a", "aac", "-b:a", "128k", compressed_output
        ], check=True)
        send_file = compressed_output

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if bot_token and chat_id:
        send_cmd = [
            "curl", "-s", "-X", "POST",
            f"https://api.telegram.org/bot{bot_token}/sendVideo",
            "-F", f"chat_id={chat_id}",
            "-F", f"video=@{send_file}",
            "-F", f"caption=✅ Montage terminé par l'usine AlBinZeti9 : {title} ({video_format.upper()})"
        ]
        subprocess.run(send_cmd)

if __name__ == "__main__":
    asyncio.run(main())
