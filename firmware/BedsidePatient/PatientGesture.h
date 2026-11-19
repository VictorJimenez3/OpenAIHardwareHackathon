#pragma once

// Optional, opt-in gesture input for the M5Stack Unit Gesture (U127).
//
// The unit is the PAJ7620U2 at I2C address 0x73.  On CoreS3, Port.A is
// SDA=2/SCL=1.  M5UnitUnified's board-aware wiring helper selects those pins
// without opening the CoreS3 internal I2C bus used by the camera/touch stack.
// The sensor is never probed during boot: the Mac must send
// {"type":"gesture","enabled":true} first.

#include <Arduino.h>
#include <Wire.h>
#include <M5UnitUnified.h>
#include <M5UnitUnifiedGESTURE.h>
#include <wiring/m5_unit_unified_wiring.hpp>

class PatientGesture {
 public:
  PatientGesture() = default;

  static constexpr uint8_t kGestureAddress = 0x73;

  // Enable or disable one gesture session.  A ready/error event is emitted at
  // most once for each enable cycle.  A missing sensor is a reported feature
  // error, not a reason to stop the touch UI or the serial loop.
  bool setEnabled(bool enabled, const String& promptId, Stream& output) {
    if (!enabled) {
      enabled_ = false;
      readySent_ = false;
      responseLatched_ = false;
      gestureArmed_ = false;
      promptId_ = promptId;
      if (attached_) sensor_.stopPeriodicMeasurement();
      return true;
    }

    enabled_ = true;
    if (!promptId.isEmpty() && promptId != promptId_) {
      promptId_ = promptId;
      responseLatched_ = false;
    }
    if (readySent_) return ready_;

    if (!attached_) {
      // CoreS3 Port.A is resolved by M5UnitUnified to SDA=2, SCL=1.  This is
      // deliberately done only after opt-in, keeping an unplugged unit from
      // delaying normal boot or touch handling.
      if (!m5::unit::wiring::addI2C(units_, sensor_)) {
        sendError(output, "gesture_bus");
        enabled_ = false;
        readySent_ = true;
        ready_ = false;
        return false;
      }
      attached_ = true;
    }

    // Fail fast before the official driver's long register-init sequence.
    // This keeps an unplugged unit from spending a default I2C timeout on
    // every register while the touch UI is otherwise usable.
    Wire.setTimeOut(20);
    Wire.beginTransmission(kGestureAddress);
    if (Wire.endTransmission(true) != 0) {
      sendError(output, "gesture_not_found");
      enabled_ = false;
      readySent_ = true;
      ready_ = false;
      return false;
    }

    auto cfg = sensor_.config();
    cfg.start_periodic = true;
    cfg.mode = m5::unit::paj7620u2::Mode::Gesture;
    cfg.frequency = m5::unit::paj7620u2::Frequency::Normal;
    cfg.hflip = false;
    cfg.vflip = true;
    cfg.rotation = 0;
    cfg.store_on_change = true;
    sensor_.config(cfg);

    if (!units_.begin()) {
      sendError(output, "gesture_init");
      enabled_ = false;
      readySent_ = true;
      ready_ = false;
      return false;
    }

    ready_ = true;
    readySent_ = true;
    responseLatched_ = false;
    gestureArmed_ = false;
    output.println(F("{\"type\":\"gesture_ready\"}"));
    output.flush();
    return true;
  }

  bool enabled() const { return enabled_; }
  bool ready() const { return ready_; }

  // Poll once without blocking.  Returns a semantic response only for a
  // fresh horizontal swipe.  Right means yes (or start on the start prompt),
  // Left means no.  All other PAJ7620U2 gestures are intentionally ignored:
  // there is no gesture for Send, Retry, or any other irreversible action.
  // `externalInputLatched` is the touch-side latch; sensor readings are still
  // consumed when touch won the race so they cannot leak into the next prompt.
  bool poll(const String& promptId, bool allowYes, bool allowNo, bool allowStart,
            bool externalInputLatched, String& value) {
    value = String();
    if (!enabled_ || !ready_) return false;

    if (promptId != promptId_) {
      promptId_ = promptId;
      responseLatched_ = false;
    }

    units_.update();
    if (!sensor_.updated()) return false;

    const auto gesture = sensor_.gesture();
    sensor_.discard();
    // Require a neutral reading between horizontal swipes.  This prevents a
    // hand held in place, or a stale reading that arrived while touch won the
    // race, from becoming an answer for the next prompt.
    if (gesture == m5::unit::paj7620u2::Gesture::None) {
      gestureArmed_ = true;
      return false;
    }
    if (gesture == m5::unit::paj7620u2::Gesture::Right ||
        gesture == m5::unit::paj7620u2::Gesture::Left) {
      const bool freshSwipe = gestureArmed_;
      gestureArmed_ = false;
      if (!freshSwipe) return false;
    }
    if (gesture == m5::unit::paj7620u2::Gesture::Right) {
      if (allowStart) {
        value = F("start");
      } else if (allowYes) {
        value = F("yes");
      } else {
        return false;
      }
    } else if (gesture == m5::unit::paj7620u2::Gesture::Left) {
      if (!allowNo) return false;
      value = F("no");
    } else {
      return false;
    }

    if (responseLatched_ || externalInputLatched) return false;

    responseLatched_ = true;
    return true;
  }

 private:
  void sendError(Stream& output, const char* error) {
    output.print(F("{\"type\":\"gesture_error\",\"error\":\""));
    output.print(error);
    output.println(F("\"}"));
    output.flush();
  }

  m5::unit::UnitUnified units_;
  m5::unit::UnitGesture sensor_;
  String promptId_;
  bool enabled_ = false;
  bool attached_ = false;
  bool ready_ = false;
  bool readySent_ = false;
  bool responseLatched_ = false;
  bool gestureArmed_ = true;
};
