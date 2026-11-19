# Bedside AI communication assistant

## Project decision

Build a **patient-initiated, spoken check-in for an awake ICU patient who cannot speak and has difficulty pointing or typing**. The CoreS3 faces the patient. An intentional nod or shake can answer the active question if camera recognition passes a live test. Touch gives an alternate input. The patient approves the exact message, the StickS3 displays it, and the caregiver acknowledges it with a button.

This is the two-device prototype agreed in the original chat. The adaptation for the 90-minute build is a wired connection from each board to the Mac. [Approved project discussion](https://chatgpt.com/c/6aff5aab-d474-83ea-a230-07106424ee91)

## Physical setup

| Item | Role |
| --- | --- |
| **CoreS3 K128 + DinBase** | Patient-side screen, touchscreen input and camera. Mount it facing the volunteer at a distance and angle you can repeat. CoreS3 speaker playback is optional in this build. |
| **StickS3 K150** | Caregiver display and physical acknowledgment button. Wrap the full confirmed message across lines. |
| **Mac + two USB data cables** | Connect both boards directly by USB. The Mac routes events, runs the check-in and local vision, and plays audio. Bring a USB hub if two data ports are unavailable. GPT-Live needs internet from the Mac. |
| **Gesture Unit U127** | Optional alternate select input on CoreS3 Port A. Use a tested swipe 5–15 cm from the sensor. The CoreS3 touchscreen is the first fallback. |

[CoreS3 hardware](https://docs.m5stack.com/en/core/CoreS3) · [StickS3 hardware](https://docs.m5stack.com/en/core/StickS3) · [Gesture Unit specifications](https://docs.m5stack.com/en/unit/Unit_Gesture)

## 90-minute build order

| Elapsed time | Result to verify |
| --- | --- |
| **0–15 min** | Get both boards onto separate macOS serial ports and label their roles. Make the StickS3 display and acknowledge a test message. Capture a frame from the actual CoreS3 camera in the intended mounting position. Start a GPT-Live connection test as soon as the key is available. |
| **15–40 min** | Finish the full touch-based flow. Start → pain → positioning → review → approve → StickS3 displays the exact text → caregiver presses acknowledge → patient sees acknowledgment. Use the Mac’s local voice while testing. |
| **40–65 min** | Test a short sequence of real CoreS3 frames over USB and recognize deliberate nods and shakes on the Mac. At minute 65, choose camera or touch for the showcase based on actual results. |
| **65–80 min** | Add GPT-Live questions to the working flow. Keep final readback tied to the exact text. Test one unclear response and verify that it sends no message. |
| **80–90 min** | Rehearse the complete two-board handoff twice. If there is time, run the same task with fixed choices and record time, actions and errors. |

## Demo decisions

- **Camera.** Aim for at least nine correct classifications in ten deliberate trials across nods and shakes, plus a few neutral trials with no accepted response. Check with the actual volunteer and framing. Use touch if frames, timing or classification are unreliable. Introduce the demo by naming the input actually in use.
- **Voice.** Use GPT-Live-1.1 for the spoken questions when the session is available. Use the Mac speaker first. When voice access fails, use local audio for the scripted exchange and describe that at the table.
- **Physical handoff.** Prioritize the real StickS3 displaying the exact approved text and returning a button acknowledgment. A Mac caregiver view can debug delivery. If StickS3 is unavailable, describe that demo as a single-device simulation of the handoff.

The CoreS3 gesture sketch previously compiled. The local Arduino board profiles expose USB CDC on boot for both boards. Camera frame transfer, nod/shake recognition and the full two-device flow are still unverified on the kit.

## Companion docs

- [Technical design, objective and research](./TECHNICAL_RESEARCH.md)
- [User story and showcase script](./USER_STORY_AND_DEMO.md)
