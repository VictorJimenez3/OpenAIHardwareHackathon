#pragma once

// Optional CoreS3 camera transport.  The patient UI remains usable when the
// camera library is not installed or the sensor cannot be initialized.
// Frames are deliberately sent as one newline-delimited JSON event so the Mac
// can keep the same serial connection for screen events and camera samples.

#include <Arduino.h>

#if __has_include(<esp_camera.h>) && __has_include(<M5Unified.h>)
#define BEDSIDE_PATIENT_CAMERA_AVAILABLE 1
#include <esp_camera.h>
#include <M5Unified.h>
#else
#define BEDSIDE_PATIENT_CAMERA_AVAILABLE 0
#endif

class PatientCamera {
 public:
  PatientCamera() = default;

  // Camera startup is lazy.  This keeps boot fast and makes a normal touch
  // check-in independent from camera hardware or PSRAM availability.
  bool setEnabled(bool enabled, const String& promptId, Stream& output) {
    enabled_ = enabled;
    promptId_ = promptId;
    if (enabled_) {
      consecutiveCaptureFailures_ = 0;
      captureErrorSent_ = false;
      cameraStartedAt_ = millis();
    }
    if (!enabled_) {
      shutdown();
      return true;
    }
    return ensureStarted(output);
  }

  void disable() {
    enabled_ = false;
    shutdown();
  }

  bool enabled() const { return enabled_; }
  bool available() const {
#if BEDSIDE_PATIENT_CAMERA_AVAILABLE
    return started_;
#else
    return false;
#endif
  }

  // Call from loop(). The interval leaves time for touch between frames
  // while sampling a quick nod more than once over native USB CDC.
  void update(Stream& output, const String& promptId) {
    if (!enabled_) return;
    if (promptId != promptId_) {
      promptId_ = promptId;
      sequence_ = 0;
    }
    if (!ensureStarted(output)) return;
    if (millis() - cameraStartedAt_ >= kMaxBurstMs) {
      sendError(output, promptId_, "camera_timeout");
      enabled_ = false;
      shutdown();
      return;
    }
    if (millis() - lastFrameAt_ < kFrameIntervalMs) return;
    lastFrameAt_ = millis();
    sendFrame(output);
  }

 private:
  static constexpr uint32_t kFrameIntervalMs = 80;
  static constexpr uint32_t kMaxBurstMs = 12000;
  static constexpr size_t kMaxJpegBytes = 40 * 1024;
  bool enabled_ = false;
  bool started_ = false;
  bool errorSent_ = false;
  uint32_t lastFrameAt_ = 0;
  uint32_t cameraStartedAt_ = 0;
  uint32_t sequence_ = 0;
  uint8_t consecutiveCaptureFailures_ = 0;
  bool captureErrorSent_ = false;
  String promptId_;
#if BEDSIDE_PATIENT_CAMERA_AVAILABLE
  camera_fb_t* frame_ = nullptr;
#endif

  static void sendError(Stream& output, const String& promptId, const char* error) {
    output.print(F("{\"type\":\"camera_error\",\"prompt_id\":\""));
    writeJsonString(output, promptId);
    output.print(F("\",\"error\":\""));
    writeJsonString(output, String(error));
    output.println(F("\"}"));
  }

  static void writeJsonString(Stream& output, const String& value) {
    for (uint16_t i = 0; i < value.length(); ++i) {
      const char c = value[i];
      if (c == '\\' || c == '"') output.write('\\');
      if (c == '\n') {
        output.print(F("\\n"));
      } else if (c == '\r') {
        output.print(F("\\r"));
      } else {
        output.write(static_cast<uint8_t>(c));
      }
    }
  }

  bool ensureStarted(Stream& output) {
    if (started_) return true;
    if (errorSent_) return false;
#if BEDSIDE_PATIENT_CAMERA_AVAILABLE
    // The M5Stack CoreS3 board profile initializes the internal bus before
    // setup() returns. The ESP camera SCCB-ng driver can register the GC0308
    // as another device on that active bus. This keeps touch available while
    // the camera is streaming and avoids taking ownership of the bus.
    camera_config_t config = {
        .pin_pwdn = -1,
        .pin_reset = -1,
        .pin_xclk = -1,
        .pin_sccb_sda = -1,
        .pin_sccb_scl = -1,
        .pin_d7 = 47,
        .pin_d6 = 48,
        .pin_d5 = 16,
        .pin_d4 = 15,
        .pin_d3 = 42,
        .pin_d2 = 41,
        .pin_d1 = 40,
        .pin_d0 = 39,
        .pin_vsync = 46,
        .pin_href = 38,
        .pin_pclk = 45,
        .xclk_freq_hz = 20000000,
        .ledc_timer = LEDC_TIMER_0,
        .ledc_channel = LEDC_CHANNEL_0,
        .pixel_format = PIXFORMAT_RGB565,
        // Allocate the capture buffers at the native size used for the
        // gesture burst. Do not resize the sensor after init because the
        // driver has already allocated fixed-size frame buffers.
        .frame_size = FRAMESIZE_QVGA,
        .jpeg_quality = 0,
        .fb_count = 2,
        .fb_location = CAMERA_FB_IN_PSRAM,
        .grab_mode = CAMERA_GRAB_WHEN_EMPTY,
        .sccb_i2c_port = M5.In_I2C.getPort(),
    };
    const bool initialized = config.sccb_i2c_port >= 0 && esp_camera_init(&config) == ESP_OK;
    if (!initialized) {
      sendError(output, promptId_, "camera_init_failed");
      errorSent_ = true;
      return false;
    }
    // JPEG quality is set during RGB565-to-JPEG conversion below. Keeping the
    // sensor's native QQVGA allocation avoids a capture-buffer size mismatch.
    started_ = true;
    return true;
#else
    sendError(output, promptId_, "camera_library_unavailable");
    errorSent_ = true;
    return false;
#endif
  }

  void shutdown() {
#if BEDSIDE_PATIENT_CAMERA_AVAILABLE
    if (!started_) return;
    if (frame_ != nullptr) {
      releaseFrame();
    }
    // The SCCB-ng driver removes only its borrowed camera device. M5's
    // internal bus remains alive for the touch controller.
    esp_camera_deinit();
    started_ = false;
#endif
  }

  static const char* base64Alphabet() {
    return "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  }

  static void writeBase64(Stream& output, const uint8_t* bytes, size_t length) {
    const char* alphabet = base64Alphabet();
    uint8_t blockOutput[384];
    size_t blockLength = 0;
    for (size_t i = 0; i < length; i += 3) {
      const size_t remaining = length - i;
      const uint32_t block = (static_cast<uint32_t>(bytes[i]) << 16) |
                             (remaining > 1 ? static_cast<uint32_t>(bytes[i + 1]) << 8 : 0) |
                             (remaining > 2 ? bytes[i + 2] : 0);
      const char encoded[4] = {
          alphabet[(block >> 18) & 0x3F],
          alphabet[(block >> 12) & 0x3F],
          remaining > 1 ? alphabet[(block >> 6) & 0x3F] : '=',
          remaining > 2 ? alphabet[block & 0x3F] : '=',
      };
      for (size_t j = 0; j < sizeof(encoded); ++j) {
        blockOutput[blockLength++] = static_cast<uint8_t>(encoded[j]);
        if (blockLength == sizeof(blockOutput)) {
          output.write(blockOutput, blockLength);
          blockLength = 0;
        }
      }
    }
    if (blockLength > 0) {
      output.write(blockOutput, blockLength);
    }
  }

  void noteCaptureFailure(Stream& output) {
    if (consecutiveCaptureFailures_ < 255) ++consecutiveCaptureFailures_;
    if (consecutiveCaptureFailures_ < 5 || captureErrorSent_) return;
    captureErrorSent_ = true;
    sendError(output, promptId_, "capture_failed");
    enabled_ = false;
    shutdown();
  }

  void sendFrame(Stream& output) {
#if BEDSIDE_PATIENT_CAMERA_AVAILABLE
    if (!captureFrame() || frame_ == nullptr) {
      // A transient capture failure is kept short so the next interval can
      // recover without taking down the check-in.
      noteCaptureFailure(output);
      return;
    }

    uint8_t* jpeg = frame_->buf;
    size_t jpegLength = frame_->len;
    uint8_t* converted = nullptr;
    size_t convertedLength = 0;
    if (frame_->format != PIXFORMAT_JPEG) {
      if (!frame2jpg(frame_, 40, &converted, &convertedLength)) {
        releaseFrame();
        noteCaptureFailure(output);
        return;
      }
      jpeg = converted;
      jpegLength = convertedLength;
    }

    // Never put an unbounded frame on the CDC line. A later interval can
    // recover after a transiently complex scene.
    if (jpegLength > kMaxJpegBytes) {
      if (converted != nullptr) free(converted);
      releaseFrame();
      return;
    }

    consecutiveCaptureFailures_ = 0;

    output.print(F("{\"type\":\"frame\",\"prompt_id\":\""));
    writeJsonString(output, promptId_);
    output.print(F("\",\"seq\":"));
    output.print(sequence_++);
    output.print(F(",\"jpeg\":\""));
    writeBase64(output, jpeg, jpegLength);
    output.println(F("\"}"));
    output.flush();

    if (converted != nullptr) free(converted);
    releaseFrame();
#else
    (void)output;
#endif
  }

#if BEDSIDE_PATIENT_CAMERA_AVAILABLE
  bool captureFrame() {
    frame_ = esp_camera_fb_get();
    return frame_ != nullptr;
  }

  void releaseFrame() {
    if (frame_ == nullptr) return;
    esp_camera_fb_return(frame_);
    frame_ = nullptr;
  }
#endif
};
