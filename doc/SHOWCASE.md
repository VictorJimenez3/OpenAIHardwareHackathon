# Bedside Check-In showcase

Test whether a person who can only answer yes or no can send a specific request to a caregiver. The patient hears one question at a time and reviews the exact words. A separate device shows the request and returns a physical acknowledgment.

## Demonstrate a pain check-in

Ask for a short, fictional caregiver observation with a clue about where the patient may be uncomfortable or what help they may want. Run the host from the project folder with both boards connected by USB.

```sh
uv run --python 3.12 --extra live python -m bedside \
  --handoff 'The patient gestured toward their right shoulder earlier when uncomfortable.'
```

1. Tap **Start**. GPT-Live asks **“Are you experiencing pain?”** through the Mac speaker.
2. Answer **Yes**. The OpenAI planner uses the fictional note to propose one follow-up, such as **“Is the pain in your right shoulder?”**
3. Answer **Yes** to confirm the detail or **No** to keep the plain pain report. Check the sentence on the CoreS3 and listen to the exact readback.
4. Tap **Send**. Show the same text on the StickS3. Press its blue button and show **Caregiver acknowledged** on the CoreS3.

Use `--voice local` to play the questions using macOS speech. The review readback always uses macOS speech. If a request gets no response, the CoreS3 displays **No answer yet** and waits.

## Repeatable runs

The default script works offline with `--voice local`. Tap **Start → Yes → Yes → Send**, then press the StickS3’s blue button. The reviewed text is “I'm in pain and would like help changing my position.”

For repeatable model choices, use `--adaptive --scenario comfort` or `--adaptive --scenario mouth`. Answer **No** to the pain question in both runs. The model selected repositioning for comfort and mouth care for the dry-mouth context in live API tests. In `--scenario pain`, a confirmed **Yes** can go straight to review.

Use `--simulate both --voice off` with any of these modes to test in a separate terminal. Enter `start`, `yes`, `no`, `send`, `cancel`, `ack`, or `quit` on separate lines.

## Evidence and next test

The team ran the touchscreen, exact review, caregiver display, and acknowledgment on both physical boards. The Mac generated GPT-Live audio in the same run. The CoreS3 camera streamed at about five frames per second. Live head-gesture trials reached calibration but missed deliberate movements, so the showcase uses touch or a separately verified swipe input.

Compare a fixed board and the spoken check-in using the same access method and intended request. Record the exact message, deliberate actions, elapsed time, and retries. The [project demo plan](USER_STORY_AND_DEMO.md) includes the comparison worksheet.
