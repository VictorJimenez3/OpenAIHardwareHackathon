#include <Arduino.h>
#include <M5Unified.h>
#include "PatientCamera.h"
#include "PatientGesture.h"

// CoreS3 patient-side display. The Mac owns the check-in state machine and
// sends one newline-delimited JSON "screen" event at a time.
//
// Device -> Mac:
//   {"type":"hello","role":"patient"}
//   {"type":"input","prompt_id":"pain-1","value":"yes","source":"touch"}
// Mac -> device:
//   {"type":"ping"}
//   {"type":"screen","prompt_id":"pain-1","title":"...","text":"...",
//    "left_label":"Yes","left_value":"yes","right_label":"No","right_value":"no",
//    "status":"Touch an answer"}

namespace {

// M5GFX's default color type is RGB565. Keeping the source colors in RGB888
// and converting once prevents accidental truncation of six-digit colors.
constexpr uint16_t rgb565(uint8_t red, uint8_t green, uint8_t blue) {
  return static_cast<uint16_t>(((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3));
}

constexpr uint16_t kBackground = rgb565(11, 18, 32);
constexpr uint16_t kPanel = rgb565(22, 36, 58);
constexpr uint16_t kPanelAlt = rgb565(30, 51, 80);
constexpr uint16_t kText = rgb565(243, 247, 255);
constexpr uint16_t kMuted = rgb565(156, 175, 200);
constexpr uint16_t kAccent = rgb565(53, 208, 186);
constexpr uint16_t kAccentDark = rgb565(19, 79, 80);
constexpr uint16_t kWarning = rgb565(246, 196, 83);

int screenWidth = 320;
int screenHeight = 240;

String promptId = "start-1";
String titleText = "Start a check-in";
String bodyText = "Use the large buttons to answer. You can review the exact message before it is shared.";
String leftLabel = "Start";
String leftValue = "start";
String rightLabel;
String rightValue;
String statusText = "Waiting for your choice";
String serialLine;
unsigned long lastHelloAt = 0;
unsigned long lastInputAt = 0;
unsigned long lastPaintAt = 0;
bool connected = false;
bool dirty = true;
bool inputLatched = false;
bool touchArmed = true;
PatientCamera patientCamera;
PatientGesture patientGesture;

struct Button {
  int x;
  int y;
  int w;
  int h;
  String label;
  String value;
  bool visible;
};

String jsonValue(const String &json, const char *key) {
  String needle = String("\"") + key + "\"";
  int keyAt = json.indexOf(needle);
  if (keyAt < 0) return String();
  int colon = json.indexOf(':', keyAt + needle.length());
  if (colon < 0) return String();
  int quote = json.indexOf('"', colon + 1);
  if (quote < 0) return String();

  String out;
  bool escaped = false;
  for (int i = quote + 1; i < json.length(); ++i) {
    char c = json[i];
    if (escaped) {
      if (c == 'n') out += '\n';
      else if (c == 'r') out += '\r';
      else if (c == 't') out += '\t';
      else out += c;
      escaped = false;
    } else if (c == '\\') {
      escaped = true;
    } else if (c == '"') {
      break;
    } else {
      out += c;
    }
  }
  return out;
}

bool jsonTrue(const String &json, const char *key) {
  String needle = String("\"") + key + "\"";
  int keyAt = json.indexOf(needle);
  if (keyAt < 0) return false;
  int colon = json.indexOf(':', keyAt + needle.length());
  if (colon < 0) return false;
  String value = json.substring(colon + 1);
  value.trim();
  return value.startsWith("true");
}

String jsonEscape(const String &value) {
  String out;
  out.reserve(value.length() + 8);
  for (unsigned int i = 0; i < value.length(); ++i) {
    char c = value[i];
    if (c == '\\' || c == '"') out += '\\';
    if (c == '\n') {
      out += "\\n";
      continue;
    }
    if (c == '\r') {
      out += "\\r";
      continue;
    }
    out += c;
  }
  return out;
}

void sendHello() {
  Serial.println(F("{\"type\":\"hello\",\"role\":\"patient\"}"));
  Serial.flush();
  lastHelloAt = millis();
}

void sendInput(const String &value) {
  if (value.isEmpty() || inputLatched) return;
  inputLatched = true;
  Serial.print(F("{\"type\":\"input\",\"prompt_id\":\""));
  Serial.print(jsonEscape(promptId));
  Serial.print(F("\",\"value\":\""));
  Serial.print(jsonEscape(value));
  Serial.println(F("\",\"source\":\"touch\"}"));
  Serial.flush();
  lastInputAt = millis();
  statusText = "Response sent";
  dirty = true;
}

void sendGestureInput(const String &value) {
  if (value.isEmpty() || inputLatched) return;
  inputLatched = true;
  Serial.print(F("{\"type\":\"input\",\"prompt_id\":\""));
  Serial.print(jsonEscape(promptId));
  Serial.print(F("\",\"value\":\""));
  Serial.print(jsonEscape(value));
  Serial.println(F("\",\"source\":\"gesture\"}"));
  Serial.flush();
  lastInputAt = millis();
  statusText = "Response sent";
  dirty = true;
}

void drawWrapped(const String &text, int x, int y, int maxWidth, int lineHeight, int maxLines) {
  String line;
  int lineNo = 0;
  int start = 0;
  while (start <= text.length() && lineNo < maxLines) {
    int end = text.indexOf(' ', start);
    if (end < 0) end = text.length();
    String word = text.substring(start, end);
    String candidate = line.isEmpty() ? word : line + " " + word;
    if (!line.isEmpty() && M5.Display.textWidth(candidate) > maxWidth) {
      M5.Display.drawString(line, x, y + lineNo * lineHeight);
      line = word;
      ++lineNo;
    } else {
      line = candidate;
    }
    start = end + 1;
    if (start > text.length()) break;
  }
  if (lineNo < maxLines && !line.isEmpty()) {
    M5.Display.drawString(line, x, y + lineNo * lineHeight);
  }
}

Button buttonForTouch(int16_t x, int16_t y) {
  const int padding = max(14, screenWidth / 18);
  const int gap = 10;
  const int height = min(64, max(56, screenHeight / 4));
  const int yTop = screenHeight - height - 20;
  const bool hasLeft = !leftLabel.isEmpty();
  const bool hasRight = !rightLabel.isEmpty();
  const int count = (hasLeft ? 1 : 0) + (hasRight ? 1 : 0);
  const int width = count > 1 ? (screenWidth - 2 * padding - gap) / 2 : screenWidth - 2 * padding;
  const int leftX = padding;
  const int rightX = count > 1 ? padding + width + gap : padding;
  Button left{leftX, yTop, width, height, leftLabel, leftValue, hasLeft};
  Button right{rightX, yTop, width, height, rightLabel, rightValue, hasRight};
  if (left.visible && x >= left.x && x < left.x + left.w && y >= left.y && y < left.y + left.h) return left;
  if (right.visible && x >= right.x && x < right.x + right.w && y >= right.y && y < right.y + right.h) return right;
  return Button{0, 0, 0, 0, String(), String(), false};
}

void drawButton(const Button &button, bool pressed) {
  if (!button.visible) return;
  uint16_t fill = pressed ? kAccentDark : kPanelAlt;
  M5.Display.fillRoundRect(button.x, button.y, button.w, button.h, 12, fill);
  M5.Display.drawRoundRect(button.x, button.y, button.w, button.h, 12, kAccent);
  M5.Display.setTextColor(kText);
  M5.Display.setTextSize(2);
  int textX = button.x + (button.w - M5.Display.textWidth(button.label)) / 2;
  M5.Display.drawString(button.label, textX, button.y + 14);
}

void paint() {
  if (!dirty) return;
  lastPaintAt = millis();
  dirty = false;

  M5.Display.fillScreen(kBackground);
  const int padding = max(14, screenWidth / 18);
  const int headerHeight = 36;
  M5.Display.fillRect(0, 0, screenWidth, headerHeight, kPanel);
  M5.Display.setTextSize(1);
  M5.Display.setTextColor(kMuted);
  M5.Display.drawString(F("BEDSIDE CHECK-IN"), padding, 12);
  const String connectionLabel = connected ? F("CONNECTED") : F("CONNECTING");
  M5.Display.setTextColor(connected ? kAccent : kWarning);
  const int dotX = screenWidth - 10;
  const int labelX = dotX - 8 - M5.Display.textWidth(connectionLabel);
  M5.Display.drawString(connectionLabel, labelX, 10);
  M5.Display.fillCircle(dotX, 17, 5, connected ? kAccent : kWarning);

  M5.Display.setTextColor(kText);
  M5.Display.setTextSize(2);
  drawWrapped(titleText, padding, 48, screenWidth - 2 * padding, 24, 2);

  M5.Display.setTextColor(kMuted);
  M5.Display.setTextSize(2);
  drawWrapped(bodyText, padding, 82, screenWidth - 2 * padding, 23, 3);

  const int gap = 10;
  const int height = min(64, max(56, screenHeight / 4));
  const int yTop = screenHeight - height - 20;
  const bool hasLeft = !leftLabel.isEmpty();
  const bool hasRight = !rightLabel.isEmpty();
  const int count = (hasLeft ? 1 : 0) + (hasRight ? 1 : 0);
  const int width = count > 1 ? (screenWidth - 2 * padding - gap) / 2 : screenWidth - 2 * padding;
  const int leftX = padding;
  const int rightX = count > 1 ? padding + width + gap : padding;
  Button left{leftX, yTop, width, height, leftLabel, leftValue, hasLeft};
  Button right{rightX, yTop, width, height, rightLabel, rightValue, hasRight};
  drawButton(left, false);
  drawButton(right, false);

  M5.Display.setTextColor(kMuted);
  M5.Display.setTextSize(1);
  if (!statusText.isEmpty()) M5.Display.drawString(statusText, padding, screenHeight - 14);
}

void handleScreen(const String &line) {
  String nextPrompt = jsonValue(line, "prompt_id");
  bool newPrompt = !nextPrompt.isEmpty() && nextPrompt != promptId;
  bool deliberateRetry = jsonTrue(line, "retry") || jsonTrue(line, "reset_input");
  if (newPrompt) {
    promptId = nextPrompt;
    statusText = "";
  }
  if (newPrompt || deliberateRetry) {
    inputLatched = false;
    // A prompt may arrive while a finger is still on the glass. Require a
    // release before accepting a response for this prompt.
    touchArmed = false;
  }

  String next = jsonValue(line, "title");
  if (!next.isEmpty()) titleText = next;
  next = jsonValue(line, "text");
  if (!next.isEmpty()) bodyText = next;
  leftLabel = jsonValue(line, "left_label");
  leftValue = jsonValue(line, "left_value");
  rightLabel = jsonValue(line, "right_label");
  rightValue = jsonValue(line, "right_value");
  next = jsonValue(line, "status");
  if (!next.isEmpty()) statusText = next;
  if (!connected) {
    connected = true;
  }
  dirty = true;
}

void handleLine(const String &line) {
  String type = jsonValue(line, "type");
  if (type == "screen") {
    handleScreen(line);
  } else if (type == "camera") {
    String cameraPrompt = jsonValue(line, "prompt_id");
    if (cameraPrompt.isEmpty()) cameraPrompt = promptId;
    const bool enabled = jsonTrue(line, "enabled");
    patientCamera.setEnabled(enabled, cameraPrompt, Serial);
    statusText = enabled ? "Camera active" : "Camera off";
    dirty = true;
  } else if (type == "gesture") {
    String gesturePrompt = jsonValue(line, "prompt_id");
    if (gesturePrompt.isEmpty()) gesturePrompt = promptId;
    const bool enabled = jsonTrue(line, "enabled");
    const bool gestureReady = patientGesture.setEnabled(enabled, gesturePrompt, Serial);
    statusText = !enabled ? "Gesture input off" : (gestureReady ? "Gesture input active" : "Gesture unavailable");
    dirty = true;
  } else if (type == "ping") {
    connected = true;
    sendHello();
  }
}

void readSerial() {
  while (Serial.available() > 0) {
    char c = static_cast<char>(Serial.read());
    if (c == '\n') {
      if (serialLine.length() > 0) handleLine(serialLine);
      serialLine = "";
    } else if (c != '\r' && serialLine.length() < 2048) {
      serialLine += c;
    }
  }
}

}  // namespace

void setup() {
  auto config = M5.config();
  M5.begin(config);
  // CoreS3 rotation 0 is the physical landscape orientation used by the
  // camera and the demo stand. Keep a defensive landscape fallback for a
  // board profile that reports portrait dimensions.
  M5.Display.setRotation(0);
  screenWidth = M5.Display.width();
  screenHeight = M5.Display.height();
  if (screenWidth < screenHeight) {
    M5.Display.setRotation(3);
    screenWidth = M5.Display.width();
    screenHeight = M5.Display.height();
  }
  M5.Display.setTextDatum(top_left);
  M5.Display.setTextFont(1);
  Serial.begin(115200);
  delay(50);
  sendHello();
  paint();
}

void loop() {
  M5.update();
  readSerial();
  patientCamera.update(Serial, promptId);

  String gestureValue;
  const bool allowGestureYes = leftValue == "yes" || rightValue == "yes";
  const bool allowGestureNo = leftValue == "no" || rightValue == "no";
  const bool allowGestureStart = leftValue == "start" || rightValue == "start";
  if (patientGesture.poll(promptId, allowGestureYes, allowGestureNo, allowGestureStart,
                          inputLatched, gestureValue)) {
    sendGestureInput(gestureValue);
  }

  // Re-announce after reconnects and make the serial link self-discovering.
  if (millis() - lastHelloAt > 5000) sendHello();
  auto touch = M5.Touch.getDetail();
  // Arm from the actual current state, not only the one-frame release event.
  // A screen update can arrive after the release frame and before the next
  // tap, which would otherwise discard the patient's first answer.
  if (!touch.isPressed()) touchArmed = true;
  if (millis() - lastInputAt > 800 && touch.wasPressed() && touchArmed && !inputLatched) {
    Button button = buttonForTouch(touch.x, touch.y);
    if (button.visible) {
      touchArmed = false;
      sendInput(button.value);
    }
  }
  paint();
  delay(8);
}
