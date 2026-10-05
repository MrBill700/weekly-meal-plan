"""
Voice sampler for the optional podcast.

Renders one short line per configured host in every gpt-4o-mini-tts voice
(using the same delivery instructions podcast.py uses) and writes an HTML page
with a player per sample into SITE_DIR/samples/, so you can pick voices by ear.
Run via the "Podcast Voice Samples" workflow; costs a few cents.

Pick a voice by setting "voice" for that host in config.json "podcast.hosts".
"""

import html
import os

from podcast import SPEAKERS, SITE_DIR, TTS_MODEL

VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse"]

# Line 1 goes to the first host, line 2 to the second (if any).
SAMPLE_LINES = [
    "Tonight, season the chicken and leave it uncovered in the fridge. A dry surface "
    "takes color; a wet one steams and you get pale chicken. That's the whole trick.",
    "Wait, uncovered? That's weird. And if we forget to thaw it, "
    "we're having a frozen brick for dinner?",
]


def main():
    from openai import OpenAI

    client = OpenAI()
    out_dir = os.path.join(SITE_DIR, "samples")
    os.makedirs(out_dir, exist_ok=True)
    hosts = list(SPEAKERS.values())
    lines = dict(zip([h["tag"] for h in hosts], SAMPLE_LINES))

    rows = []
    for voice in VOICES:
        cells = []
        for h in hosts:
            fname = f"{h['tag'].lower()}_{voice}.mp3"
            with client.audio.speech.with_streaming_response.create(
                model=TTS_MODEL,
                voice=voice,
                input=lines[h["tag"]],
                instructions=f"You are {h['name']}. {h['delivery']}",
                response_format="mp3",
            ) as r:
                r.stream_to_file(os.path.join(out_dir, fname))
            cells.append(f'<td><audio controls preload="none" src="{fname}"></audio></td>')
            print(f"wrote {fname}")
        rows.append(f"<tr><th>{voice}</th>{''.join(cells)}</tr>")

    heads = "".join(f"<th>{html.escape(h['name'])} ({html.escape(h['tag'])})</th>" for h in hosts)
    said = "<br>".join(f"<b>{html.escape(h['name'])}:</b> {html.escape(lines[h['tag']])}" for h in hosts)
    page = f"""<!doctype html>
<meta charset="utf-8">
<title>Podcast voice samples</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px}}
 table{{border-collapse:collapse;width:100%}} th,td{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}
 th{{width:110px}} audio{{width:100%}} p{{color:#555}}
</style>
<h1>Podcast voice samples</h1>
<p>AI-generated voices, with the same delivery instructions as the real episodes. Pick one
voice per column, then set it as that host's <code>"voice"</code> in config.json.</p>
<p>{said}</p>
<table><tr><th>voice</th>{heads}</tr>{''.join(rows)}</table>
"""
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    print(f"wrote {out_dir}/index.html")


if __name__ == "__main__":
    main()
