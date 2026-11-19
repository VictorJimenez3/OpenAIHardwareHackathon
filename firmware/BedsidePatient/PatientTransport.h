#pragma once

#include <Arduino.h>

inline String jsonEscape(const String& value) {
  String escaped;
  escaped.reserve(value.length() + 8);
  for (size_t i = 0; i < value.length(); ++i) {
    const char c = value[i];
    if (c == '\n') escaped += "\\n";
    else if (c == '\r') escaped += "\\r";
    else if (c == '\t') escaped += "\\t";
    else {
      if (c == '\\' || c == '"') escaped += '\\';
      escaped += c;
    }
  }
  return escaped;
}

class PatientTransport {
 public:
  void begin() {
#if ARDUINO_USB_CDC_ON_BOOT && ARDUINO_USB_MODE
    // Arduino starts TinyUSB before setup(), but setup() starts HWCDC. The
    // HWCDC setters replace live ISR buffers, so call them before begin().
    Serial.setRxBufferSize(4096);
    frameBufferReady_ = Serial.setTxBufferSize(64 * 1024) == 64 * 1024;
    if (!frameBufferReady_) Serial.setTxBufferSize(2048);
    Serial.setTxTimeoutMs(0);
#endif
    Serial.begin(115200);
  }

  bool supportsFrames() const { return frameBufferReady_; }

  bool tryControl(const String& line) {
    return controlCount_ == 0 && tryWrite(line, 0);
  }

  void sendControl(const String& line) {
    if (tryControl(line)) return;
    if (controlCount_ == kControlQueueSize) return;
    controls_[(controlHead_ + controlCount_) % kControlQueueSize] = line;
    ++controlCount_;
  }

  void update() {
    while (controlCount_ && tryWrite(controls_[controlHead_], 0)) {
      controls_[controlHead_] = "";
      controlHead_ = (controlHead_ + 1) % kControlQueueSize;
      --controlCount_;
    }
  }

  bool canSendFrame(size_t length) const {
    return frameBufferReady_ && controlCount_ == 0 && !finishPartial_ &&
           hasSpace(length + kControlReserve);
  }

  bool sendFrame(const char* line, size_t length) {
    return canSendFrame(length) && tryWrite(line, length, kControlReserve);
  }

 private:
  static constexpr size_t kControlReserve = 2048;
  static constexpr size_t kControlQueueSize = 4;
  String controls_[kControlQueueSize];
  size_t controlHead_ = 0;
  size_t controlCount_ = 0;
  bool frameBufferReady_ = false;
  bool finishPartial_ = false;

  static bool hasSpace(size_t length) {
    const int available = Serial.availableForWrite();
    return available >= 0 && static_cast<size_t>(available) >= length;
  }

  bool tryWrite(const String& line, size_t reserve) {
    return tryWrite(line.c_str(), line.length(), reserve);
  }

  bool tryWrite(const char* line, size_t length, size_t reserve) {
    if (length == 0) return false;
    if (finishPartial_) {
      if (!hasSpace(1) || Serial.write('\n') != 1) return false;
      finishPartial_ = false;
    }
    if (!hasSpace(length + reserve)) return false;

    // The loop is the only producer and the ISR only frees space. A line
    // admitted here fits in one HWCDC byte-buffer item even if USB stalls.
    const size_t sent = Serial.write(reinterpret_cast<const uint8_t*>(line), length);
    if (sent == length) return true;
    finishPartial_ = sent != 0;
    return false;
  }
};
