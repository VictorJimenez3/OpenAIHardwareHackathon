# Technical design, objective and research

## Objective

Test whether a person who can follow short questions and deliberately move their head, but has difficulty pointing or typing, can express a specific care need through a spoken check-in and receive an acknowledgment from the caregiver. The prototype represents an interaction at the bedside with a simulated patient. It delivers a confirmed statement to a demo caregiver display.

The evaluation compares the same request through a fixed-choice board and a guided check-in using the same input and confirmation rules. A flat list of AI-suggested sentences is a useful second comparison if there is time. Measure correct message, physical actions, waiting time and retries.

## Implementation

```text
CoreS3 camera, touch, optional U127
       ↕ USB data
Mac ── local vision and dialogue state ── GPT-Live for spoken prompts
       ↕ USB data
StickS3 ── exact approved message and physical acknowledgment
```

| Layer | Implementation |
| --- | --- |
| **CoreS3 firmware** | Use the installed M5Stack Arduino board profile and M5Unified. Display **Start**, the active question, **Yes / No**, and the review. Send deliberate input to the Mac. Use the CoreS3 camera sample to verify capture while touch still works. |
| **Board communication** | Use USB CDC with a separate port for each device. On connect, identify each board by role. Read and write newline-delimited messages on each link. Keep user input and answer processing tied to the current prompt. |
| **Camera experiment** | Capture a short burst only while a yes/no question is active. Start with M5Stack's RGB565 camera example; if raw frames are too large for stable serial transfer, try compressing frames on the CoreS3 with the ESP32 camera library's `fmt2jpg`. Send bounded JPEG frames and sequence IDs over the patient port; Base64 is an easy way to carry them in the same line format. Measure frame rate and drop stale frames. |
| **Local head movement** | Calibrate against a brief neutral pose at the actual demo angle. Compare pitch and yaw changes across frames to distinguish a nod from a shake. Validate on the actual CoreS3 stream. No readable face, stillness, mixed movement or a timeout leaves the question unresolved. |
| **Mac application** | Own the active prompt, accepted answers, review text, and delivery state. Use GPT-Live-1.1 for short spoken questions, with permitted wording anchored to the current state. Keep a small question tree usable when voice or AI is unavailable. If model-assisted adaptation is ready, let one model select or rephrase an allowed follow-up. |
| **StickS3 firmware** | Render the exact approved text and confirm it was displayed. One button acknowledges that message. The Mac routes this acknowledgment back to the CoreS3. |

[M5Stack camera example](https://docs.m5stack.com/en/arduino/m5cores3/camera) · [M5Stack touch example](https://docs.m5stack.com/en/arduino/m5cores3/touch) · [GPT-Live-1.1](https://developers.openai.com/api/docs/models/gpt-live-1.1) · [GPT-Live guide](https://developers.openai.com/api/docs/guides/live)

### State and event rules

- Start → pain question → positioning follow-up → exact text review → patient approval → sent → displayed → caregiver acknowledged. A **No** answer can return to a supported option or end the check-in. An uncertain or absent answer leaves the answer open and exposes touch.
- Accept camera answers and button events against the active question. Advance only once per deliberate signal. Require a return to neutral or a fresh press before accepting a subsequent response, especially at final review.
- Use an identifier for each confirmed message. The StickS3 returns separate displayed and caregiver-acknowledged events for that identifier. Clear the pending state if the connection drops or times out. Show a visible delivery failure and offer retry; a retry must not create a second message.
- Freeze the first-person review sentence before approval. Read it verbatim with local text-to-speech and send that same string to the StickS3. Model output can suggest a question, while explicit patient responses supply the message content.

Example messages on the wires:

```jsonl
{"type":"hello","role":"patient"}
{"type":"hello","role":"caregiver"}
{"type":"answer","question_id":"pain-1","value":"yes","source":"touch"}
{"type":"show_request","request_id":"r1","text":"I'm in pain and would like help changing my position."}
{"type":"displayed","request_id":"r1"}
{"type":"acknowledged","request_id":"r1"}
```

GPT-Live-1.1 processes audio and text. The Mac handles camera frames locally and passes accepted responses as text/events. The experimental ESP32 SDK's M5Stack example targets the CoreS3 Lite. Running Live on the Mac and using its speaker first avoids making board audio part of the critical path. Keep the event API key on the Mac. [Model documentation](https://developers.openai.com/api/docs/models/gpt-live-1.1) · [Experimental ESP32 examples](https://github.com/openai/openai-live-esp32)

## Research and product decisions

| Established approach | Decision for this prototype |
| --- | --- |
| **Clinical matching.** SPEACS and the Patient Communication Support Guide describe matching communication methods to patient abilities. | Focus on the ability to make a deliberate response and offer an alternate input. The clinician’s choice of communication aid comes first. [ATS workshop report](https://academic.oup.com/annalsats/article/20/4/491/8414883) · [PCSG study](https://doi.org/10.1093/annalsats/aaoag077) |
| **Boards and ICU tablets.** Partner-assisted scanning works with an agreed signal. VidaTalk already covers pain details, spoken presets, typing and drawing. | Treat fixed choices as a serious baseline. Compare using equivalent access. [ASHA AAC guidance](https://www.asha.org/practice-portal/professional-issues/augmentative-and-alternative-communication/) · [VidaTalk study](https://pmc.ncbi.nlm.nih.gov/articles/PMC11846697/) |
| **Low-movement input.** Voxello already supports intentional movement and speech-generating tablet access. AllyAAC and A3CP explore gesture-based communication. | The hardware interaction has precedents. Evaluate the full check-in and acknowledged handoff. [Voxello](https://voxello.com/product-information/) · [AllyAAC](https://doi.org/10.1145/3772318.3791273) · [A3CP](https://doi.org/10.1080/07434618.2026.2716643) |
| **AI suggestions.** Lingraphica provides conversation-based replies. SPICA and other AAC research explore context-aware assistance. | Compare the guided flow with a ranked list of complete replies when time allows. Use the same intended message and patient input. [Lingraphica Conversations](https://lingraphica.com/hub-support/conversations/conversations-feature-faq/) · [SPICA](https://doi.org/10.1145/3742413.3789116) · [Interactive AAC suggestions](https://doi.org/10.1145/3800645.3813056) |
| **Clarification and effort.** ReferClaw asks a short question when it would resolve a meaningful ambiguity. SpeakFaster measures motor effort as well as communication speed. | Ask about a detail only when it affects what will be sent. Record input actions, elapsed time, retries and accuracy. [ReferClaw](https://arxiv.org/abs/2611.07683) · [SpeakFaster](https://research.google/pubs/using-large-language-models-to-accelerate-communication-for-eye-gaze-typing-users-with-als/) |
| **Vision.** VideoFDB found gaps in how voice-and-vision models use nonverbal cues. GazeAnywhere addresses a different task, estimating the target a person looks at. | Detect the deliberate head response as a separate input. Reserve eye tracking for later testing. [VideoFDB](https://research.nvidia.com/labs/amri/projects/video-fdb/) · [GazeAnywhere](https://research.google/pubs/gaze-target-estimation-anywhere-with-concepts/) |

The demo records a volunteer's intentional answer. The comparison and hardware tests determine what claims the team can make. Caregiver acknowledgment means receipt of the simulated message.
