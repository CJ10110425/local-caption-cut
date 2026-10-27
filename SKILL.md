---
name: local-caption-cut
description: Edit a phone video (usually an iPhone .MOV of people talking, a vlog, a funny moment with friends, a talk or pitch) into a tight cut with burned-in, styled Traditional Chinese captions, entirely on this machine with FFmpeg and whisper.cpp. Use when someone asks to 剪影片, 剪成精華, 加字幕, 加特色字幕 / 綜藝字幕 / WhatSub 風格字幕, 把停頓剪掉, or to turn a raw clip into something postable, and the footage should stay local.
---

# Local caption cut

This folder is both the skill and the tool. Run commands from here with `python3 -m captioncut …`
(or `captioncut …` after `pip install -e .`). Every step reads and writes one project folder,
`<video>-edit/`, next to the source. The source file is never modified.

## Workflow

1. **Set up.** `captioncut init <video.MOV> [--style playful|whatsub-inspired]` prints duration,
   size, rotation, HDR and audio streams. Landscape casual footage suits `playful`; portrait
   talking-head suits `whatsub-inspired`. Fonts: `captioncut fonts list`, then `captioncut fonts fetch`
   if any are missing.
2. **Understand the footage.** `captioncut transcribe <project>` writes `source-transcript.txt`:
   lines with source times, `(?)` on low-confidence lines, and exact speech regions from
   silencedetect. Use `--by-pauses` when line times look off (whisper's token times drift around
   long pauses), and `--start/--end` to re-run a doubtful stretch. `captioncut sheet <project>`
   makes a contact sheet; look at it to see what happens on screen, not only what is said.
3. **Write the edit in `project.json`.**
   - `cuts`: `[{start, end, purpose}]` in source seconds, in playback order. Keep the story and the
     punchlines; drop dead air, false starts and repeats. Start and end cuts inside pauses (use the
     speech regions), not mid-word. Leave ~0.2–0.35 s of air around speech.
   - `captions`: `[{start, end, text, role}]` in **source** seconds. They are mapped onto the
     edited timeline automatically and split if a caption spans a removed section. Write
     editorial captions: drop fillers (嗯、那個、就是) and unclear words, keep the speaker's
     voice, Traditional Chinese with full-width punctuation, one thought per caption, no overlaps.
   - `role` picks the look; see the style JSON for the list (playful: dialogue, question, joke,
     reaction, hush, positive, warm; whatsub-inspired: dialogue, question, warm, emphasis,
     punchline). Use the loud roles sparingly so they land.
   - `notes`: `[{start, end, text, x, y}]` small asides like （毫不猶豫）, placed by fraction of the
     frame away from faces. `titles`: `[{clip, text, duration}]` short chapter cards at the top.
   - Optional: `tilt` / `pop` per caption, `style_overrides`, `loudness`, `grade`, `tonemap`.
4. **Build and check.** `captioncut cut`, then `captioncut captions`, then `captioncut preview` and
   open `caption-review.jpg`. Fix wording, roles, line breaks or positions in `project.json` and
   rerun `captions` + `preview` (cheap) until it reads well.
5. **Finish.** `captioncut render` then `captioncut verify`. Verify checks full decode, frame
   count, BT.709 SDR tags, A/V length, caption timing, loudness and that every clip's audio still
   matches the source within 30 ms, and writes `delivery.json` and `final-contact-sheet.jpg`.
   Look at the contact sheet. A low correlation warning on applause or music after loudness
   normalisation is usually not drift; listen to that clip rather than blocking on it.
   `captioncut all <project>` runs steps 4–5 in one go.

## Rules worth keeping

- Never overwrite the source or an existing delivery; render to a new name instead.
- Keep the original audio. Do not regenerate or re-voice speech.
- At most two lines per caption; if `captions` refuses, split the caption.
- Every glyph must exist in the role's font; the tool falls back to the style's fallback font and
  stops if nothing covers a character.
- Technical checks are not a viewing. Say what was verified and what still needs a human watch.
