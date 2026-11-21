package io.github.mkckr0.audio_share_app.service

enum class LatencyProfile(
    val prebufferMs: Int,
    val minQueueMs: Int,
    val maxQueueMs: Int,
    val holdWifiLock: Boolean,
    val deepBuffer: Boolean,
    val minTrackMs: Int,
) {
    LOW(20, 20, 100, true, false, 0),
    BALANCED(60, 40, 200, true, false, 100),
    // the deep queue absorbs Wi-Fi power save bursts
    // deep buffer output wakes the audio DSP far less often than the fast mixer
    STABLE(150, 100, 400, false, true, 200);

    companion object {
        fun fromIndex(index: Int): LatencyProfile = values().getOrElse(index) { BALANCED }
    }
}
