# Video 01: How Does The Internet Work? (30s, portrait, 1 prompt)

**Post title:** How Your Message Crosses The World In Under A Second 🤔

**The one goal:** follow **one glowing blue orb** (your message) from the moment a boy hits send until it lands on a girl's phone in another country. There are 10 fast shots with hard cuts, and the same orb appears in every shot.

**Facts used (all verified):**
- Wi-Fi is radio waves between your phone and your router.
- Fiber optic cables carry data as pulses of light.
- About 99% of international internet traffic goes through undersea cables, not satellites (source: TeleGeography).
- The whole trip takes a fraction of a second.

**What you need:** ComfyUI (LTX), ElevenLabs, and **CapCut** (free) to add the voice, sound, and captions.

---

## STEP 1: Set up LTX in ComfyUI

Open the **LTX-2.5 Text to Video** template (the one using `ltx-2.5-22b-distilled`). On the main screen, only 2 nodes matter.

**Node 1: Resolution Selector** (small node on the left)
- **aspect_ratio** → `9:16 (Portrait Widescreen)`
- **megapixels** → `0.5` (gives about 544 × 928; CapCut upscales to 1080 × 1920 on export)

**Node 2: Text to Video (LTX-2.5)** (the big node)
- **prompt** → paste the prompt from Step 2
- **prompt_enhance** → **OFF**, so the model follows our exact prompt instead of rewriting it
- **duration** → `30` (it's in seconds; the workflow converts it to frames for you)
- **frame_rate** → `24`
- **seed** → leave it; change it to try another version

Then click **Run**.

**Where the video goes:** `D:\Comfy-Desktop\ComfyUI-Shared\output\video\` (named `LTX_2.5_t2v_...mp4`)

**Notes:**
- **No negative prompt needed.** Your model is the *distilled* version, which runs at CFG 1, and at CFG 1 the negative prompt is ignored. Everything you want or don't want goes in the main prompt.
- **LTX-2.5 always makes its own audio.** You can't turn it off; just mute it in CapCut.
- **Your GPU has 8 GB of memory** (RTX 5070 Laptop), which is tight for 30 seconds. If it fails with an "out of memory" error, lower **megapixels to 0.4**. If it still fails, lower **duration to 20**.

---

## STEP 2: Paste this ONE prompt into the **prompt** box → rename the result to **VIDEO.mp4**

**How this prompt is built** (based on 2 Zack D Films Shorts and Lightricks' own multi-shot rules):
- **10 shots, each starting with "A hard cut to" or "A match cut to."** This is how LTX-2.5 knows it's a new shot, not one long camera move.
- **Every shot names a camera move:** crash zoom, whip pan, push in, pull back, tilt down, tracking shot. This gives the fast, always-moving Zack D feel.
- **"The same glowing electric-blue orb" is repeated in every shot,** so the model keeps it the same across cuts.
- **The boy and the girl each have one fixed description** (hair and shirt), so they don't change mid-video.
- **Bright daylight, realistic video-game-style 3D, close-ups and over-the-shoulder angles,** matching the look of the reference videos.
- **"Nobody speaks"** stops LTX from making characters talk, since our voice comes from ElevenLabs.

```
A hyper-realistic 3D animated short with the look of a polished video-game cinematic: realistic Unreal Engine style rendering, bright natural daylight, saturated colors, crisp detail, shallow depth of field, vertical 9:16 frame, fast energetic editing with a new camera angle every few seconds and constant camera motion, never a static shot. It opens on an over-the-shoulder close-up of a teenage boy with messy black hair and a plain grey t-shirt sitting on his bed in a sunny bedroom, holding a black smartphone; the camera crash-zooms past his shoulder onto the phone as his thumb taps the glowing send button. A hard cut to an extreme macro shot of the phone screen: a small glowing electric-blue orb of light bursts out of the screen straight toward the camera. A hard cut to a low-angle tracking shot: the same glowing electric-blue orb flies across the sunny bedroom riding rippling translucent blue rings of radio waves, and the camera whip-pans to follow it into a white Wi-Fi router on a wooden shelf whose green lights blink rapidly. A hard cut to a fast tilt-down shot: the same glowing electric-blue orb shoots out of the router and dives through the floor, and the camera follows it into a cutaway view under a sunny street where it races along a thick cable buried in the soil. A match cut to a macro shot inside a clear glass fiber optic tunnel: the same glowing electric-blue orb stretches into a bright streak of blue light racing toward the camera. A hard cut to a high aerial wide shot of a bright blue ocean coastline on a sunny day; the camera pushes down fast toward the water. A hard cut to an underwater tracking shot: the same glowing electric-blue streak of light races along a thick black cable lying on the sandy sea floor, sunlight rays and small fish drifting past, the camera gliding low beside the cable. A hard cut to a fast dolly shot rushing down a long bright data center aisle lined with tall server racks covered in blinking blue lights, the same glowing electric-blue orb zipping ahead and diving into one server. A hard cut to a wide shot of a city skyline at night in another country; the camera pulls in fast toward one lit apartment window. A hard cut to a close-up in a dark bedroom at night: a young woman with long brown hair and a white t-shirt lies in bed as the black smartphone on her bedside table lights up with a soft blue glow; the camera pushes in on her surprised, smiling face as she picks up the phone. Sound: a quick whoosh on every cut and soft digital hums, no music, nobody speaks. No text, no words, no logos, no black frames.
```

**After it's generated, watch it once and write down when each shot starts:**

- Boy taps send: ___s
- Orb bursts out of the screen: ___s
- Waves → router: ___s
- Dives under the street: ___s
- Fiber light tunnel: ___s
- Ocean from above: ___s
- Sea floor cable: ___s
- Data center: ___s
- City at night: ___s
- Girl's phone lights up: ___s

You'll use these times in Step 8 to line up the voice.

**If it comes out wrong:**
- **Shots blend together or the orb disappears:** try 2 or 3 other seeds before changing anything.
- **Still falling apart after 3 seeds:** Lightricks' own guide says LTX-2.5 is most reliable with **2 to 4 shots per 10-second clip**, and 10 shots in one 30-second clip goes beyond that. The fallback is to split the prompt into 3 clips of 10 seconds each:
  - **Clip 1:** boy → router
  - **Clip 2:** street → sea floor
  - **Clip 3:** data center → girl
  
  Keep the first sentence (the style) at the start of all 3 prompts, then join the clips in CapCut. Ask me and I'll write the 3 prompts.

---

## STEP 3: Pick the voice in ElevenLabs

1. Go to **Text to Speech**.
2. Open the **Voice Library** and search for **"narrator"** or **"explainer."** Pick a **clear, upbeat male voice**.
3. **Model:** Eleven Multilingual v2
4. **Settings:**
   - Speed: **1.1**
   - Stability: **45%**
   - Similarity: **75%**
   - Style exaggeration: **20%**

Use this same voice for every video. It's part of the brand.

## STEP 4: In ElevenLabs, paste this script and generate → save as **VO_main.mp3**

```
You just hit send. Watch where your message goes.
It flies out of your phone as invisible radio waves, into your Wi-Fi router,
then down into cables under the street, as pulses of light.
To cross the ocean, it doesn't go to space.
It dives into a cable on the sea floor, thousands of kilometers long,
reaches a server on the other side,
and lands on your friend's phone.
The whole trip? Less than a second.
```

**Target length: about 22 to 24 seconds.** If it's longer, raise the speed to 1.15. Generate 2 or 3 takes and keep the best one.

## STEP 5: In ElevenLabs, generate the catchphrase → save as **VO_catchphrase.mp3**

```
Now you know. Stay nullish.
```

*(Swap this for the catchphrase you choose.)*

---

## STEP 6: In ElevenLabs **Sound Effects**, generate these

| Save as | Paste this prompt | Duration |
| --- | --- | --- |
| SFX_tap | `crisp smartphone screen tap, short digital click` | 0.5s |
| SFX_waves | `soft sci-fi radio wave pulse, warm whooshing hum` | 1.5s |
| SFX_beep | `small electronic device beep, router lights blinking` | 0.5s |
| SFX_zap | `fast laser light zap racing through a tube` | 1s |
| SFX_underwater | `deep underwater rumble with soft bubbles` | 3s |
| SFX_servers | `server room hum with fans and electronic beeps` | 2s |
| SFX_ding | `soft pleasant phone notification chime` | 0.5s |
| SFX_whoosh1, 2, 3 | `fast clean swoosh` (generate 3 times) | 0.5s |

## STEP 7: In ElevenLabs **Music**, paste this → save as **MUSIC.mp3**

```
Upbeat light playful electronic background music for a short science explainer video, curious and fun mood, steady tempo, no vocals, 30 seconds
```

*(If Music isn't on your plan, pick a light, upbeat track from CapCut's music library instead.)*

---

## STEP 8: Put it together in CapCut

1. **New project** → ratio **9:16** → import VIDEO.mp4 and all the audio files.
2. Put **VIDEO.mp4** on the timeline and **mute** it.
3. Put **VO_main.mp3** under it.
4. **Split the voice** at each sentence (select the audio → Split) and slide each line to its shot, using your times from Step 2:
   - "You just hit send. Watch where your message goes." → **Boy taps send** + **Orb bursts out**
   - "It flies out of your phone as invisible radio waves, into your Wi-Fi router," → **Waves → router**
   - "then down into cables under the street, as pulses of light." → **Dives under the street** + **Fiber light tunnel**
   - "To cross the ocean, it doesn't go to space." → **Ocean from above**
   - "It dives into a cable on the sea floor, thousands of kilometers long," → **Sea floor cable**
   - "reaches a server on the other side," → **Data center**
   - "and lands on your friend's phone." → **City at night**
   - "The whole trip? Less than a second." → **Girl's phone lights up**
5. **If a shot runs too long for its line,** select that part of the video → Split → **Speed 1.2x to 1.5x**. If it's too short, use **0.8x**.
6. **Make it even faster, like Zack D:** LTX shots are about 3 seconds each, but Zack D cuts every 1 to 1.5 seconds. Inside any shot longer than 2 seconds, split it once and add a **zoom keyframe** on the second half (start **100%**, end **120%**). This "punch-in" makes one shot feel like two.

## STEP 9: Add the sound effects

Place each one where its thing happens on screen:

- SFX_tap → boy taps send
- SFX_waves → waves across the room
- SFX_beep → router lights blink
- SFX_zap → light racing through the fiber
- SFX_underwater → sea floor shot
- SFX_servers → data center
- SFX_ding → the moment the girl's phone lights up
- **Whoosh** → on **every hard cut**. Rotate whoosh1, 2, and 3 so they don't sound repeated.

## STEP 10: Add the music and the catchphrase

- MUSIC on the bottom track, volume about **15%** while the voice plays.
- After "Less than a second," raise it to about **30%**.
- Put **VO_catchphrase.mp3** at the very end.

## STEP 11: Add the captions

1. Go to **Text → Auto captions → English**.
2. Style:
   - **White, bold font**, black drop shadow
   - Position: **lower middle**, about 65% down the screen
3. Split long captions so each shows **1 to 4 words**, like "the 17 year old" and "would be separated" in the Zack D video.

## STEP 12: Check, then export

- [ ] About **30 seconds** long
- [ ] Every voice line matches what's on screen
- [ ] With **sound off**, the captions still tell the story
- [ ] With **sound on**, there's no dead silence

**Export:**
- **1080 × 1920**
- **30 fps**
- Quality **High**

Save as `01_how_the_internet_works_FINAL.mp4`.

---

## STEP 13: Post it

Upload to YouTube Shorts, TikTok, Instagram Reels, and Facebook Reels.

**Title:**

```
How Your Message Crosses The World In Under A Second 🤔
```

**Caption:**

```
Your text travels under the ocean 🌊 Shirts in bio.
#howitworks #internet #techfacts #didyouknow #learnontiktok
```
